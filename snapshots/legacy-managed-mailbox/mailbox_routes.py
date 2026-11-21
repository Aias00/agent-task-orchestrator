"""Trusted local workflow routing over the shared mailbox/transport APIs.

Legacy JSON is an independent permission/decision ledger. This module adds no
business authority, scheduler, credentials from user auth, or automatic retries.
Managed endpoint capabilities are host-only files, never model prompt material.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import uuid

import runner as core
from message_bus import MessageBus, EndpointCredential, ConflictError
from transport import dispatch_one, receive_event


def home():
    return Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))).resolve()


def directory():
    p = home()/'ctask-interaction/mailbox'
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def bus():
    return MessageBus(directory()/'messages.sqlite')


def _key(reference):
    return core.digest(reference)


def _path(reference):
    return directory()/(_key(reference)+'.json')


def _save(path, record):
    # Capabilities are separate from message bodies and public JSON ledgers.
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.chmod(tmp, 0o600)
        json.dump(record, f)
        f.flush(); os.fsync(f.fileno())
    tmp.replace(path)


def _record(reference):
    return core.read(_path(reference))


def credential(reference):
    return EndpointCredential(**_record(reference)['credential'])


def _register(reference, kind, session_id, project, source, **fields):
    import recovery
    with recovery.locked(directory()/'registry.lock'):
        if _path(reference).exists():
            old = _record(reference)
            ep = bus().endpoint(reference)
            if (ep['kind'], ep['session_id'], ep['project']) != (kind, session_id, str(Path(project).resolve())):
                raise ValueError('endpoint identity cannot be rebound')
            return old
        c = bus().register_endpoint(reference, kind=kind, session_id=session_id,
                                    project=project, source=source)
        row = dict(credential=asdict(c), **fields)
        _save(_path(reference), row)
        return row


def register_managed(*, kind, session_id, project, runtime, source, executable=None, model=None):
    """Explicit host registration of an isolated framework-owned native session.

    Returns a public reference; use actor_environment only in the managed host.
    Native CLI creation/readback is handled by the adapter when pump() is called.
    Tools are fixed to none here, which cannot widen existing file-only workers.
    """
    if kind not in ('claude', 'pi') or not source.strip():
        raise ValueError('managed registration requires claude/pi and verification source')
    session_id = str(uuid.UUID(session_id))
    reference = 'managed:'+kind+':'+session_id
    runtime = Path(runtime).resolve()
    # Never use a native user's config directory as an adapter runtime.
    if runtime == home() or runtime == Path.home() or runtime.exists() and any(runtime.iterdir()):
        raise ValueError('registration requires an empty isolated runtime directory')
    _register(reference, kind, session_id, str(Path(project).resolve(strict=True)), source,
              runtime=str(runtime), executable=executable, model=model)
    return reference


def identity(reference, *, project=None, require_active=True):
    """Exact registered managed ID or the legacy exact Codex session index."""
    if not reference:
        raise ValueError('missing origin identity')
    if reference.startswith('managed:'):
        ep = bus().endpoint(reference)
        c = credential(reference)
        if ep['kind'] not in ('claude', 'pi') or ep['session_id'] != c.session_id:
            raise ValueError('managed endpoint identity mismatch')
        if project is not None and ep['project'] != str(Path(project).resolve()):
            raise ValueError('managed origin belongs to a different project')
    else:
        import recovery
        reference = str(uuid.UUID(reference))
        path = Path(recovery.session_path(home(), reference))
        with path.open() as handle:
            meta = json.loads(handle.readline()).get('payload', {})
        if project is not None and meta.get('cwd') and Path(meta['cwd']).resolve() != Path(project).resolve():
            raise ValueError('Codex origin belongs to a different project')
        native_project = str(Path(meta.get('cwd') or home()).resolve())
        _register(reference, 'codex', reference, native_project,
                  'exact legacy Codex session index and metadata')
        ep = bus().endpoint(reference)
    if require_active and ep['state'] != 'active':
        raise ValueError('endpoint is paused or closed')
    return ep


def origin_reference(origin):
    # An explicit third-party ID never replaces the old Codex UUID field.
    return origin.get('origin_endpoint_id') or origin.get('origin_thread_id')


def verify_origin(origin, project=None, *, require_active=True):
    if origin.get('origin_endpoint_id') and origin.get('origin_thread_id'):
        raise ValueError('managed origin cannot overwrite a legacy Codex binding')
    if project is not None and Path(origin['repo']).resolve() != Path(project).resolve():
        raise ValueError('origin repository binding changed')
    if Path(origin['codex_home']).resolve() != home():
        raise ValueError('workflow and hub must belong to the same local Codex home')
    return identity(origin_reference(origin), project=project, require_active=require_active)


def actor_environment(reference):
    identity(reference)
    c = credential(reference)
    return dict(CTASK_ENDPOINT_ID=reference, CTASK_ENDPOINT_TOKEN=c.token,
                CTASK_SESSION_ID=c.session_id, CTASK_PROJECT=c.project)


def current():
    return os.environ.get('CTASK_ENDPOINT_ID') or os.environ.get('CODEX_THREAD_ID')


def actor(reference):
    if reference.startswith('managed:'):
        c = credential(reference)
        expected = actor_environment(reference)
        import hmac
        if any(not hmac.compare_digest(os.environ.get(k, ''), v) for k, v in expected.items()):
            raise ValueError('operation requires its authenticated managed origin')
        return c
    if current() != reference:
        raise ValueError('this operation must run in its bound Codex conversation')


def offset(reference):
    ep = identity(reference)
    if ep['kind'] != 'codex':
        return 0
    import recovery
    return Path(recovery.session_path(home(), reference)).stat().st_size


def _service(namespace):
    reference = 'service:'+_key([str(home()), namespace])[:24]
    _register(reference, 'ctask', reference, str(home()), 'local workflow notification host')
    return reference


def send(*, target, text, key, task, contract_hash, source, sender=None, allow_local=False,
         expires_at=None):
    """Durably enqueue before invoking an adapter. Native origins await pump().

    The caller must first validate its frozen workflow/generation/STOP and user
    source. Existing outbox claims remain authoritative; they are never imported
    as fresh sends. Cross-project routing is confined to this explicit binding.
    """
    ep = identity(target)
    sender = sender or _service(key.split(':')[0])
    if not sender.startswith('service:'):
        identity(sender)
    b = bus()
    route = b.bind_route(sender=sender, target=target, task_id=str(task),
                       contract_hash=contract_hash, source=source, cross_project=True)
    m = b.enqueue(credential(sender), binding_id=route['id'], target=target, kind=key.split(':')[0],
                  body=text, idempotency_key=key, task_id=str(task), contract_hash=contract_hash,
                  expires_at=expires_at)
    _guard(b, m, key)
    if m['state'] != 'queued':
        return dict(state={'delivered': 'queued', 'failed': 'send_failed'}.get(m['state'], m['state']),
                    mailbox_id=m['id'], message_id=m.get('external_id'), thread_id=target)
    if allow_local and current() == target:
        actor(target)
        state = 'local' if _deliver_local(b, ep, m) else 'queued'
        m = b.message(m['id'])
    elif ep['kind'] == 'codex':
        _dispatch_codex(target)
        m = b.message(m['id'])
        state = {'delivered': 'queued', 'failed': 'send_failed'}.get(m['state'], m['state'])
    else:
        state = m['state']  # Stored, not a claim that a model was awakened.
    return dict(state=state, mailbox_id=m['id'], message_id=m.get('external_id'),
                thread_id=target, endpoint_id=target, created_at=m['created_at'])


def _dispatch_codex(reference):
    import recovery
    from message_adapters import CodexQueueAdapter
    b = bus(); c = credential(reference)
    try:
        owner = b.acquire_endpoint(c, owner='codex-queue-host', lease_seconds=60)
    except ConflictError:
        return
    try:
        class WorkflowCodex:
            def send(self, endpoint, message):
                state_path = Path(message['task_id'])/'state.json'
                cwd = core.read(state_path)['repo'] if state_path.is_file() else None
                adapter = CodexQueueAdapter(home=home(), verifier=lambda h, t, p: recovery.session_path(h, t), cwd=cwd)
                return adapter.send(endpoint, message)
        m = dispatch_one(b, c, owner, {'codex': WorkflowCodex()}, lease_seconds=60)
        if m and m['state'] in ('delivered', 'delivery_unknown'):
            b.queued_natively(c, m['id'])
        return m
    finally:
        b.release_endpoint(c, owner)


def pump(reference, *, provider_env=None):
    """One foreground owned native turn, no scheduler or implicit resend.

    Provider settings are supplied explicitly by a trusted isolated test/host,
    never persisted here. Restart receipts are replayed by exact message ID.
    """
    ep = identity(reference)
    if ep['kind'] == 'codex':
        _check_pending(reference, include_notifications=False)
        return _dispatch_codex(reference)
    _check_pending(reference)
    from message_adapters import ClaudeAdapter, PiAdapter
    if ep['kind'] not in ('claude', 'pi'):
        raise ValueError('pump requires a registered managed native endpoint')
    row = _record(reference); b = bus(); c = credential(reference)
    owner = b.acquire_endpoint(c, owner='managed-host', lease_seconds=120)
    adapter = (ClaudeAdapter if ep['kind'] == 'claude' else PiAdapter)(
        endpoint_id=reference, session_id=ep['session_id'], project=ep['project'],
        runtime=row['runtime'], executable=row.get('executable'), model=row.get('model'),
        provider_env=provider_env, tools='none')
    try:
        for event in adapter.recorded_events():
            _receive_native_event(b, c, event)
        with b._transaction() as con:
            queued = con.execute("select 1 from messages where target=? and state='queued'", (reference,)).fetchone()
        if not queued:
            return None
        with adapter:
            m = dispatch_one(b, c, owner, {ep['kind']: adapter}, lease_seconds=120)
            if m:
                events = adapter.events()
                _check_pending(reference)
                for event in events:
                    _receive_native_event(b, c, event)
            return b.message(m['id']) if m else None
    finally:
        adapter.close()
        b.release_endpoint(c, owner)



def response(message_id):
    """Only an exact accepted/native reply; arbitrary transcript text is excluded."""
    m = bus().message(message_id)
    if m['reply_id'] and m['state'] == 'replied':
        return bus().message(m['reply_id'])
    return None


def acknowledge(delivery, *, complete=False):
    if not delivery or not delivery.get('mailbox_id'):
        return  # Old JSON remains valid without fabricating mailbox evidence.
    b = bus(); m = b.message(delivery['mailbox_id']); c = credential(m['target'])
    if m['state'] in ('dispatching', 'delivered', 'delivery_unknown', 'accepted'):
        b.accept(c, m['id'], receipt_id='workflow-accept:'+m['id'])
        if complete:
            b.complete(c, m['id'], receipt_id='workflow-complete:'+m['id'])


def invalidate(task, *, source, kind=None):
    """Fence queued and late native events on a stopped/changed workflow."""
    b = bus()
    with b._transaction() as con:
        rows = con.execute("select id,sender from messages where task_id=? and state not in "
                           "('completed','replied','cancelled','expired','failed') and (? is null or kind=?)",
                           (str(task), kind, kind)).fetchall()
    for row in rows:
        b.cancel(credential(row['sender']), row['id'], source=source)



def _guard(b, m, key):
    import recovery
    state_path = Path(m['task_id'])/'state.json'
    state = core.read(state_path) if state_path.is_file() else None
    guard = dict(generation=recovery.generation(state_path.parent, state) if state else None,
                 origin=origin_reference(core.read(state_path.parent/'origin.json')) if state else None,
                 notification=key.startswith('control:'), key=key)
    with b._transaction() as con:
        con.execute('create table if not exists workflow_guards(message_id text primary key, body text not null)')
        con.execute('insert or ignore into workflow_guards values(?,?)', (m['id'], json.dumps(guard)))


def _check_pending(reference, *, include_notifications=True):
    """Revalidate canonical authority before dispatch AND before applying replies.

    Missing guard after an enqueue/metadata crash fails closed. Late events still
    enter receipts through the bus but cannot revive the cancelled message.
    """
    import recovery
    b = bus()
    with b._transaction() as con:
        con.execute('create table if not exists workflow_guards(message_id text primary key, body text not null)')
        rows = con.execute("select m.id,g.body from messages m left join workflow_guards g on g.message_id=m.id "
                           "where m.target=? and m.state not in ('replied','completed','cancelled','expired','failed')",
                           (reference,)).fetchall()
    for row in rows:
        m = b.message(row['id']); valid = False
        try:
            guard = json.loads(row['body'])
            if guard['notification'] and not include_notifications:
                continue
            d = Path(m['task_id']); s = core.read(d/'state.json'); c = core.read(d/'contract.json')
            origin = core.read(d/'origin.json')
            verify_origin(origin, s['repo'])
            valid = (not (d/'STOP').exists() and s['status'] not in ('stopped', 'paused')
                and core.digest(c) == s['contract_hash'] == m['contract_hash']
                and guard['generation'] == recovery.generation(d, s)
                and guard['origin'] == origin_reference(origin)
                and (guard['notification'] or s['status'] != 'completed'))
        except (ValueError, OSError, KeyError, TypeError):
            pass
        if valid and guard['key'].startswith('recovery:'):
            try:
                r = core.read(d/'recovery.json')
                request = r['requests'][-1]
                valid = (r['enabled'] and request['id'] == guard['key'].split(':', 1)[1]
                         and request['state'] in ('queued', 'working', 'dispatching')
                         and core.now() < request['expires_at'])
            except (OSError, ValueError, KeyError, IndexError):
                valid = False
        if not valid:
            b.cancel(credential(m['sender']), m['id'], source='workflow dispatch guard invalidated')
    b.recover()


def actor_credential(reference, supplied):
    """Trusted host event capability, separate from model-generated question text."""
    identity(reference)
    if not isinstance(supplied, EndpointCredential) or supplied.endpoint_id != reference:
        raise ValueError('managed event capability mismatch')
    b = bus()
    with b._transaction() as con:
        b._authenticate(con, supplied)


def _route_native_request(event, supplied):
    if event.kind != 'reply':
        return
    try:
        payload = json.loads(event.text)
    except (ValueError, TypeError):
        return
    request = payload.get('ctask') if isinstance(payload, dict) else None
    if not isinstance(request, dict) or request.get('kind') not in ('question', 'needs_input', 'blocked', 'accept_answer', 'accept_delegation'):
        return  # Model text can never impersonate a sourced user answer or grant.
    import interaction
    import recovery
    b = bus(); m = b.message(event.message_id)
    if m['state'] != 'replied':
        return  # Cancelled/expired events are evidence only.
    try:
        d, s, _, origin = interaction.binding(m['task_id'])
    except (ValueError, OSError, KeyError):
        return  # Historical replay does not reopen a stopped/stale question.
    if origin != event.endpoint_id or m['contract_hash'] != s['contract_hash']:
        raise ValueError('native question origin or contract mismatch')
    with b._transaction() as con:
        guard = json.loads(con.execute('select body from workflow_guards where message_id=?', (m['id'],)).fetchone()[0])
    if guard['generation'] != recovery.generation(d, s):
        return
    if request['kind'] == 'accept_answer':
        q = core.read(interaction.question_path(request.get('question_id', '')))
        if (q['workflow'] != str(d) or q['origin_thread'] != origin
                or q.get('answer_delivery', {}).get('mailbox_id') != m['id']):
            raise ValueError('native acceptance belongs to a different user answer')
        if q['state'] != 'accepted':
            interaction.accept(q['id'], credential=supplied)
        return
    if request['kind'] == 'accept_delegation':
        import delegation
        record = core.read(d/'delegation.json')
        dispatched = core.read(Path(record['directory'])/'delegation.json')
        if dispatched.get('delivery', {}).get('mailbox_id') != m['id']:
            raise ValueError('native acceptance belongs to a different delegation')
        if dispatched['state'] in ('queued', 'delivery_unknown'):
            delegation.accept(record['directory'], credential=supplied)
        return
    text = request.get('question') or request.get('note')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('native request needs a concrete question or blocker')
    rpath = d/'recovery.json'
    r = core.read(rpath) if rpath.exists() else None
    latest = r['requests'][-1] if r and r['requests'] else None
    if latest and latest.get('delivery', {}).get('mailbox_id') == m['id']:
        if latest['state'] in ('queued', 'working'):
            recovery.takeover(d, latest['id'], 'blocked' if request['kind'] == 'blocked' else 'needs_input',
                text, recommendation=request.get('recommendation', ''), credential=supplied)
        return
    if request['kind'] in ('question', 'needs_input'):
        interaction.ask(d, text, request.get('recommendation', ''), request.get('context', ''),
                        credential=supplied)


def _receive_native_event(b, supplied, event):
    result = receive_event(b, supplied, event)
    if event.kind == 'reply' and result:
        b.project_reply(credential(result['target']), result['id'])
    _route_native_request(event, supplied)


def _deliver_local(b, endpoint, message):
    """A live bound host can receive locally only in the endpoint's FIFO slot."""
    from transport import DeliveryResult
    c = credential(endpoint['id'])
    try:
        owner = b.acquire_endpoint(c, owner='local-host')
    except ConflictError:
        return False
    try:
        with b._transaction() as con:
            first = con.execute("select id from messages where target=? and state='queued' order by seq limit 1",
                                (endpoint['id'],)).fetchone()
        if not first or first['id'] != message['id']:
            return False
        class Local:
            def send(self, endpoint, message):
                return DeliveryResult('delivered', message['id'])
        sent = dispatch_one(b, c, owner, {endpoint['kind']: Local()})
        if sent is None:
            return False
        b.accept(c, sent['id'], receipt_id='local:'+sent['id'])
        b.complete(c, sent['id'], receipt_id='local-complete:'+sent['id'])
        return True
    finally:
        b.release_endpoint(c, owner)
