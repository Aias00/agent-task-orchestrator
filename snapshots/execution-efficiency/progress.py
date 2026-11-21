"""Deterministic, restartable observation of native CLI streams. Never invokes a model."""
import json


def initial():
    return dict(offset=0, counts={}, seen=[], tools={}, last_output=None, alerts=[])


def normalize(row):
    """Compatibility decoder for old process records lacking agent metadata."""
    import agent_native_events
    return agent_native_events.legacy(row)


def observe(path, state, now, digest, stream_idle=120, tool_idle=600, repeated=6, normalizer=None):
    """Incremental bounded JSONL read. Partial records are retried, never skipped."""
    if state['last_output'] is None:
        state['last_output'] = now
    if not path.exists():
        rows = []
    else:
        size = path.stat().st_size
        if size < state['offset']:
            # A replaced stream is a new observation, not evidence of old progress.
            state.update(initial())
            state['last_output'] = now
        rows = []
        with path.open('rb') as handle:
            handle.seek(state['offset'])
            for _ in range(1000):
                start = handle.tell()
                raw = handle.readline(8*1024*1024+1)
                if not raw or not raw.endswith(b'\n'):
                    break
                state['offset'] = handle.tell()
                state['last_output'] = now
                try:
                    row = json.loads(raw)
                    if isinstance(row, dict):
                        rows.append(row)
                except ValueError:
                    continue
    found = []
    for row in rows:
        for event, identity, name, args in (normalizer or normalize)(row):
            if not identity:
                continue
            identity = str(identity)
            if event == 'start' and identity not in state['seen']:
                # Hash arguments immediately: commands, paths and text need not be copied.
                signature = digest([name, args])
                state['seen'].append(identity)
                state['counts'][signature] = state['counts'].get(signature, 0)+1
                state['tools'][identity] = dict(name=name, at=now)
                if state['counts'][signature] >= repeated:
                    found.append(('repeated_tool_call', signature, name))
            elif event == 'end':
                state['tools'].pop(identity, None)
            elif event == 'progress' and identity in state['tools']:
                state['tools'][identity]['at'] = now
    if now-state['last_output'] >= stream_idle:
        found.append(('stream_idle', 'stream', None))
    for identity, tool in state['tools'].items():
        if now-tool['at'] >= tool_idle:
            found.append(('tool_idle', identity, tool['name']))
    alerts = []
    for kind, key, name in found:
        marker = kind+':'+key
        if marker not in state['alerts']:
            state['alerts'].append(marker)
            alerts.append(dict(kind=kind, key=key, tool=name))
    return alerts
