"""Canonical private material inbox over ATO's neutral managed mailbox.

No native transport, prefix convention, model wakeup or user notice is used.
Only verified origin/hub actors can submit/read/assign exact workflow material.
A local inbox read receipt is not user presentation or execution ownership.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

import ato_config
import agents  # Built-in adapter registration only; no native I/O.
import interaction
import recovery
import runner as core
import session_router
import transport

MAX_ARTIFACT = 65536
MAX_SUMMARY = 4096
MAX_ROWS = 256
INBOX_KIND = 'ato_private_inbox'
FORBIDDEN = frozenset({'.git', '.codex', '.aws', '.ssh', '.venv', 'node_modules', 'auth.json',
                       'credentials', 'credentials.json', '.env'})


def text(value, label, limit=512):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit or '\x00' in value:
        raise ValueError(label+' must be bounded nonempty text')
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def safe_path(value):
    # Preserve the supplied path before canonicalization so ancestor links fail.
    p = Path(os.path.abspath(value))
    if any(q.is_symlink() for q in (p, *p.parents)):
        raise ValueError('symlink in internal material path')
    return p


def artifact(project, value):
    root = safe_path(project).resolve(strict=True)
    raw = Path(value)
    if '..' in raw.parts:
        raise ValueError('artifact path escape')
    p = safe_path(raw if raw.is_absolute() else root/raw)
    if not p.is_relative_to(root) or p == root:
        raise ValueError('artifact outside exact project')
    relative = p.relative_to(root)
    if any(part in FORBIDDEN or part.startswith('.env.') for part in relative.parts):
        raise ValueError('private configuration is not report material')
    # Walk by descriptors; concurrent directory/symlink replacement cannot escape.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        item = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(item, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT:
                raise ValueError('artifact must be a bounded regular file')
            data = stream.read(MAX_ARTIFACT+1)
            if len(data) > MAX_ARTIFACT:
                raise ValueError('artifact too large')
    finally:
        os.close(fd)
    # UTF-8 text only: no binary/private database snapshots in the inbox.
    data.decode('utf-8')
    return dict(path=str(relative), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)), data


def bound(directory, role, proof=None, reference=None, operation='internal.inbox', require_open=False):
    if role not in ('origin', 'hub'):
        raise ValueError('internal coordination requires origin or hub')
    safe_path(directory)
    d, state, contract, origin = interaction.binding(directory, require_open=require_open)
    hub = (interaction.config() or {}).get('thread_id')
    if not hub:
        raise ValueError('verified hub binding required')
    target = origin if role == 'origin' else hub
    if reference is not None and reference != target:
        raise ValueError('wrong internal actor reference')
    if session_router.is_reference(target) and session_router.resolve(target)[0]['version'] == 2:
        verified = session_router.authenticate_actor(target, proof, role=role, project=state['repo'],
            workflow_id=str(d), contract_hash=state['contract_hash'], generation=state.get('execution_revision', 0))
        session_router.authorize_actor(verified, operation, project=state['repo'], workflow_id=str(d),
            contract_hash=state['contract_hash'], generation=state.get('execution_revision', 0))
        actor = verified.to_mapping()
    else:
        if session_router.current_actor_locator() != target:
            raise ValueError('internal operation requires its bound actor')
        interaction.actor(target)
        actor = dict(reference=target, role=role, authentication_source='existing native actor verifier')
    context = dict(workflow=str(d), project=state['repo'], contract_hash=state['contract_hash'],
                   generation=recovery.generation(d, state), origin=origin, hub=hub,
                   origin_hash=core.digest(core.read(d/'origin.json')))
    return d, state, contract, context, dict(reference=target, role=role, verified=actor)


def database(create=True):
    root = safe_path(ato_config.interaction_directory())
    path = safe_path(root/'mailbox'/'state.sqlite')
    if not create and not path.is_file():
        return None
    if not create:
        # No schema migration, WAL reconfiguration or creation on query/read.
        bus = transport.MessageBus.__new__(transport.MessageBus)
        bus.path, bus.clock = path, core.now
        with bus._transaction() as con:
            if not con.execute("select 1 from sqlite_master where name='internal_reports'").fetchone():
                return None
        return bus
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with recovery.locked(root/'internal-schema.lock'):
        bus = transport.managed_bus()
        with bus._transaction() as con:
            con.execute('''create table if not exists internal_reports(
                id text primary key, fingerprint text not null, body text not null,
                artifact blob not null, mailbox_id text not null unique,
                read_receipt text, assignment text, workflow text not null)''')
    return bus


def sealed(value):
    return dict(value, checksum=core.digest(value))


def valid_seal(value):
    return value.get('checksum') == core.digest({k: v for k, v in value.items() if k != 'checksum'})


def matches(report, context):
    return all(report.get(k) == v for k, v in context.items())


def validate_record(bus, row, context, *, current=True):
    body = json.loads(row['body'])
    if current and not matches(body, context):
        raise ValueError('stale report workflow/contract/generation/actor binding')
    data = bytes(row['artifact'])
    if (row['fingerprint'] != core.digest(body) or hashlib.sha256(data).hexdigest() != body['artifact']['sha256']
            or len(data) != body['artifact']['bytes']):
        raise ValueError('internal artifact or report tampered')
    envelope = bus.message(row['mailbox_id'])
    if (envelope['canonical_id'] != row['id'] or envelope['kind'] != 'internal_report'
            or envelope['task_id'] != body['workflow'] or envelope['contract_hash'] != body['contract_hash']
            or json.loads(envelope['generation']) != body['generation'] or json.loads(envelope['body']) != body):
        raise ValueError('internal mailbox binding tampered')
    sender = bus.endpoint(envelope['sender'])
    if (body.get('sender') != body['origin'] or body.get('recipient') != body['hub']
            or sender['kind'] != INBOX_KIND or sender['session_id'] != body['origin']
            or sender['project'] != body['project']):
        raise ValueError('internal sender identity changed')
    endpoint = bus.endpoint(envelope['target'])
    if (endpoint['kind'] != INBOX_KIND or endpoint['session_id'] != body['recipient']
            or endpoint['project'] != body['project']):
        raise ValueError('internal recipient identity changed')
    return body, envelope


def fetch(bus, identifier):
    if not isinstance(identifier, str) or not re.fullmatch('internal-[0-9a-f]{32}', identifier):
        raise ValueError('invalid internal request ID')
    with bus._transaction() as con:
        row = con.execute('select * from internal_reports where id=?', (identifier,)).fetchone()
    if row is None:
        raise ValueError('unknown internal report')
    return dict(row)


def submit(directory, request_id, artifact_path, summary, source, *, actor_proof=None, reference=None):
    """Exactly one atomic mailbox + artifact admission, with stable deduplication.

    Repeating an exact submission returns its existing receipt, including any
    uncertain dispatch barrier. It never dispatches, retries or backfills.
    """
    text(request_id, 'request ID', 128); text(summary, 'summary', MAX_SUMMARY); text(source, 'source')
    d, state, _, context, actor = bound(directory, 'origin', actor_proof, reference, 'internal.submit', True)
    session_router.verify_target(context['hub'])
    identity, data = artifact(state['repo'], artifact_path)
    identifier = 'internal-'+core.digest([context, request_id])[:32]
    report = dict(version=1, id=identifier, request_id=request_id, **context, source=source,
        summary=summary, artifact=identity, sender=actor['reference'], recipient=context['hub'],
        execution_grant=False, user_notice=dict(state='not_requested'), wakeup=dict(state='unsupported',
        reason='no qualified private automatic consumer; explicit authorized inbox read required'))
    bus = database()
    sender = transport.host_endpoint(bus, kind=INBOX_KIND, native_id=context['origin'],
        project=context['project'], source='verified internal origin identity')
    receiver = transport.host_endpoint(bus, kind=INBOX_KIND, native_id=context['hub'],
        project=context['project'], source='verified internal hub identity')
    route = bus.bind_route(sender=sender.endpoint_id, target=receiver.endpoint_id, task_id=str(d),
        contract_hash=state['contract_hash'], source='canonical private material route',
        generation=context['generation'], advance=True)
    # The mailbox's idempotency fingerprint, artifact and canonical report
    # commit in one transaction. A crash cannot leave an untracked submission.
    with recovery.locked(d.parent.parent/'.worker.lock'):
        _, fresh, _, fresh_context, _ = bound(d, 'origin', actor_proof, reference, 'internal.submit', True)
        if fresh_context != context:
            raise ValueError('workflow changed before internal admission')
        with bus._transaction() as con:
            old = con.execute('select * from internal_reports where id=?', (identifier,)).fetchone()
            if old:
                if old['fingerprint'] != core.digest(report) or bytes(old['artifact']) != data:
                    raise ValueError('request ID already submitted with different source or material')
            else:
                message = bus._enqueue(con, sender, binding_id=route['id'], target=receiver.endpoint_id,
                    kind='internal_report', body=encode(report), idempotency_key=identifier,
                    task_id=str(d), contract_hash=state['contract_hash'], correlation_id=identifier,
                    protocol=2, generation=context['generation'], canonical_id=identifier,
                    authority_source='canonical private material route')
                con.execute('insert into internal_reports values(?,?,?,?,?,?,?,?)',
                    (identifier, core.digest(report), encode(report), data, message['id'], None, None, str(d)))
    return projection(bus, fetch(bus, identifier), context)


def projection(bus, row, context):
    report, envelope = validate_record(bus, row, context, current=False)
    receipt = json.loads(row['read_receipt']) if row['read_receipt'] else None
    claim = json.loads(row['assignment']) if row['assignment'] else None
    for record in (receipt, claim):
        if record and (not valid_seal(record)
                or not matches(record, {k:report[k] for k in context})
                or record.get('artifact') != report['artifact']
                or record.get('actor', {}).get('reference') != report['hub']
                or record.get('actor', {}).get('role') != 'hub'):
            raise ValueError('internal receipt or assignment tampered')
    valid = matches(report, context)
    assignment_state = 'unassigned'
    if claim:
        import task_responsibility
        if not task_responsibility.finite_deadline(claim.get('expires_at'), claim.get('created_at'), 900):
            raise ValueError('invalid assignment deadline')
        assignment_state = ('stale' if not valid or not matches(claim, context) else
            'expired' if core.now() >= claim['expires_at'] else 'assigned')
    # The queued envelope is available for an explicit local reader. Native
    # delivery/unknown is separately visible and never inferred as read.
    return dict(report, mailbox_id=envelope['id'], submission_state=envelope['state'],
        inbox_state='read' if receipt else 'available', read_receipt=receipt,
        binding_state='current' if valid else 'stale', assignment=claim, assignment_state=assignment_state,
        handling_progress='unverified', actual_execution=False)


def inbox(directory, role='hub', *, actor_proof=None, reference=None):
    _, _, _, context, _ = bound(directory, role, actor_proof, reference)
    bus = database(False)
    if bus is None:
        return dict(reports=[], read_only=True, wakeup='unsupported')
    with bus._transaction() as con:
        # Filter before material exposure; no unrelated task or actor inbox.
        rows = con.execute('select * from internal_reports where workflow=? order by rowid desc limit ?',
                           (context['workflow'], MAX_ROWS)).fetchall()
    result = []
    for row in rows:
        body = json.loads(row['body'])
        if (body.get('workflow') != context['workflow'] or body.get('origin') != context['origin']
                or body.get('hub') != context['hub']):
            continue
        result.append(projection(bus, dict(row), context))
        if len(result) >= MAX_ROWS:
            break
    return dict(reports=result, read_only=True, wakeup='unsupported', receipt_created=False,
                limit=MAX_ROWS, newest_first=True)


def read_report(directory, identifier, source, role='hub', *, actor_proof=None, reference=None):
    text(source, 'read receipt source')
    d, state, _, context, actor = bound(directory, role, actor_proof, reference, 'internal.read')
    bus = database(False)
    if bus is None:
        raise ValueError('unknown internal report')
    with recovery.locked(ato_config.interaction_directory()/'internal.lock'):
        row = fetch(bus, identifier)
        report, _ = validate_record(bus, row, context)
        projection(bus, row, context)
        # Only the exact recipient may acknowledge reading. Origin may query
        # its submission, but cannot manufacture a maintainer read receipt.
        if actor['reference'] != report['recipient'] or role != 'hub':
            raise ValueError('read receipt requires the bound recipient hub actor')
        identity, _ = artifact(state['repo'], report['artifact']['path'])
        if identity != report['artifact']:
            raise ValueError('source artifact hash drift')
        receipt = dict(id='read-'+core.digest([identifier, actor['reference']])[:32],
            actor=actor, source=source, at=core.now(), **context, artifact=identity,
            meaning='explicit local inbox read; not user presentation or acceptance')
        _, _, _, fresh_context, _ = bound(d, role, actor_proof, reference, 'internal.read')
        if fresh_context != context:
            raise ValueError('workflow changed before internal read receipt')
        with bus._transaction() as con:
            if not row['read_receipt']:
                con.execute('update internal_reports set read_receipt=? where id=?', (encode(sealed(receipt)), identifier))
        result = projection(bus, fetch(bus, identifier), context)
        result['artifact_text'] = bytes(row['artifact']).decode('utf-8')
        return result


def assign(directory, identifier, next_action, source, seconds=300, *, actor_proof=None, reference=None):
    text(next_action, 'bounded next action'); text(source, 'assignment source')
    if type(seconds) is not int or not 1 <= seconds <= 900:
        raise ValueError('assignment must expire within 900 seconds')
    d, state, _, context, actor = bound(directory, 'hub', actor_proof, reference, 'internal.assign', True)
    bus = database(False)
    if bus is None:
        raise ValueError('unknown internal report')
    with recovery.locked(ato_config.interaction_directory()/'internal.lock'):
        row = fetch(bus, identifier)
        report, _ = validate_record(bus, row, context)
        projection(bus, row, context)
        if not row['read_receipt'] or json.loads(row['read_receipt'])['actor']['reference'] != actor['reference']:
            raise ValueError('assignment requires actual recipient read first')
        identity, _ = artifact(state['repo'], report['artifact']['path'])
        if identity != report['artifact']:
            raise ValueError('assignment artifact hash drift')
        proposal = dict(**context, actor=actor, next_action=next_action, source=source,
                        artifact=identity, seconds=seconds, execution_grant=False, report_id=identifier)
        _, _, _, fresh_context, _ = bound(d, 'hub', actor_proof, reference, 'internal.assign', True)
        if fresh_context != context:
            raise ValueError('workflow changed before internal assignment')
        if row['assignment']:
            old = json.loads(row['assignment'])
            if any(old.get(k) != v for k, v in proposal.items()):
                raise ValueError('assignment cannot renew or replace the original claim')
        else:
            now = core.now()
            claim = dict(proposal, created_at=now, expires_at=now+seconds)
            with bus._transaction() as con:
                con.execute('update internal_reports set assignment=? where id=?', (encode(sealed(claim)), identifier))
        return projection(bus, fetch(bus, identifier), context)
