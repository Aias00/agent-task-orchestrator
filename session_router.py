"""Explicit, verified native session references for trusted registered adapters.

Legacy UUID compatibility dispatch is owned by this router and its trusted
adapter. This module never discovers sessions, loads plugins or grants tools.
"""
import os
import json
from pathlib import Path
import re
import uuid
import time
from collections.abc import Mapping

import agent_registry


def is_reference(value):
    return isinstance(value, str) and bool(re.fullmatch(r'agentref-[0-9a-f]{32}', value))


def _path(reference):
    if not is_reference(reference):
        raise ValueError('invalid agent session reference')
    import interaction
    return interaction.directory() / 'agent-sessions' / (reference + '.json')


def register(kind, identity, project, source):
    """Register only after the trusted adapter verifies the exact session/project."""
    import runner
    if not all(isinstance(x, str) and x.strip() for x in (kind, identity, source)):
        raise ValueError('session registration requires kind, identity and source')
    project = str(Path(project).resolve(strict=True))
    adapter = agent_registry.get(kind, 'session_verify')
    if not callable(getattr(adapter, 'verify_session', None)):
        raise ValueError('adapter has no session verification implementation')
    if adapter.verify_session(identity, project) is not True:
        raise ValueError('native session identity or project could not be verified')
    reference = 'agentref-' + uuid.uuid4().hex
    path = _path(reference)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    runner.save(path, dict(version=1, reference=reference, kind=kind, identity=identity,
                           project=project, source=source))
    return reference


def register_v2(identity):
    """Persist an opaque v2 reference after exact adapter verification.

    This is an additive entry point. ``register`` and its v1 on-disk shape stay
    available for callers that still consume ``kind/identity/project`` rows.
    """
    import runner
    if not isinstance(identity, agent_registry.SessionIdentity):
        raise ValueError('register_v2 requires a versioned session identity')
    project_path = Path(identity.project).resolve(strict=True)
    if not project_path.is_dir() or str(project_path) != identity.project:
        raise ValueError('v2 session project must be an existing canonical directory')
    adapter = agent_registry.get(identity.adapter)
    agent_registry.session_capabilities(identity.adapter).require('session.verify')
    if not callable(getattr(adapter, 'verify_session', None)) or adapter.verify_session(
            identity.native_id, identity.project) is not True:
        raise ValueError('native session identity or project could not be verified')
    reference = 'agentref-' + uuid.uuid4().hex
    path = _path(reference)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    runner.save(path, dict(version=2, reference=reference, identity=identity.to_mapping()))
    return agent_registry.SessionReference(reference, identity)


def _row_identity(row):
    """Read v1 or v2 stored rows through the shared adapter boundary."""
    if row.get('version') == 1:
        return agent_registry.SessionIdentity(row['kind'], row['identity'], row['project'],
                                              row.get('source') or 'legacy registered reference')
    if row.get('version') == 2:
        return agent_registry.SessionIdentity.from_mapping(row['identity'])
    raise ValueError('invalid stored session reference version')


def resolve_v2(reference, project=None, capability=None):
    """Resolve either stored format into the neutral v2 contract."""
    import runner
    row = runner.read(_path(reference))
    if row.get('reference') != reference:
        raise ValueError('invalid stored session reference')
    identity = _row_identity(row)
    if project is not None and identity.project != str(Path(project).resolve()):
        raise ValueError('session project mismatch')
    if capability:
        agent_registry.session_capabilities(identity.adapter).require(capability)
    verifier = agent_registry.get(identity.adapter)
    if (not callable(getattr(verifier, 'verify_session', None))
            or verifier.verify_session(identity.native_id, identity.project) is not True):
        raise ValueError('session verification failed')
    return agent_registry.SessionReference(reference, identity), verifier


def capability_contract(reference):
    """Return code-owned v2 capabilities for an explicitly registered session."""
    session, _ = resolve_v2(reference)
    return agent_registry.session_capabilities(session.identity.adapter)


def current_reference():
    """Resolve CTASK_SESSION_REF as a locator only; this does not authenticate an actor."""
    reference = os.environ.get('CTASK_SESSION_REF')
    if not reference:
        return None
    session, _ = resolve_v2(reference)
    return session


def authenticate_actor(reference, proof, *, role, project, workflow_id, contract_hash, generation):
    """Authenticate an actor against adapter-held local credentials and exact bindings.

    The proof is passed directly to trusted adapter code and is never persisted.
    Environment variables can supply a reference locator but cannot supply this
    proof or make the actor's role authoritative.
    """
    if not isinstance(proof, agent_registry.ActorProof):
        raise ValueError('actor authentication requires an explicit v2 credential proof')
    session, adapter = resolve_v2(reference, project=project)
    capabilities = agent_registry.session_capabilities(session.identity.adapter)
    capabilities.require('actor.authenticate')
    authenticate = getattr(adapter, 'authenticate_actor', None)
    if not callable(authenticate):
        raise agent_registry.CapabilityUnavailable(session.identity.adapter, 'actor.authenticate',
                                                   agent_registry.CapabilityState.UNKNOWN)
    try:
        role = agent_registry.ActorRole(role)
    except (TypeError, ValueError):
        raise ValueError('invalid actor role') from None
    if (not isinstance(workflow_id, str) or not workflow_id.strip()
            or not isinstance(contract_hash, str) or not contract_hash.strip()
            or type(generation) is not int or generation < 0):
        raise ValueError('actor authentication requires workflow, contract and generation bindings')
    principal = authenticate(session.identity, proof, role=role, workflow_id=workflow_id,
                             contract_hash=contract_hash, generation=generation)
    if not isinstance(principal, str) or not principal.strip():
        raise ValueError('adapter did not authenticate an actor principal')
    return agent_registry.ActorContext._from_verified_adapter(
        principal, role, session, session.identity.project, workflow_id, contract_hash,
        generation, proof.method)


def authorize_actor(actor, action, *, project, workflow_id, contract_hash, generation):
    return agent_registry.authorize_actor(actor, action, project=project, workflow_id=workflow_id,
                                          contract_hash=contract_hash, generation=generation)


def activity_v2(reference, *, generation):
    """Return authoritative activity or an explicit unknown result.

    Lack of a capability, an absent implementation, or malformed evidence can
    never be interpreted as an idle session.
    """
    session, adapter = resolve_v2(reference)
    if type(generation) is not int or generation < 0:
        raise ValueError('activity generation must be a nonnegative integer')
    capabilities = agent_registry.session_capabilities(session.identity.adapter)
    state = capabilities.state('session.activity')
    method = getattr(adapter, 'session_activity', None)
    if state != agent_registry.CapabilityState.SUPPORTED or not callable(method):
        source = 'capability_'+state.value
        return agent_registry.SessionActivity(session.identity, 'unknown', source, time.time(), generation)
    value = method(session.identity, generation=generation)
    if isinstance(value, Mapping):
        value = agent_registry.SessionActivity(session.identity, value.get('state'), value.get('source', ''),
            value.get('observed_at'), value.get('generation', generation), value.get('turn_id', ''),
            value.get('version', 2))
    if (not isinstance(value, agent_registry.SessionActivity)
            or value.session != session.identity or value.generation != generation):
        raise ValueError('adapter activity identity or generation mismatch')
    return value


def observe_v2(reference, correlation_id, *, generation, cursor):
    """Observe only v2 events correlated to this exact session and generation."""
    session, adapter = resolve_v2(reference)
    if not isinstance(correlation_id, str) or not correlation_id.strip():
        raise ValueError('event observation requires a correlation ID')
    if type(generation) is not int or generation < 0:
        raise ValueError('event generation must be a nonnegative integer')
    capabilities = agent_registry.session_capabilities(session.identity.adapter)
    capabilities.require('session.observe')
    method = getattr(adapter, 'observe_session_v2', None)
    if not callable(method):
        raise agent_registry.CapabilityUnavailable(session.identity.adapter, 'session.observe',
                                                   agent_registry.CapabilityState.UNKNOWN)
    events, next_cursor = method(session.identity, correlation_id, generation, cursor)
    if not isinstance(events, list):
        raise ValueError('adapter returned invalid v2 session events')
    try:
        json.dumps(next_cursor, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError('session cursor must be finite JSON data') from None
    for event in events:
        if (not isinstance(event, agent_registry.SessionEvent) or event.version != 2
                or event.session != session.identity or event.correlation_id != correlation_id
                or event.generation != generation or not event.source.strip()):
            raise ValueError('session event identity, correlation or generation mismatch')
    return events, next_cursor


def load_or_resume_v2(reference, checkpoint, *, actor, workflow_id, contract_hash,
                      generation, operation='resume'):
    """Use native load/resume only when code declares support; never guess or relabel."""
    if operation not in ('load', 'resume'):
        raise ValueError('operation must be load or resume')
    session, adapter = resolve_v2(reference)
    capabilities = agent_registry.session_capabilities(session.identity.adapter)
    capabilities.require('session.load_resume')
    authorize_actor(actor, 'session.resume_owned', project=session.identity.project,
                     workflow_id=workflow_id, contract_hash=contract_hash, generation=generation)
    if actor.session != session:
        raise ValueError('session recovery actor does not own the exact target session')
    method = getattr(adapter, 'load_or_resume_session', None)
    if not callable(method):
        raise agent_registry.CapabilityUnavailable(session.identity.adapter, 'session.load_resume',
                                                   agent_registry.CapabilityState.UNKNOWN)
    if not isinstance(checkpoint, Mapping):
        raise ValueError('resume checkpoint must be a mapping')
    def invoke():
        return method(session.identity, dict(checkpoint), operation=operation,
                      workflow_id=workflow_id, contract_hash=contract_hash, generation=generation)
    # Abstract authenticated adapter contracts remain usable without a task
    # directory. Actual workflow recovery additionally joins the existing flock
    # and durable execution fence shared with worker and origin host work.
    workflow_path = Path(workflow_id)
    if workflow_path.is_absolute():
        import recovery
        import runner
        state = runner.read(workflow_path/'state.json')
        lockfile = Path(state['repo'])/'.codex/tasks/.worker.lock'
        with recovery.locked(lockfile, blocking=False):
            state = runner.read(workflow_path/'state.json')
            contract = runner.read(workflow_path/'contract.json')
            if ((workflow_path/'STOP').exists() or (workflow_path/'user-closure.json').exists()
                    or state['status'] in ('completed', 'stopped')
                    or state['repo'] != session.identity.project
                    or state.get('execution_revision', 0) != generation
                    or state['contract_hash'] != contract_hash or runner.digest(contract) != contract_hash
                    or recovery.alive(state.get('owner_pid'))):
                raise ValueError('session host recovery requires the exact inactive, open workflow generation')
            if bind_origin(runner.read(workflow_path/'origin.json'), state['repo']) != reference:
                raise ValueError('session host recovery must target the exact bound workflow origin')
            activity = activity_v2(reference, generation=generation)
            if activity.state != agent_registry.ActivityState.IDLE:
                raise ValueError('session host recovery requires verified idle native activity')
            with recovery.execution_owner(workflow_path, state, role='session_host'):
                result = invoke()
    else:
        result = invoke()
    if not isinstance(result, agent_registry.SessionIdentity):
        raise ValueError('adapter must return a verified versioned session identity')
    if result.project != session.identity.project or result.adapter != session.identity.adapter:
        raise ValueError('resumed session changed adapter or project')
    if result.native_id != session.identity.native_id:
        raise ValueError('native resume changed identity; use explicit checkpoint reconstruction')
    if (not callable(getattr(adapter, 'verify_session', None))
            or adapter.verify_session(result.native_id, result.project) is not True):
        raise ValueError('adapter could not verify the resumed native session identity')
    return result


def resolve(reference, project=None, capability='session_verify'):
    import runner
    row = runner.read(_path(reference))
    if row.get('version') not in (1, 2) or row.get('reference') != reference:
        raise ValueError('invalid stored session reference')
    identity = _row_identity(row)
    if project is not None and identity.project != str(Path(project).resolve()):
        raise ValueError('session project mismatch')
    if row.get('version') == 2:
        v2_capability = {'session_verify': 'session.verify', 'session_create': 'session.create',
                         'transport': 'session.transport', 'session_observe': 'session.observe',
                         'session_actor': 'actor.authenticate'}.get(capability)
        if v2_capability:
            agent_registry.session_capabilities(identity.adapter).require(v2_capability)
            adapter = agent_registry.get(identity.adapter)
        else:
            adapter = agent_registry.get(identity.adapter, capability)
        verifier = agent_registry.get(identity.adapter)
    else:
        adapter = agent_registry.get(identity.adapter, capability)
        verifier = agent_registry.get(identity.adapter, 'session_verify')
    if not callable(getattr(verifier, 'verify_session', None)) or verifier.verify_session(
            identity.native_id, identity.project) is not True:
        raise ValueError('session verification failed')
    # Preserve the established row keys for v1 consumers while exposing the
    # normalized v2 identity alongside them.
    normalized = dict(row, kind=identity.adapter, identity=identity.native_id,
                      project=identity.project, source=identity.provenance,
                      identity_v2=identity.to_mapping())
    return normalized, adapter


def actor(reference):
    row, adapter = resolve(reference, capability='session_actor')
    if not callable(getattr(adapter, 'is_actor', None)) or adapter.is_actor(row['identity'], row['project']) is not True:
        raise ValueError('operation requires its verified bound session')


def current():
    reference = os.environ.get('CTASK_SESSION_REF')
    if reference:
        actor(reference)
    return reference


def preflight(reference):
    row, adapter = resolve(reference, capability='transport')
    if not callable(getattr(adapter, 'send', None)):
        raise ValueError('adapter has no session transport implementation')
    if ('session_observe' in adapter.capabilities and
            (not callable(getattr(adapter, 'session_cursor', None))
             or not callable(getattr(adapter, 'observe_session', None)))):
        raise ValueError('adapter has incomplete session observation implementation')
    return row, adapter


def send(reference, text, *, home_dir=None, cwd=None):
    if not is_reference(reference):
        return _legacy_adapter().send_legacy(reference, text, home_dir=home_dir, cwd=cwd)
    row, adapter = preflight(reference)
    from transport import DeliveryResult
    endpoint = dict(id=reference, kind=row['kind'], session_id=row['identity'], project=row['project'])
    result = adapter.send(endpoint, dict(body=text))
    if not isinstance(result, DeliveryResult):
        raise ValueError('invalid session transport result')
    return result


def send_completion(reference, text, claim_key, *, envelope=None, endpoint=None):
    # Managed destinations retain their existing mailbox route in control.send.
    if is_reference(reference):
        raise ValueError('legacy completion route requires the exact legacy destination')
    adapter = _legacy_adapter()
    if not callable(getattr(adapter, 'send_completion', None)):
        raise ValueError('adapter has no qualified completion delivery route')
    return adapter.send_completion(reference, text, claim_key, envelope=envelope, endpoint=endpoint)


def cursor(reference):
    row, adapter = resolve(reference, capability='session_observe')
    if row['version'] == 2 and callable(getattr(adapter, 'session_cursor_v2', None)):
        value = adapter.session_cursor_v2(agent_registry.SessionIdentity.from_mapping(row['identity_v2']))
        json.dumps(value, allow_nan=False)
        if value is None:
            raise ValueError('session observation requires a concrete cursor')
        return value
    if not callable(getattr(adapter, 'session_cursor', None)):
        raise ValueError('adapter has no session cursor implementation')
    value = adapter.session_cursor(row['identity'], row['project'])
    if value is None:
        raise ValueError('session observation requires a concrete cursor')
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError('session cursor must be finite JSON data') from None
    return value


def observe(reference, correlation_id, cursor):
    """Read only adapter-provided events for the exact registered session/turn."""
    row, adapter = resolve(reference, capability='session_observe')
    if not callable(getattr(adapter, 'observe_session', None)):
        raise ValueError('adapter has no session observation implementation')
    events, next_cursor = adapter.observe_session(row['identity'], row['project'], correlation_id, cursor)
    if not isinstance(events, list):
        raise ValueError('adapter returned invalid session events')
    for event in events:
        if (not isinstance(event, agent_registry.AgentEvent) or event.version != 1
                or event.session.kind != row['kind'] or event.session.identity != row['identity']
                or str(Path(event.session.project).resolve()) != row['project']
                or event.correlation_id != correlation_id or not event.source.strip()
                or event.kind not in ('received', 'responded', 'completed')
                or not isinstance(event.turn_id, str) or not isinstance(event.text, str)):
            raise ValueError('session event identity or correlation mismatch')
    return events, next_cursor


# Compatibility dispatch is centralized here; core callers never import a
# vendor module or interpret a native receipt. Existing UUID records retain
# their public identity and cursor fields instead of being silently rewritten.
def _legacy_adapter():
    import agents  # Trusted built-in registration, never task-controlled code.
    return agent_registry.get('codex')


def compatibility_identity(value):
    return _legacy_adapter().normalize_identity(value)


def compatibility_home():
    return _legacy_adapter().native_home()


def compatibility_path(home_dir, target):
    return _legacy_adapter().session_path(home_dir, target)


def current_compatibility_locator():
    """Legacy environment locator only; does not authenticate a v2 actor."""
    return _legacy_adapter().current_locator()


def current_actor_locator():
    return current() or current_compatibility_locator()


def check_actor(target):
    if is_reference(target):
        return actor(target)
    return _legacy_adapter().check_legacy_actor(target)


def bind_origin(origin, project):
    target = origin.get('origin_session_ref')
    if target:
        if origin.get('origin_thread_id'):
            raise ValueError('ambiguous origin identity')
        resolve(target, project=project)
        # Explicit checkpoint reconstruction preserves the original reference
        # and prior mailbox endpoints. Only new origin work follows a verified
        # successor; queued/unknown historical envelopes are never rebound.
        links = origin.get('session_reconstructions', [])
        if not isinstance(links, list) or len(links) > 16:
            raise ValueError('invalid origin reconstruction history')
        for link in links:
            if not isinstance(link, dict) or link.get('previous_reference') != target:
                raise ValueError('origin reconstruction chain mismatch')
            previous, adapter = resolve_v2(target, project=project)
            successor, successor_adapter = resolve_v2(link.get('successor_reference'), project=project)
            verify = getattr(adapter, 'verify_checkpoint_successor', None)
            if (adapter is not successor_adapter or not callable(verify)
                    or verify(previous, successor, link) is not True):
                raise ValueError('origin reconstruction successor could not be verified')
            target = successor.reference
        return target
    return _legacy_adapter().bind_legacy_origin(origin, project)


def verify_target(target, home_dir=None):
    if is_reference(target):
        return preflight(target)
    return _legacy_adapter().verify_target(target, home_dir)


def delivery_checkpoint(target, home_dir=None):
    """Return opaque, JSON-safe adapter cursor fields, preserving v1 records."""
    if not is_reference(target):
        return _legacy_adapter().delivery_checkpoint(target, home_dir)
    row, adapter = preflight(target)
    checkpoint = {'session_ref': target}
    if row['version'] == 2:
        # Passive authenticated CLI endpoints have no observable native agent
        # turn. Transport must not invent observation from mailbox admission.
        if agent_registry.session_capabilities(row['kind']).state('session.observe') == agent_registry.CapabilityState.SUPPORTED:
            checkpoint['event_cursor'] = cursor(target)
    elif 'session_observe' in adapter.capabilities:
        checkpoint['event_cursor'] = cursor(target)
    return checkpoint


def managed_delivery_supported(target):
    """Select one authority, preserving the public v1 submission-only API.

    Old third-party references promise send(), not managed native lifecycle.
    They cannot be silently upgraded to a v2 coordinator by registration alone.
    The verified native queue capability is sufficient for legacy submissions;
    otherwise only a v2 reference opts into fenced managed delivery.
    """
    if not is_reference(target):
        return True  # The trusted UUID bridge declares native queue serialization.
    row, adapter = resolve(target)
    return row['version'] == 2 or 'native_queue_serialization' in adapter.capabilities


def delivery_fields(result, receipt_limit=4000):
    """Project a normalized transport result into the legacy durable outboxes."""
    from transport import DeliveryResult
    if not isinstance(result, DeliveryResult):
        raise ValueError('invalid session transport result')
    fields = dict(state={'delivered': 'queued', 'failed': 'send_failed',
                         'delivery_unknown': 'delivery_unknown'}[result.state],
                  message_id=result.external_id)
    if result.receipt:
        fields['receipt'] = result.receipt[-receipt_limit:]
    return fields


def observe_recovery(record, request, now):
    target = record['thread_id']
    if not is_reference(target):
        expected = _legacy_adapter().verify_target(target, record.get('codex_home'))
        if str(Path(record['rollout']).resolve()) != expected:
            raise ValueError('native recovery checkpoint identity mismatch')
        return _legacy_adapter().observe_recovery(record['rollout'], request, now)
    row, _ = resolve(target, capability='session_observe')
    if row['version'] == 2:
        import transport
        generation = request['generation'][-1]
        events, next_cursor = observe_v2(target, request['id'], generation=generation,
                                        cursor=request.get('event_cursor'))
        delivery = request.get('delivery')
        if not delivery or delivery.get('authority') != 'managed_mailbox':
            raise ValueError('v2 recovery observation requires its canonical mailbox')
        projected = transport.observe_managed(delivery, events)
        request['delivery'] = projected
        request['event_cursor'] = next_cursor
        message = transport.managed_bus().message(projected['mailbox_id'])
        request['mailbox_state'] = message['state']
        # Only the durable authority may establish receipt/response evidence.
        # The recovery request's working/repaired disposition stays independent.
        if message['turn_id']:
            request.update(seen_in_session=True, observed_turn=message['turn_id'])
        if message['state'] in ('responded', 'turn_completed'):
            request.setdefault('responded_at', now())
        if message['state'] == 'turn_completed':
            request.setdefault('turn_completed_at', now())
        return
    events, next_cursor = observe(target, request['id'], request.get('event_cursor'))
    request['event_cursor'] = next_cursor
    for event in events:
        if event.kind == 'received' and event.turn_id:
            request.update(seen_in_session=True, observed_turn=event.turn_id)
        elif request.get('seen_in_session') and event.turn_id == request.get('observed_turn'):
            if event.kind == 'responded':
                request.setdefault('responded_at', now())
            elif event.kind == 'completed':
                request.setdefault('turn_completed_at', now())


def observe_reply(target, tracker, correlation_id, now, *, path=None, delivery=None):
    if not is_reference(target):
        native_path = _legacy_adapter().verify_target(target)
        if path is not None and str(Path(path).resolve()) != native_path:
            raise ValueError('native reply checkpoint identity mismatch')
        return _legacy_adapter().observe_reply(native_path, tracker, correlation_id, now)
    row, _ = resolve(target, capability='session_observe')
    if row['version'] == 2:
        import transport
        if not delivery or delivery.get('authority') != 'managed_mailbox':
            raise ValueError('v2 reply observation requires its canonical mailbox')
        envelope = transport.managed_bus().message(delivery['mailbox_id'])
        gen = json.loads(envelope['generation'])
        revision = gen[-1] if isinstance(gen, list) else gen
        events, next_cursor = observe_v2(target, correlation_id, generation=revision, cursor=tracker['cursor'])
        projected = transport.observe_managed(delivery, events)
        reply = None
        current = transport.managed_bus().message(projected['mailbox_id'])
        for event in events:
            if event.turn_id != current['turn_id']:
                continue
            if event.kind == 'response_completed' and current['state'] in ('responded', 'turn_completed'):
                tracker['text'] = event.text[-6000:]
            if event.kind == 'turn_completed' and current['state'] == 'turn_completed':
                text = event.text or tracker.get('text')
                if text:
                    reply = dict(turn=event.turn_id, text=text[-6000:], at=now())
        tracker['cursor'] = next_cursor
        return reply
    events, next_cursor = observe(target, correlation_id, tracker['cursor'])
    tracker['cursor'] = next_cursor
    reply = None
    for event in events:
        if event.kind == 'received' and event.turn_id:
            tracker.update(seen=True, turn=event.turn_id, text='')
        elif tracker.get('seen') and event.turn_id == tracker.get('turn'):
            if event.kind == 'responded':
                tracker['text'] = event.text[-6000:]
            elif event.kind == 'completed':
                text = event.text or tracker.get('text')
                if text:
                    reply = dict(turn=event.turn_id, text=text[-6000:], at=now())
                tracker['seen'] = False
    return reply


def activity(target, *, generation=0):
    """Never infer idle from the lack of observable activity."""
    if not is_reference(target):
        return _legacy_adapter().legacy_activity(target)
    return activity_v2(target, generation=generation).state.value


def legacy_failure_notice_compatible(target):
    """Preserve old terminal notices without upgrading legacy activity proof."""
    return not is_reference(target) and _legacy_adapter().legacy_failure_notice_compatible(target)


def created_reference(kind, created):
    """Keep established raw identities only for the code-owned legacy bridge."""
    if created.legacy_rollout:
        adapter = agent_registry.get(kind)
        if kind != 'codex' or not callable(getattr(adapter, 'verify_target', None)):
            raise ValueError('legacy Codex rollout is reserved for the Codex bridge')
        if adapter.verify_target(created.identity) != created.legacy_rollout:
            raise ValueError('created session checkpoint mismatch')
        return created.identity
    adapter = agent_registry.get(kind)
    if getattr(adapter, 'session_reference_version', 1) == 2:
        return register_v2(agent_registry.SessionIdentity(
            kind, created.identity, created.project, created.source)).reference
    return register(kind, created.identity, created.project, created.source)


def recovery_binding(origin, project):
    """Verify a recoverable origin without exposing native decoding to callers."""
    target = bind_origin(origin, project)
    if not is_reference(target):
        return target, _legacy_adapter().verify_target(target, origin['codex_home'])
    row, adapter = resolve(target, project=project, capability='session_observe')
    observer = 'observe_session_v2' if row['version'] == 2 else 'observe_session'
    if (not callable(getattr(adapter, observer, None))
            or not (callable(getattr(adapter, 'session_cursor', None))
                    or row['version'] == 2 and callable(getattr(adapter, 'session_cursor_v2', None)))):
        raise ValueError('adapter has no session observation implementation')
    preflight(target)
    return target, None


def creation_adapter(kind, contract):
    """Require the whole coordinator path before creating any native session."""
    adapter = agent_registry.get(kind, 'session_create')
    for capability, method in (('session_create', 'create_session'),
            ('session_verify', 'verify_session'), ('session_actor', 'is_actor'),
            ('session_observe', 'observe_session'), ('session_observe', 'session_cursor'),
            ('transport', 'send')):
        agent_registry.get(kind, capability)
        if not callable(getattr(adapter, method, None)):
            raise ValueError('adapter lacks required delegation method: '+method)
    if ((contract.get('permissions') or contract['sandbox'] == 'danger-full-access')
            and 'explicit_permissions' not in adapter.capabilities):
        raise ValueError('coordinator adapter cannot widen worker permissions')
    return adapter


def creation_fingerprint_parts(kind):
    """Keep historical default-adapter delegation IDs byte-for-byte stable."""
    return [] if kind == 'codex' else [kind]
