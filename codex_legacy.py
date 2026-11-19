"""Compatibility boundary for exact legacy Codex conversation identities.

No latest-session discovery, session creation, or permission changes occur here.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import uuid


def identity(value):
    return str(uuid.UUID(value))


def home():
    return Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))).resolve()


def current():
    return os.environ.get('CODEX_THREAD_ID')


def session_path(home_dir, thread):
    for db in sorted(Path(home_dir).glob('state_*.sqlite'), reverse=True):
        try:
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2) as con:
                row = con.execute('select rollout_path from threads where id=?', (thread,)).fetchone()
            if row:
                path = Path(row[0]).resolve()
                with path.open() as handle:
                    meta = json.loads(handle.readline())
                if meta.get('payload', {}).get('id') == thread:
                    return str(path)
        except (OSError, ValueError, sqlite3.Error):
            continue
    raise ValueError('original session cannot be verified in the configured Codex home')


def queue(home_dir, thread, text, *, cwd=None):
    result = subprocess.run([os.environ.get('CTASK_QUEUE_BIN', 'codex'), 'queue', '--thread', thread,
                             '--message', text], env=dict(os.environ, CODEX_HOME=str(home_dir)),
                            cwd=cwd, capture_output=True, text=True, timeout=25)
    match = re.search(r'Queued message ([0-9a-f-]+) for thread ([0-9a-f-]+)', result.stdout)
    accepted = bool(result.returncode == 0 and match and match[2] == thread)
    return result, match[1] if accepted else None


def create_session(repo, contract, name, identifier, persist):
    """Create and read back the legacy native session with its exact policy."""
    import appserver
    import interaction
    import recovery
    from agent_registry import CreatedSession
    repo = Path(repo).resolve()
    with appserver.Client(interaction.home()) as client:
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
        saved_path = recovery.session_path(interaction.home(), thread['id'])
        readback = client.request('thread/read', {'threadId': thread['id'], 'includeTurns': False})
        if readback['thread']['id'] != thread['id'] or Path(readback['thread']['cwd']).resolve() != repo:
            raise ValueError('conversation readback mismatch')
        persist(verified_rollout=saved_path)
        return CreatedSession(identity(thread['id']), str(repo), contract['sandbox'], 'never',
                              'Codex app-server thread readback and exact session index', saved_path)


def observe_recovery(rollout, request, now):
    path = Path(rollout)
    if not path.exists():
        return
    with path.open('rb') as handle:
        handle.seek(request.get('scan_offset', request['rollout_offset']))
        for _ in range(1000):
            line = handle.readline()
            if not line or not line.endswith(b'\n'):
                break
            request['scan_offset'] = handle.tell()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get('payload', {})
            if (e.get('type') == 'response_item' and p.get('role') == 'user'
                    and request['id'] in json.dumps(p, ensure_ascii=False)):
                request['seen_in_session'] = True
            if (e.get('type') == 'event_msg' and p.get('type') == 'user_message'
                    and request['id'] in p.get('message', '')):
                request['seen_in_session'] = True
            if request.get('seen_in_session'):
                if e.get('type') == 'response_item' and p.get('role') == 'assistant':
                    request.setdefault('responded_at', now())
                if e.get('type') == 'event_msg' and p.get('type') == 'task_complete':
                    request.setdefault('turn_completed_at', now())


def observe_reply(path, tracker, identifier, now):
    reply = None
    with Path(path).open('rb') as handle:
        handle.seek(tracker['offset'])
        for _ in range(1000):
            line = handle.readline(8*1024*1024+1)
            if not line or not line.endswith(b'\n'):
                break
            tracker['offset'] = handle.tell()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get('payload', {})
            if e.get('type') == 'response_item' and p.get('type') == 'message':
                text = '\n'.join(b.get('text', '') for b in p.get('content', []) if isinstance(b, dict))
                if p.get('role') == 'user' and identifier in text:
                    tracker['seen'] = True
                elif tracker.get('seen') and p.get('role') == 'assistant':
                    tracker['text'] = text[-6000:]
                continue
            if e.get('type') != 'event_msg':
                continue
            kind = p.get('type')
            if kind == 'task_started':
                tracker.update(turn=p.get('turn_id'), seen=False, text='')
            elif kind == 'user_message' and identifier in p.get('message', ''):
                tracker['seen'] = True
            elif tracker.get('seen') and kind == 'agent_message':
                tracker['text'] = p.get('message', '')[-6000:]
            elif tracker.get('seen') and kind == 'task_complete' and p.get('turn_id') == tracker.get('turn'):
                text = p.get('last_agent_message') or tracker.get('text')
                if text:
                    reply = dict(turn=tracker['turn'], text=text[-6000:], at=now())
                tracker['seen'] = False
    return reply
