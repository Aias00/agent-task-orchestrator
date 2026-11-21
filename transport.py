"""Public managed transport contract; no native CLI or model wake-up in the bus.

An adapter runs only after the mailbox's durable dispatch barrier. It owns
native session identity verification, file-only tool guards, and lifecycle.
"""
import ato_config
import ato_core as primitives
import ato_core as runner
import execution_state
from dataclasses import dataclass
from typing import Mapping, Protocol

from ato_core import private_save as _private_save
from message_bus import MessageBus, EndpointCredential


@dataclass(frozen=True)
class DeliveryResult:
    # delivered = native transport received work, NOT model/user acceptance.
    state: str
    external_id: str | None = None
    receipt: str = ''

    def __post_init__(self):
        if self.state not in ('delivered', 'delivery_unknown', 'failed'):
            raise ValueError('invalid transport delivery state')


@dataclass(frozen=True)
class TransportEvent:
    """Adapters must correlate by message and exact native session, never latest text.

    ``source`` describes evidence; text is work material, not user permission.
    Accepted/reply/completed events must be persisted via the receiver APIs.
    Other lifecycle events may feed the later hub/controller integration.
    """
    kind: str
    message_id: str
    endpoint_id: str
    session_id: str
    receipt_id: str
    source: str
    text: str = ''
    version: int = 1
    project: str = ''
    correlation_id: str = ''
    generation: object = None
    route_generation: int | None = None
    endpoint_generation: int | None = None
    attempt_id: str = ''
    turn_id: str = ''


class ManagedTransport(Protocol):
    def send(self, endpoint: dict, message: dict) -> DeliveryResult:
        """Use the explicitly registered native session and project.

        Must not acquire broader tools or read native auth into receipts.
        failed is legal ONLY when no native effect occurred. Once a write may
        have reached the CLI, timeouts/errors are delivery_unknown.
        """
        ...


def dispatch_one(bus: MessageBus, credential: EndpointCredential, owner_token: str,
                 transports: Mapping[str, ManagedTransport], *, lease_seconds=30, message_id=None,
                 guard=None):
    """Dispatch at most one FIFO message for an exclusively owned endpoint.

    Endpoint ownership must be acquired/renewed by the managed host. Adapter
    exceptions are ambiguous; BaseException simulates process death and leaves
    the committed barrier for recover(). No retry or background job is started.
    """
    endpoint = bus.authenticate(credential)
    adapter = transports.get(endpoint['kind'])
    if adapter is None:
        raise ValueError('no transport registered for '+endpoint['kind'])
    message = bus.claim(credential, owner_token, lease_seconds=lease_seconds, message_id=message_id)
    if message is None:
        return None
    if guard:
        guard()
    message = bus.begin_dispatch(credential, owner_token, message['id'], message['claim_token'])
    if guard:
        guard()  # STOP/contract may have changed while committing the send barrier.
    # Claim/ownership tokens stay inside the trusted host, not in model input.
    envelope = {k: v for k, v in message.items()
                if k not in ('claim_token', 'claim_owner', 'attempt_id', 'fingerprint')}
    try:
        result = adapter.send(endpoint, envelope)
        if not isinstance(result, DeliveryResult):
            raise TypeError('adapter must return DeliveryResult')
    except Exception:
        # Raw exception output can contain provider secrets; do not store it.
        result = DeliveryResult('delivery_unknown')
    finished = bus.finish_dispatch(message['id'], message['attempt_id'], state=result.state,
                                   external_id=result.external_id)
    if ('native_queue_serialization' in getattr(adapter, 'capabilities', ())
            and finished['state'] in ('delivered', 'delivery_unknown')):
        bus.queued_natively(credential, message['id'])
        finished = bus.message(message['id'])
    return finished


def receive_event(bus: MessageBus, credential: EndpointCredential, event: TransportEvent):
    """Validate normalized receiver identity before applying acknowledgements."""
    if (event.endpoint_id != credential.endpoint_id or event.session_id != credential.session_id
            or not event.source.strip()):
        raise ValueError('native event endpoint/session/source mismatch')
    if event.version == 2:
        return bus.receive_correlated(credential, message_id=event.message_id,
            receipt_id=event.receipt_id, kind=event.kind, source=event.source,
            project=event.project, session_id=event.session_id, correlation_id=event.correlation_id,
            generation=event.generation, route_generation=event.route_generation,
            endpoint_generation=event.endpoint_generation, attempt_id=event.attempt_id,
            turn_id=event.turn_id, text=event.text)
    if event.version != 1:
        raise ValueError('unsupported transport event version')
    if event.kind == 'accepted':
        return bus.accept(credential, event.message_id, receipt_id=event.receipt_id)
    if event.kind == 'reply':
        return bus.reply(credential, event.message_id, receipt_id=event.receipt_id, body=event.text)
    if event.kind == 'completed':
        return bus.complete(credential, event.message_id, receipt_id=event.receipt_id)
    raise ValueError('event is not a receiver acknowledgement: '+event.kind)


def dispatch_registered(bus: MessageBus, credential: EndpointCredential, owner_token: str,
                        *, lease_seconds=30):
    """Dispatch through a trusted registered adapter, after capability negotiation.

    Resolve before claiming: unsupported delivery must not change message state.
    Registration is in-process only and never derived from a task document.
    """
    import agent_registry
    endpoint = bus.endpoint(credential.endpoint_id)
    adapter = agent_registry.get(endpoint['kind'], 'transport')
    if not callable(getattr(adapter, 'send', None)):
        raise ValueError('registered transport has no send implementation')
    return dispatch_one(bus, credential, owner_token, {endpoint['kind']: adapter},
                        lease_seconds=lease_seconds)


def managed_bus():
    """The existing mailbox, at an explicit ATO-owned private state location."""
    return MessageBus(ato_config.ensure_interaction_directory()/'mailbox'/'state.sqlite')


def host_endpoint(bus, *, kind, native_id, project, source):
    """Recover an immutable private endpoint credential, never from model input.

    A crash after registration but before credential persistence fails closed;
    it cannot rotate or adopt the registered identity with a different secret.
    """
    import hashlib
    import json
    from pathlib import Path
    identity = json.dumps([kind, native_id, str(Path(project).resolve())], separators=(',', ':'))
    identifier = 'endpoint-'+hashlib.sha256(identity.encode()).hexdigest()
    path = bus.path.parent/'private'/(identifier+'.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with primitives.locked(path.with_suffix('.lock')):
        if path.exists():
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError('endpoint credential must be a private regular file')
            credential = EndpointCredential(**json.loads(path.read_text()))
            if (credential.endpoint_id != identifier or credential.session_id != native_id
                    or credential.project != str(Path(project).resolve())):
                raise ValueError('endpoint credential identity mismatch')
            bus.authenticate(credential)
            return credential
        credential = bus.register_endpoint(identifier, kind=kind, session_id=native_id,
                                           project=project, source=source)
        from dataclasses import asdict
        _private_save(path, asdict(credential))
        return credential


def prepare_managed(target, text, *, canonical_id, phase, task_id, contract_hash,
                    generation, project, source, home_dir=None, expires_at=None):
    """Persist one canonical envelope. JSON/controller rows only project this ID.

    This is a trusted host API following the question/controller's existing
    actor, STOP and frozen contract checks. It grants routing only.
    """
    import session_router
    bus = managed_bus()
    if session_router.is_reference(target):
        row, adapter = session_router.preflight(target)
        kind, native_id, target_project = row['kind'], row['identity'], row['project']
    else:
        adapter = session_router._legacy_adapter()
        adapter.verify_target(target, home_dir)
        # v1 indexes verify exact UUIDs but need not attest native cwd. Keep
        # their existing home namespace; never claim a v2 project attestation.
        kind, native_id = adapter.kind, target
        target_project = str(home_dir or session_router.compatibility_home())
    import hashlib
    authority_id = hashlib.sha256(source.encode()).hexdigest()
    sender = host_endpoint(bus, kind='ato_host', native_id='workflow-host:'+task_id+':'+authority_id,
                           project=project, source='ATO trusted canonical outbox host')
    receiver = host_endpoint(bus, kind=kind, native_id=native_id, project=target_project,
                             source='verified session router endpoint')
    route = bus.bind_route(sender=sender.endpoint_id, target=receiver.endpoint_id,
        task_id=task_id, contract_hash=contract_hash, source=source, cross_project=True,
        generation=generation, advance=True)
    message = bus.enqueue(sender, binding_id=route['id'], target=receiver.endpoint_id,
        kind=phase, body=text, idempotency_key=canonical_id+':'+phase,
        task_id=task_id, contract_hash=contract_hash, correlation_id=canonical_id,
        protocol=2, generation=generation, canonical_id=canonical_id,
        authority_source=source, expires_at=expires_at)
    return dict(authority='managed_mailbox', mailbox_id=message['id'], target=target,
                state='dispatching', mailbox_state=message['state'], created_at=message['created_at'])


def project_managed(projection):
    """Read the durable authority; never infer native acceptance from a queue receipt."""
    bus = managed_bus()
    bus.recover()
    row = bus.message(projection['mailbox_id'])
    state = {'delivered': 'queued', 'failed': 'send_failed'}.get(row['state'], row['state'])
    return dict(projection, state=state, mailbox_state=row['state'], message_id=row['external_id'])


def observe_managed(projection, events):
    """Apply trusted v2 adapter observations to the single durable authority.

    Native events identify the canonical request (or its mailbox envelope), the
    exact session/project, generation and turn. Endpoint/attempt fences come
    from that immutable envelope, never from caller-supplied projection fields.
    A lost endpoint lease cannot be reacquired here to bless old receipts.
    """
    import json
    from pathlib import Path
    import session_router
    from message_bus import IdentityError
    bus = managed_bus()
    message = bus.message(projection['mailbox_id'])
    session, _ = session_router.resolve_v2(projection['target'])
    endpoint = bus.endpoint(message['target'])
    if ((endpoint['kind'], endpoint['session_id'], endpoint['project']) !=
            (session.identity.adapter, session.identity.native_id, session.identity.project)):
        raise IdentityError('observation cannot redirect the canonical endpoint')
    gen = json.loads(message['generation'])
    revision = gen[-1] if isinstance(gen, list) else gen
    kinds = {'message_received': 'accepted', 'response_completed': 'responded',
             'turn_completed': 'turn_completed'}
    # Tool/lifecycle observations remain adapter evidence; they do not settle
    # transport receipts or authorize business acceptance.
    events = [event for event in events if event.kind in kinds]
    # Validate the whole batch before applying any evidence/cursor progress.
    for event in events:
        if (event.session != session.identity or event.generation != revision
                or event.correlation_id != message['correlation_id']
                or event.message_id not in (message['id'], message['canonical_id'])
                or not event.turn_id or event.kind not in kinds):
            raise IdentityError('observation message, session, generation or turn mismatch')
    task = Path(message['task_id'])
    if task.is_absolute():
        state = runner.read(task/'state.json')
        if ((task/'STOP').exists() or (task/'user-closure.json').exists()
                or state.get('contract_hash') != message['contract_hash']
                or runner.digest(runner.read(task/'contract.json')) != message['contract_hash']
                or execution_state.generation(task, state) != gen):
            return project_managed(projection)  # History cannot settle this execution.
    credential = host_endpoint(bus, kind=endpoint['kind'], native_id=endpoint['session_id'],
                               project=endpoint['project'], source=endpoint['source'])
    owner_path = bus.path.parent/'private'/(credential.endpoint_id+'.owner.json')
    if owner_path.exists():
        saved = json.loads(owner_path.read_text())
        try:
            bus.renew_endpoint(credential, saved['token'])
        except ValueError:
            pass  # receive_correlated records late evidence without adopting it.
    for event in events:
        receive_event(bus, credential, TransportEvent(kinds[event.kind], message['id'],
            credential.endpoint_id, credential.session_id, event.event_id, event.source,
            text=event.text, version=2, project=event.session.project,
            correlation_id=event.correlation_id, generation=gen,
            route_generation=message['route_generation'], endpoint_generation=message['endpoint_generation'],
            attempt_id=message['attempt_id'], turn_id=event.turn_id))
    return project_managed(projection)


def dispatch_managed(projection, *, home_dir=None, cwd=None):
    """One bounded foreground pump. Never replay dispatching/unknown envelopes."""
    import json
    import os
    import session_router
    bus = managed_bus()
    bus.recover()
    message = bus.message(projection['mailbox_id'])
    if message['state'] != 'queued':
        return project_managed(projection)
    endpoint = bus.endpoint(message['target'])
    def guard():
        from pathlib import Path
        from message_bus import IdentityError
        target = projection['target']
        if session_router.is_reference(target):
            row, _ = session_router.preflight(target)
            if ((row['kind'], row['identity'], row['project']) !=
                    (endpoint['kind'], endpoint['session_id'], endpoint['project'])):
                raise IdentityError('projection cannot redirect its immutable mailbox endpoint')
        elif target != endpoint['session_id']:
            raise IdentityError('projection native identity mismatch')
        task = Path(message['task_id'])
        if task.is_absolute():
            state = runner.read(task/'state.json')
            contract = runner.read(task/'contract.json')
            import coordination
            stale_handoff = False
            try:
                coordination.validate_envelope(task, state, contract, message)
            except (ValueError, KeyError, TypeError):
                stale_handoff = True
            if (stale_handoff or (task/'STOP').exists() or (task/'user-closure.json').exists()
                    or state['status'] == 'stopped'
                    or state['status'] == 'completed' and not message['kind'].startswith('followup')
                    or state['contract_hash'] != message['contract_hash']
                    or runner.digest(contract) != message['contract_hash']
                    or execution_state.generation(task, state) != json.loads(message['generation'])):
                sender_ep = bus.endpoint(message['sender'])
                sender = host_endpoint(bus, kind=sender_ep['kind'], native_id=sender_ep['session_id'],
                    project=sender_ep['project'], source=sender_ep['source'])
                bus.cancel(sender, message['id'], source='workflow STOP, closure, contract or execution changed')
                raise ValueError('managed dispatch authority is stopped or stale')
    guard()
    credential = host_endpoint(bus, kind=endpoint['kind'], native_id=endpoint['session_id'],
                               project=endpoint['project'], source=endpoint['source'])
    path = bus.path.parent/'private'/(credential.endpoint_id+'.owner.json')
    with primitives.locked(path.with_suffix('.lock')):
        owner = None
        if path.exists():
            saved = json.loads(path.read_text())
            try:
                bus.renew_endpoint(credential, saved['token'])
                owner = saved['token']
            except ValueError:
                pass
        if owner is None:
            try:
                owner = bus.acquire_endpoint(credential, owner='canonical-host', lease_seconds=30)
            except ValueError:
                return project_managed(projection)  # Another session host owns this endpoint.
            _private_save(path, dict(token=owner))

        class RouterTransport:
            def __init__(self):
                if session_router.is_reference(projection['target']):
                    _, adapter = session_router.preflight(projection['target'])
                else:
                    adapter = session_router._legacy_adapter()
                self.capabilities = getattr(adapter, 'capabilities', frozenset())

            def send(self, endpoint, envelope):
                # The legacy send signature is preserved. Neutral adapters get
                # the complete correlation envelope rather than just body text.
                if not session_router.is_reference(projection['target']):
                    key = envelope.get('canonical_id', '')
                    if envelope['body'].startswith('[ctask-workflow-completed id=') and key.startswith('followup:result-'):
                        return session_router.send_completion(projection['target'], envelope['body'], key, envelope=envelope, endpoint=endpoint)
                    return session_router.send(projection['target'], envelope['body'], home_dir=home_dir, cwd=cwd)
                _, adapter = session_router.preflight(projection['target'])
                return adapter.send(endpoint, envelope)

        dispatch_one(bus, credential, owner, {endpoint['kind']: RouterTransport()},
                     message_id=message['id'], guard=guard)
    return project_managed(projection)


class ExecutionOwnership:
    """Foreground execution lease cooperating with the caller's repository lock."""
    def __init__(self, bus, resource, token, fence, generation):
        self.bus, self.resource = bus, resource
        self.token, self.fence, self.generation = token, fence, generation

    def renew(self):
        self.bus.renew_execution(self.resource, self.token, self.fence,
                                 generation=self.generation)

    def advance(self, generation):
        if generation != self.generation:
            self.fence = self.bus.advance_execution(self.resource, self.token, self.fence,
                generation=self.generation, next_generation=generation)
            self.generation = generation

    def release(self):
        self.bus.release_execution(self.resource, self.token, self.fence,
                                   generation=self.generation)
