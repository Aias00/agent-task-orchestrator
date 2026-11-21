"""Trusted local CLI identities and scoped actor credentials.

This passive endpoint uses the existing mailbox, with no service, subprocess,
native configuration or model. Its transport turn is mailbox admission, never a
human reading, answering or approving anything. Trusted CLI provisioning is a
local OS-user boundary; it is not isolation from a malicious same-user process.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

import agent_registry as registry

KIND = 'local_cli'
SOURCE = 'private local CLI mailbox admission; not human receipt or approval'
HUB_BINDING = 'local-cli-hub-binding-v2'
MAX_FILE = 65536


def private_read(path):
    """Read a bounded private regular file without following a final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > MAX_FILE):
            raise ValueError('actor file must be a bounded private regular file owned by this user')
        return json.loads(stream.read(MAX_FILE + 1))


def private_create(path, value):
    """Do not replace an existing credential, including symlinks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def storage():
    import interaction
    path = interaction.directory()/'local-cli'
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('local CLI identity directory must be private')
    return path


def _id_path(identifier, prefix):
    if not isinstance(identifier, str) or not re.fullmatch(prefix + r'-[0-9a-f]{32}', identifier):
        raise ValueError('invalid local CLI identity or credential')
    return storage()/(identifier+'.json')


def identity_record(native_id, project):
    row = private_read(_id_path(native_id, 'cli'))
    identity = registry.SessionIdentity.from_mapping(row['identity'])
    if (identity.adapter != KIND or identity.native_id != native_id
            or identity.project != str(Path(project).resolve()) or row['role'] not in ('origin', 'hub')):
        raise ValueError('local CLI session identity or project mismatch')
    return row


def load_actor_file(path):
    """Explicit file argument supplies proof; environment values are only locators."""
    row = private_read(path)
    if row.get('version') != 2 or not isinstance(row.get('proof'), dict):
        raise ValueError('invalid actor credential file')
    proof = registry.ActorProof(**row['proof'])
    import session_router
    if not session_router.is_reference(row.get('reference')):
        raise ValueError('actor file requires an opaque session reference')
    return proof, row['reference'], row.get('workflow_id')


def _credential(reference, role, source, output, binding=None):
    identifier, token = 'credential-'+uuid.uuid4().hex, secrets.token_urlsafe(32)
    private_create(_id_path(identifier, 'credential'), dict(version=2, reference=reference,
        role=role, source=source, token_hash=hashlib.sha256(token.encode()).hexdigest(), binding=binding))
    private_create(output, dict(version=2, reference=reference,
        workflow_id=binding['workflow_id'] if binding else None,
        proof=dict(method='local-cli-token', credential_ref=identifier, challenge=token)))


def _verified_credential(reference, proof):
    if not isinstance(proof, registry.ActorProof) or proof.method != 'local-cli-token':
        raise ValueError('local CLI requires its explicit private actor proof')
    row = private_read(_id_path(proof.credential_ref, 'credential'))
    if (row['reference'] != reference or not secrets.compare_digest(row['token_hash'],
            hashlib.sha256(proof.challenge.encode()).hexdigest())):
        raise ValueError('local CLI actor credential mismatch')
    return row


def create(project, role, source, credential_file, user_credential_file=None):
    """Trusted host provisioning, never exposed as an ACP/model action."""
    import session_router
    if role not in ('hub', 'origin') or not isinstance(source, str) or not source.strip():
        raise ValueError('local CLI creation requires origin/hub role and actual instruction source')
    if (role == 'hub') != bool(user_credential_file):
        raise ValueError('a hub requires a separate private user credential file')
    outputs = [Path(credential_file)] + ([Path(user_credential_file)] if user_credential_file else [])
    if len({str(p.resolve()) for p in outputs}) != len(outputs) or any(p.exists() or p.is_symlink() for p in outputs):
        raise ValueError('credential output paths must be distinct and unused')
    project = Path(project).resolve(strict=True)
    if not project.is_dir():
        raise ValueError('local CLI project must be a directory')
    identity = registry.SessionIdentity(KIND, 'cli-'+uuid.uuid4().hex, str(project), source)
    private_create(_id_path(identity.native_id, 'cli'), dict(version=2,
        identity=identity.to_mapping(), role=role, source=source, created_at=time.time()))
    session = session_router.register_v2(identity)
    _credential(session.reference, role, source, credential_file)
    if user_credential_file:
        _credential(session.reference, 'user', source, user_credential_file)
    return dict(reference=session.reference, role=role, project=str(project),
        credential_file=str(Path(credential_file).resolve()),
        user_credential_file=str(Path(user_credential_file).resolve()) if user_credential_file else None)


def grant(credential_file, workflow, output, source):
    """Issue an immutable workflow/contract/execution-scoped proof from a root key."""
    import interaction
    import recovery
    import session_router
    if not isinstance(source, str) or not source.strip():
        raise ValueError('actor grant requires an actual authority source')
    proof, reference, _ = load_actor_file(credential_file)
    root = _verified_credential(reference, proof)
    if root.get('binding') is not None:
        raise ValueError('a scoped grant cannot mint or renew actor credentials')
    session, _ = session_router.resolve_v2(reference)
    d, state, _, origin = interaction.binding(workflow)
    _check_role_binding(reference, root['role'], origin)
    if session.identity.project != state['repo']:
        raise ValueError('actor grant project mismatch')
    _credential(reference, root['role'], source, output, dict(workflow_id=str(d),
        contract_hash=state['contract_hash'], generation=state.get('execution_revision', 0),
        execution=recovery.generation(d, state)))
    return dict(reference=reference, role=root['role'], workflow_id=str(d),
        generation=state.get('execution_revision', 0), credential_file=str(Path(output).resolve()))


def _check_role_binding(reference, role, origin):
    import interaction
    if role == 'origin':
        if reference != origin:
            raise ValueError('origin actor must match the exact workflow origin')
    elif role in ('hub', 'user'):
        hub = interaction.config()
        if not hub or hub['thread_id'] != reference:
            raise ValueError('user/hub actor must match the explicitly configured hub')
    else:
        raise ValueError('invalid local CLI role')


class LocalCLIAdapter:
    kind = KIND
    capabilities = frozenset({'session_verify', 'session_actor', 'transport'})
    session_capabilities_v2 = {
        name: dict(state='supported', evidence='private local CLI identity, role proof and canonical mailbox')
        for name in ('session.verify', 'session.transport', 'actor.authenticate')}
    session_capabilities_v2.update({name: dict(state='unsupported',
        evidence='passive CLI endpoint has no native agent process or native resume')
        for name in ('session.create', 'session.cancel', 'session.load_resume', 'session.observe')})
    session_capabilities_v2['session.activity'] = dict(state='unknown',
        evidence='no authoritative observation of a human or external origin activity')

    def verify_session(self, native_id, project):
        try:
            identity_record(native_id, project)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def is_actor(self, native_id, project):
        return False  # An environment locator or session registration is never proof.

    def authenticate_actor(self, identity, proof, *, role, workflow_id, contract_hash, generation):
        import interaction
        import recovery
        import session_router
        session = identity_record(identity.native_id, identity.project)
        credential = private_read(_id_path(proof.credential_ref, 'credential'))
        reference = credential['reference']
        resolved, _ = session_router.resolve_v2(reference, project=identity.project)
        if resolved.identity != identity:
            return None
        credential = _verified_credential(reference, proof)
        if (credential['role'] != role.value
                or (role.value if role.value != 'user' else 'hub') != session['role']):
            return None
        if (role == registry.ActorRole.HUB and credential['binding'] is None
                and workflow_id == 'hub-binding:'+identity.project
                and contract_hash == HUB_BINDING and generation == 0):
            return 'local-cli-hub:'+proof.credential_ref
        d, state, _, origin = interaction.binding(workflow_id)
        _check_role_binding(reference, role.value, origin)
        expected = dict(workflow_id=str(d), contract_hash=state['contract_hash'],
            generation=state.get('execution_revision', 0), execution=recovery.generation(d, state))
        if (credential['binding'] != expected or identity.project != state['repo']
                or generation != expected['generation'] or contract_hash != expected['contract_hash']):
            return None
        return 'local-cli-'+role.value+':'+proof.credential_ref

    def send(self, endpoint, message):
        """Admit the existing envelope; complete only this passive host operation."""
        import transport
        from transport import DeliveryResult, TransportEvent
        bus = transport.managed_bus()
        try:
            identity_record(endpoint['session_id'], endpoint['project'])
            row = bus.message(message['id'])
            actual = bus.endpoint(row['target'])
            if (any(actual[key] != endpoint.get(key) for key in ('id', 'kind', 'session_id', 'project'))
                    or actual['kind'] != self.kind or row['state'] != 'dispatching'
                    or row['protocol'] != 2 or message.get('protocol') != 2
                    or any(row[key] != message.get(key) for key in
                           ('id', 'canonical_id', 'correlation_id', 'generation', 'task_id', 'contract_hash',
                            'body', 'sender', 'target', 'binding_id', 'route_generation', 'endpoint_generation'))):
                raise ValueError('CLI mailbox envelope or endpoint mismatch')
            if Path(row['task_id']).is_absolute():
                import interaction
                import recovery
                d, state, _, _ = interaction.binding(row['task_id'])
                if (state['contract_hash'] != row['contract_hash']
                        or recovery.generation(d, state) != json.loads(row['generation'])
                        or state['repo'] != actual['project']):
                    raise ValueError('CLI mailbox workflow authority changed')
            credential = transport.host_endpoint(bus, kind=self.kind, native_id=actual['session_id'],
                project=actual['project'], source=actual['source'])
            turn = 'cli-admission-'+row['id']
            for kind in ('accepted', 'turn_completed'):
                transport.receive_event(bus, credential, TransportEvent(kind, row['id'], actual['id'],
                    actual['session_id'], turn+':'+kind, SOURCE, version=2, project=actual['project'],
                    correlation_id=row['correlation_id'], generation=json.loads(row['generation']),
                    route_generation=row['route_generation'], endpoint_generation=row['endpoint_generation'],
                    attempt_id=row['attempt_id'], turn_id=turn))
            return DeliveryResult('delivered', turn, SOURCE)
        except (ValueError, OSError, KeyError):
            # No second outbox and no replay, even if admission partially succeeded.
            return DeliveryResult('delivery_unknown')
