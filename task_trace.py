"""Bounded, metadata-only task timeline; telemetry never controls execution.

Only explicit enumerated fields are persisted. Event IDs are supplied by trusted
producers, and uniqueness is scoped to a run. SQLite transactions give atomic
ordering and restart-safe deduplication.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

MAX_EVENTS = 4096
MAX_QUERY = 4096
KINDS = frozenset({'attempt_started', 'transport_completed', 'transport_error',
    'native_success', 'native_error', 'native_unverified', 'tool', 'progress',
    'check_started', 'check_passed', 'check_failed', 'attempt_finished', 'output_truncated'})
STATES = frozenset({'pending', 'in_progress', 'completed', 'failed', 'cancelled', 'timeout'})
SOURCES = frozenset({'scheduler', 'acp', 'native'})

def opaque(value):
    """Represent untrusted identifiers without retaining possible secrets."""
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()[:24]

def target_path(value, workspace):
    if not isinstance(value, str) or not value or '\x00' in value:
        return None
    try:
        root = Path(workspace).resolve()
        p = Path(value)
        resolved = (p if p.is_absolute() else root / p).resolve()
        rel = resolved.relative_to(root)
        if any(part.startswith('.') for part in rel.parts):
            return None
        if any(re.search(r'(?i)(secret|token|password|credential|api[_-]?key|auth)', part) for part in rel.parts):
            return None
        # Reject suspicious names rather than risk persisting credentials.
        if not re.fullmatch(r'[A-Za-z0-9_./-]{1,240}', str(rel)):
            return None
        return str(rel)
    except (ValueError, OSError, RuntimeError):
        return None

def append(directory, kind, *, event_id, task=None, attempt=None, source='scheduler',
           session=None, turn=None, tool=None, state=None, target=None, workspace=None):
    """Best effort append. False includes I/O failure and must not affect worker."""
    if kind not in KINDS or source not in SOURCES or not event_id:
        return False
    event = {'kind': kind, 'source': source, 'at': time.time()}
    event['run'] = opaque(Path(directory).name)
    try:
        parent = json.loads((Path(directory) / 'parent.json').read_text())
        workflow = opaque(parent.get('workflow_id'))
        if workflow:
            event['workflow'] = workflow
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if isinstance(task, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task):
        event['task'] = task
    if type(attempt) is int and 0 < attempt < 1000000:
        event['attempt'] = attempt
    for name, value in [('native_session', session), ('turn', turn), ('tool_id', tool)]:
        if value:
            event[name] = opaque(value)
    if state in STATES:
        event['state'] = state
    if target and workspace:
        safe = target_path(target, workspace)
        if safe:
            event['target'] = safe
    try:
        path = Path(directory) / 'trace.sqlite3'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink():
                return False
        else:
            os.close(fd)
        db = sqlite3.connect(str(path), timeout=2)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, count INTEGER NOT NULL)')
            with db:
                if db.execute('SELECT 1 FROM events WHERE event_id=?', (opaque(event_id),)).fetchone():
                    return True
                if db.execute('SELECT count(*) FROM events').fetchone()[0] >= MAX_EVENTS:
                    db.execute("INSERT INTO counters VALUES ('dropped',1) ON CONFLICT(name) DO UPDATE SET count=count+1")
                    return False
                db.execute('INSERT INTO events(event_id,data) VALUES (?,?)',
                           (opaque(event_id), json.dumps(event, separators=(',', ':'))))
            return True
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError):
        return False

def query(directory, task=None):
    path = Path(directory) / 'trace.sqlite3'
    empty = {'events': [], 'summary': {'available': False, 'tool_call_count': 0, 'dropped': 0, 'truncated': False}}
    if path.is_symlink() or not path.is_file():
        return empty
    try:
        db = sqlite3.connect('file:' + str(path.resolve()) + '?mode=ro', uri=True, timeout=2)
        try:
            rows = db.execute('SELECT seq,data FROM events ORDER BY seq LIMIT ?', (MAX_QUERY + 1,)).fetchall()
            counter = db.execute("SELECT count FROM counters WHERE name='dropped'").fetchone()
        finally:
            db.close()
        truncated = len(rows) > MAX_QUERY
        events = []
        for seq, data in rows[:MAX_QUERY]:
            event = json.loads(data)
            event['sequence'] = seq
            if task is None or event.get('task') == task:
                events.append(event)
        tools = {(e.get('task'), e.get('attempt'), e.get('native_session'), e.get('turn'), e['tool_id'])
                 for e in events if e.get('kind') == 'tool' and e.get('tool_id')}
        latest = next((e for e in reversed(events) if e.get('attempt') and e.get('task')), None)
        current = ([e for e in events if e.get('task') == latest['task']
                    and e.get('attempt') == latest['attempt']] if latest else [])
        def last_kind(prefix):
            return next((e['kind'] for e in reversed(current) if e['kind'].startswith(prefix)), None)
        last_tool = next((e for e in reversed(current) if e['kind'] == 'tool'), None)
        tool_state = last_tool.get('state') if last_tool else None
        if last_tool and any(e.get('kind') == 'tool' and e.get('tool_id') == last_tool.get('tool_id')
                             and e.get('state') == 'failed' for e in current):
            tool_state = 'failed'
        return {'events': events, 'summary': {'available': True, 'tool_call_count': len(tools),
                'dropped': counter[0] if counter else 0,
                'truncated': truncated or any(e['kind'] == 'output_truncated' for e in events),
                'latest_task': latest.get('task') if latest else None,
                'latest_attempt': latest.get('attempt') if latest else None,
                'native': ('native_error' if any(e['kind'] == 'native_error' for e in current) else last_kind('native_')),
                'transport': ('transport_error' if any(e['kind'] == 'transport_error' for e in current) else last_kind('transport_')),
                'checks': last_kind('check_'), 'latest_tool_state': tool_state}}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return empty
