"""Trusted Codex session implementation. Native formats never escape this boundary.

The legacy public module is a forwarding shim. All indexes are read-only,
all lookups use exact IDs, and native receipt/rollout parsing lives here.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import uuid
import time

MAX_LINE = 8 * 1024 * 1024
MAX_ACTIVITY_BYTES = 16 * 1024 * 1024


def identity(value):
    return str(uuid.UUID(value))


def home():
    import ato_config
    return ato_config.codex_home()


def current():
    return os.environ.get('CODEX_THREAD_ID')


def session_path(home_dir, thread):
    thread = identity(thread)
    for db in sorted(Path(home_dir).glob('state_*.sqlite'), reverse=True):
        try:
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2) as con:
                row = con.execute('select rollout_path from threads where id=?', (thread,)).fetchone()
            if row:
                path = Path(row[0]).resolve()
                with path.open('rb') as handle:
                    meta = json.loads(handle.readline(MAX_LINE + 1))
                if isinstance(meta, dict) and meta.get('payload', {}).get('id') == thread:
                    return str(path)
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            continue
    raise ValueError('original session cannot be verified in the configured Codex home')


def queue(home_dir, thread, text, *, cwd=None):
    return _queue(home_dir, thread, text, cwd=cwd, binary=os.environ.get('CTASK_QUEUE_BIN', 'codex'))


def _queue(home_dir, thread, text, *, cwd=None, binary):
    thread = identity(thread)
    result = subprocess.run([binary, 'queue', '--thread', thread,
                             '--message', text], env=dict(os.environ, CODEX_HOME=str(home_dir)),
                            cwd=cwd, capture_output=True, text=True, timeout=25)
    receipts = re.findall(r'Queued message ([0-9a-f-]+) for thread ([0-9a-f-]+)(?=[.\s]|$)', result.stdout)
    receipt = None
    if result.returncode == 0 and len(receipts) == 1:
        message_id, target = receipts[0]
        try:
            if identity(message_id) == message_id and identity(target) == target == thread:
                receipt = message_id
        except ValueError:
            pass
    return result, receipt


def create_session(repo, contract, name, identifier, persist):
    """Create and read back the legacy native session with its exact policy."""
    import appserver
    import interaction
    from agent_registry import CreatedSession
    repo = Path(repo).resolve()
    with appserver.Client(home()) as client:
        execution_config = {'sandbox_workspace_write.writable_roots': [
            str(repo/'.codex/tasks'), str(interaction.directory())]}
        grant = contract.get('permissions', {})
        if 'network_access' in grant:
            execution_config['sandbox_workspace_write.network_access'] = grant['network_access']
        execution_config['sandbox_workspace_write.writable_roots'] += [
            str(Path(p).resolve(strict=True)) for p in grant.get('writable_roots', [])]
        response = client.request('thread/start', dict(cwd=str(repo), sandbox=contract['sandbox'],
            approvalPolicy='never', ephemeral=False, threadSource='ctask-coordinator/'+identifier,
            config=execution_config, developerInstructions=
            'This is a durable coordinating conversation created by an explicit user delegation. '
            'Preserve the supplied full workflow objective and authorization. Do not treat yourself as a stage worker. '
            'User decisions belong in the configured ctask hub. Do not create another coordinator or parallel scheduler.'))
        thread = response['thread']
        persist(state='thread_created', origin_thread=thread['id'], thread_path=thread.get('path'),
                effective_sandbox=response.get('sandbox'), effective_approval=response.get('approvalPolicy'))
        if Path(thread['cwd']).resolve() != repo:
            raise ValueError('created conversation has the wrong working directory')
        expected = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite',
                    'danger-full-access': 'dangerFullAccess'}[contract['sandbox']]
        if response.get('sandbox', {}).get('type') != expected or response.get('approvalPolicy') != 'never':
            raise ValueError('created conversation did not preserve requested execution policy')
        client.request('thread/name/set', {'threadId': thread['id'], 'name': name})
        saved_path = session_path(home(), thread['id'])
        readback = client.request('thread/read', {'threadId': thread['id'], 'includeTurns': False})
        if readback['thread']['id'] != thread['id'] or Path(readback['thread']['cwd']).resolve() != repo:
            raise ValueError('conversation readback mismatch')
        persist(verified_rollout=saved_path)
        return CreatedSession(identity(thread['id']), str(repo), contract['sandbox'], 'never',
                              'Codex app-server thread readback and exact session index', saved_path)


def _events(path, offset):
    """Bound each read; partial or oversized records cannot advance a cursor."""
    if type(offset) is not int or offset < 0:
        raise ValueError('invalid native observation offset')
    with Path(path).open('rb') as handle:
        if offset > handle.seek(0, 2):
            raise ValueError('native observation file was truncated')
        handle.seek(offset)
        for _ in range(1000):
            line = handle.readline(MAX_LINE + 1)
            if not line or len(line) > MAX_LINE or not line.endswith(b'\n'):
                break
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get('payload', {}), dict):
                    event = {}
            except (ValueError, UnicodeError):
                event = {}
            yield handle.tell(), event


def _text(payload):
    return '\n'.join(block.get('text', '') for block in payload.get('content', [])
                     if isinstance(block, dict) and isinstance(block.get('text', ''), str))


def _correlates(identifier, text):
    # A longer request ID containing this ID is a different message.
    return isinstance(text, str) and bool(re.search(
        r'(?<![A-Za-z0-9_-])' + re.escape(identifier) + r'(?![A-Za-z0-9_-])', text))


def observe_recovery(rollout, request, now):
    if not Path(rollout).exists():
        return
    for offset, event in _events(rollout, request.get('scan_offset', request['rollout_offset'])):
        request['scan_offset'] = offset
        if not event:
            request['seen_in_session'] = False
            continue
        payload = event.get('payload', {})
        kind = payload.get('type')
        if event.get('type') == 'event_msg' and kind == 'task_started':
            request.update(seen_in_session=False, observed_turn=payload.get('turn_id'))
        if ((event.get('type') == 'response_item' and payload.get('role') == 'user'
             and _correlates(request['id'], _text(payload))) or
            (event.get('type') == 'event_msg' and kind == 'user_message'
             and _correlates(request['id'], payload.get('message', '')))):
            request['seen_in_session'] = True
        if not request.get('seen_in_session'):
            continue
        if ((event.get('type') == 'response_item' and payload.get('role') == 'assistant')
                or (event.get('type') == 'event_msg' and kind == 'agent_message')):
            request.setdefault('responded_at', now())
        if (event.get('type') == 'event_msg' and kind == 'task_complete'
                and request.get('observed_turn') and payload.get('turn_id') == request['observed_turn']):
            request.setdefault('turn_completed_at', now())
            request['seen_in_session'] = False
        elif (event.get('type') == 'event_msg' and kind == 'task_complete'
                and request.get('legacy_diagnostic_completion') is True
                and not request.get('observed_turn') and not payload.get('turn_id')):
            # Compatibility for old controller-only diagnostic projections.
            # Native v2 events still require an exact turn in observe_session.
            request.setdefault('turn_completed_at', now())
            request['seen_in_session'] = False


def observe_reply(path, tracker, identifier, now):
    reply = None
    for offset, event in _events(path, tracker['offset']):
        tracker['offset'] = offset
        if not event:
            tracker['seen'] = False
            continue
        payload = event.get('payload', {})
        if event.get('type') == 'response_item' and payload.get('type') == 'message':
            text = _text(payload)
            if payload.get('role') == 'user' and _correlates(identifier, text):
                tracker['seen'] = True
            elif tracker.get('seen') and payload.get('role') == 'assistant':
                tracker['text'] = text[-6000:]
            continue
        if event.get('type') != 'event_msg':
            continue
        kind = payload.get('type')
        if kind == 'task_started':
            tracker.update(turn=payload.get('turn_id'), seen=False, text='')
        elif kind == 'user_message' and _correlates(identifier, payload.get('message', '')):
            tracker['seen'] = True
        elif tracker.get('seen') and kind == 'agent_message':
            tracker['text'] = payload.get('message', '')[-6000:]
        elif (tracker.get('seen') and tracker.get('turn') and kind == 'task_complete'
              and payload.get('turn_id') == tracker['turn']):
            text = payload.get('last_agent_message') or tracker.get('text')
            if text:
                reply = dict(turn=tracker['turn'], text=text[-6000:], at=now())
            tracker['seen'] = False
    return reply


def activity(path):
    """Bounded backwards lifecycle lookup. Missing or malformed evidence is unknown."""
    with Path(path).open('rb') as stream:
        position = stream.seek(0, 2)
        remaining = MAX_ACTIVITY_BYTES
        suffix = b''
        while position and remaining:
            size = min(position, remaining, 262144)
            position -= size
            remaining -= size
            stream.seek(position)
            data = stream.read(size) + suffix
            # A partially written lifecycle line is not authoritative.
            if not suffix and not data.endswith(b'\n'):
                return 'unknown'
            lines = data.splitlines()
            suffix = lines.pop(0) if position and lines else b''
            if len(suffix) > MAX_LINE:
                return 'unknown'
            for line in reversed(lines):
                try:
                    event = json.loads(line)
                    payload = event.get('payload', {})
                    if event.get('type') == 'event_msg':
                        kind = payload.get('type')
                        if kind == 'task_started':
                            return 'active'
                        if kind in ('task_complete', 'turn_aborted'):
                            return 'idle'
                except (ValueError, AttributeError, TypeError):
                    return 'unknown'
    return 'unknown'


class CodexSessionAdapter:
    """Code-owned session operations composed with the existing worker adapter.

    Legacy actor checks are compatibility checks only. CODEX_THREAD_ID is a
    locator, never a v2 actor credential; actor.authenticate remains unknown.
    """
    kind = 'codex'
    session_capabilities = frozenset({'session_verify', 'session_create', 'session_actor',
                                      'session_observe', 'transport', 'native_queue_serialization'})
    session_capabilities_v2 = {
        name: {'state': 'supported', 'evidence': evidence}
        for name, evidence in {
            'session.verify': 'exact canonical UUID/index/header and project verification',
            'session.create': 'app-server creation with exact sandbox, approval and project readback',
            'session.transport': 'exact legacy queue receipt, ambiguous attempts never replayed',
            'session.observe': 'bounded correlated native turn reader',
            'session.activity': 'bounded lifecycle evidence; absent evidence remains unknown',
        }.items()
    }
    session_capabilities_v2['session.load_resume'] = {
        'state': 'unsupported', 'evidence': 'no verified typed native load/resume implementation'}

    normalize_identity = staticmethod(identity)
    current_locator = staticmethod(current)
    native_home = staticmethod(home)
    session_path = staticmethod(session_path)
    create_session = staticmethod(create_session)
    observe_recovery = staticmethod(observe_recovery)
    observe_reply = staticmethod(observe_reply)

    def verify_target(self, target, home_dir=None):
        if identity(target) != target:
            raise ValueError('legacy Codex thread must be a canonical UUID')
        return session_path(home_dir or home(), target)

    def verify_session(self, native_id, project):
        try:
            path = self.verify_target(native_id)
            with Path(path).open('rb') as stream:
                meta = json.loads(stream.readline(MAX_LINE + 1))['payload']
            return bool(meta.get('cwd') and Path(meta['cwd']).resolve() == Path(project).resolve())
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def is_actor(self, native_id, project):
        return current() == native_id and self.verify_session(native_id, project)

    def check_legacy_actor(self, target):
        if current() != target:
            raise ValueError('this operation must run in its bound Codex conversation')
        self.verify_target(target)

    def bind_legacy_origin(self, origin, project=None, *, workflow=None):
        if Path(origin['codex_home']).resolve() != home():
            raise ValueError('workflow and hub must belong to the same local Codex home')
        native_id = identity(origin['origin_thread_id'])
        self.verify_target(native_id)
        if workflow is not None:
            import workspace_association
            if workspace_association.has_record(workflow):
                # A changed header/project must not switch a registered scope
                # back to the unpinned legacy branch and bypass revocation.
                self.verify_workspace_association(workflow, origin, project)
                return native_id
        # Old headers can lack cwd. Never upgrade that compatibility proof to
        # v2 project verification; reject a known mismatching project.
        if project is not None:
            with Path(session_path(home(), native_id)).open('rb') as stream:
                meta = json.loads(stream.readline(MAX_LINE + 1))['payload']
            if meta.get('cwd') and Path(meta['cwd']).resolve() != Path(project).resolve():
                # This observation-only compatibility proof never reaches
                # verify_session, is_actor, transport or v2 authentication.
                if workflow is None:
                    raise ValueError('session project mismatch')
                import workspace_association
                self.verify_workspace_association(workflow, origin, project)
        return native_id

    def workspace_origin_context(self, origin, hub):
        """Keep legacy Codex account/identity interpretation in this adapter."""
        import workspace_association as association
        if origin.get('origin_session_ref') or not origin.get('origin_thread_id'):
            raise ValueError('workspace associations support exact legacy origins only')
        native_id = identity(origin['origin_thread_id'])
        if native_id != origin['origin_thread_id']:
            raise ValueError('origin must be a canonical native identity')
        native_home = association.canonical(origin['codex_home'])
        if native_home != home():
            raise ValueError('workflow native home mismatch')
        if association.canonical(hub.get('codex_home')) != native_home:
            raise ValueError('hub native home mismatch')

    def workspace_identity(self, native_id):
        """Exact read-only index/header proof for an explicit legacy association.

        Unlike the older lookup, this proof requires a canonical, nonsymlink
        rollout inside the configured native home and an original cwd.
        """
        import hashlib
        import workspace_association as association
        native_id = identity(native_id)
        root = association.canonical(os.environ.get('CODEX_HOME', str(home())))
        if root != home():
            raise ValueError('native home mismatch')
        for db in sorted(root.glob('state_*.sqlite'), reverse=True):
            association.canonical(db)
            try:
                with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2) as con:
                    rows = con.execute('select rollout_path from threads where id=?', (native_id,)).fetchall()
            except sqlite3.Error:
                continue
            if not rows:
                continue
            if len(rows) != 1:
                raise ValueError('ambiguous native session index')
            path = association.canonical(rows[0][0])
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('native rollout must be inside configured home')
            with path.open('rb') as stream:
                header = stream.readline(MAX_LINE + 1)
            if len(header) > MAX_LINE or not header.endswith(b'\n'):
                raise ValueError('invalid native header')
            meta = json.loads(header)
            payload = meta.get('payload', {})
            if meta.get('type') != 'session_meta' or payload.get('id') != native_id:
                raise ValueError('native header identity mismatch')
            cwd = association.canonical(payload.get('cwd'))
            return dict(native_id=native_id, native_home=str(root), rollout=str(path),
                        header_sha256=hashlib.sha256(header).hexdigest(),
                        header_cwd=payload['cwd'], workspace=str(cwd))
        raise ValueError('original session cannot be verified in the configured Codex home')

    def observation_bindings(self, origin, hub):
        """Pin exact native identities without granting a cross-contract actor."""
        self.workspace_origin_context(origin, hub)
        return dict(origin=self.workspace_identity(origin['origin_thread_id']),
                    hub=self.workspace_identity(hub['thread_id']))

    def verify_workspace_association(self, workflow, origin, project):
        import workspace_association as association
        record = association.validated(workflow)
        proof = record['proof']
        import runner
        if proof['origin_sha256'] != association.sha(Path(workflow)/'origin.json') or (
                runner.read(Path(workflow)/'origin.json') != origin or proof['project'] != str(project)):
            raise ValueError('workspace association origin or project mismatch')
        return record

    def delivery_checkpoint(self, target, home_dir=None):
        path = Path(self.verify_target(target, home_dir))
        return dict(thread_id=target, rollout_offset=path.stat().st_size)

    def send_legacy(self, target, text, *, home_dir=None, cwd=None):
        from transport import DeliveryResult
        try:
            self.verify_target(target, home_dir)
        except (ValueError, OSError):
            return DeliveryResult('failed')  # No subprocess was started.
        try:
            result, receipt = queue(home_dir or home(), target, text, cwd=cwd)
            return DeliveryResult('delivered' if receipt else 'delivery_unknown', receipt,
                                  (result.stdout + result.stderr)[-4000:])
        except (FileNotFoundError, PermissionError):
            return DeliveryResult('failed')  # Popen did not execute the CLI.
        except (OSError, subprocess.SubprocessError):
            return DeliveryResult('delivery_unknown')

    def send_completion(self, target, text, claim_key, *, envelope=None, endpoint=None):
        import completion_notifications
        completion_notifications.validate_claim(claim_key, target, text, envelope=envelope, endpoint=endpoint)
        from transport import DeliveryResult
        self.verify_target(target)
        native = Path.home()/'.codex/packages/standalone/current/bin/codex'
        try:
            result, receipt = _queue(home(), target, text, binary=str(native))
            return DeliveryResult('delivered' if receipt else 'delivery_unknown', receipt,
                                  (result.stdout + result.stderr)[-4000:])
        except (FileNotFoundError, PermissionError):
            return DeliveryResult('failed')
        except (OSError, subprocess.SubprocessError):
            return DeliveryResult('delivery_unknown')

    def send(self, endpoint, message):
        from transport import DeliveryResult
        key = message.get('canonical_id', '')
        if message.get('body', '').startswith('[ctask-workflow-completed id=') and key.startswith('followup:result-'):
            return self.send_completion(endpoint['session_id'], message['body'], key, envelope=message, endpoint=endpoint)
        if not self.verify_session(endpoint['session_id'], endpoint['project']):
            return DeliveryResult('failed')
        return self.send_legacy(endpoint['session_id'], message['body'], cwd=endpoint['project'])

    def session_cursor(self, native_id, project):
        if not self.verify_session(native_id, project):
            raise ValueError('session identity or project mismatch')
        return dict(offset=Path(self.verify_target(native_id)).stat().st_size,
                    native_id=native_id, project=str(Path(project).resolve()))

    def observe_session(self, native_id, project, correlation_id, cursor):
        from agent_registry import AgentEvent, SessionRef
        if not self.verify_session(native_id, project) or not isinstance(cursor, dict):
            raise ValueError('session identity, project or cursor mismatch')
        if (cursor.get('native_id', native_id) != native_id
                or cursor.get('project', str(Path(project).resolve())) != str(Path(project).resolve())):
            raise ValueError('session cursor identity or project mismatch')
        tracker = dict(cursor)
        events = []
        session = SessionRef(self.kind, native_id, str(Path(project).resolve()))
        # One native record per decoder step preserves received/responded/complete
        # ordering, including several turns in a single observation batch.
        for offset, event in _events(self.verify_target(native_id), tracker['offset']):
            tracker['offset'] = offset
            if not event:
                tracker['seen'] = False
                continue
            payload = event.get('payload', {})
            kind = payload.get('type')
            if event.get('type') == 'event_msg' and kind == 'task_started':
                tracker.update(turn=payload.get('turn_id'), seen=False)
            text = (_text(payload) if event.get('type') == 'response_item'
                    else payload.get('message', ''))
            role = payload.get('role')
            if ((event.get('type') == 'event_msg' and kind == 'user_message')
                    or (event.get('type') == 'response_item' and role == 'user')):
                if _correlates(correlation_id, text) and tracker.get('turn'):
                    tracker['seen'] = True
                    events.append(AgentEvent('received', session, correlation_id, 'Codex native log',
                                             turn_id=tracker['turn']))
            elif tracker.get('seen') and ((event.get('type') == 'event_msg' and kind == 'agent_message')
                    or (event.get('type') == 'response_item' and role == 'assistant')):
                events.append(AgentEvent('responded', session, correlation_id, 'Codex native log',
                                         turn_id=tracker['turn'], text=text[-6000:]))
            elif (tracker.get('seen') and event.get('type') == 'event_msg' and kind == 'task_complete'
                    and tracker.get('turn') and payload.get('turn_id') == tracker['turn']):
                events.append(AgentEvent('completed', session, correlation_id, 'Codex native log',
                    turn_id=tracker['turn'], text=payload.get('last_agent_message', '')[-6000:]))
                tracker['seen'] = False
        return events, tracker

    def session_activity(self, session, *, generation):
        from agent_registry import SessionActivity
        if not self.verify_session(session.native_id, session.project):
            raise ValueError('session identity or project mismatch')
        return SessionActivity(session, activity(self.verify_target(session.native_id)),
                               'Codex native lifecycle', time.time(), generation)

    def observe_session_v2(self, session, correlation_id, generation, cursor):
        import hashlib
        from agent_registry import SessionEvent
        if not isinstance(cursor, dict):
            raise ValueError('session observation requires an explicit cursor')
        if cursor.get('generation', generation) != generation:
            raise ValueError('session cursor generation mismatch')
        if cursor.get('session', session.to_mapping()) != session.to_mapping():
            raise ValueError('session cursor identity mismatch')
        if cursor.get('correlation_id', correlation_id) != correlation_id:
            raise ValueError('session cursor correlation mismatch')
        events, next_cursor = self.observe_session(session.native_id, session.project, correlation_id, cursor)
        kinds = {'received': 'message_received', 'responded': 'response_completed',
                 'completed': 'turn_completed'}
        normalized = []
        for event in events:
            fingerprint = json.dumps([session.to_mapping(), correlation_id, generation,
                                      event.turn_id, event.kind, event.text], sort_keys=True)
            normalized.append(SessionEvent(hashlib.sha256(fingerprint.encode()).hexdigest(), session,
                correlation_id, generation, kinds[event.kind], event.source, time.time(),
                turn_id=event.turn_id, message_id=correlation_id, text=event.text))
        next_cursor.update(generation=generation, session=session.to_mapping(), correlation_id=correlation_id)
        return normalized, next_cursor

    def legacy_activity(self, target):
        try:
            return activity(self.verify_target(target))
        except (OSError, ValueError):
            return 'unknown'

    def legacy_failure_notice_compatible(self, target):
        # The legacy notice API predates lifecycle evidence. Verify its exact
        # local native binding, but do not claim idle or grant v2 recovery.
        self.verify_target(target)
        return True
