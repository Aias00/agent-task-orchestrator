"""Durable workflow generation reads, independent of observation or delivery."""
import json
import os
import re

EVENT_BLOCK_BYTES = 64 * 1024
_LINE_BOUNDARY = re.compile(rb'\r\n|[\n\r\v\f\x1c-\x1e]|\xc2\x85|\xe2\x80[\xa8\xa9]')
ACTIVE = {'running', 'planning', 'verifying', 'waiting'}
REPAIR = {'blocked', 'error', 'cleanup_blocked', 'interrupted'}
REPORT = {'needs_input', 'limit', 'stage_limit', 'planning_limit', 'contract_changed'}
ALERT_STATES = {'stalled', 'blocked', 'delivery_unknown', 'send_failed', 'resume_failed'}


def generation(d, state, *, block_bytes=EVENT_BLOCK_BYTES):
    # A very fast resume can finish between watcher polls. The durable start event
    # still proves execution occurred, unlike a model's claim that it resumed.
    path = d/'events.jsonl'
    if path.exists():
        lines = (reversed(path.read_text().splitlines()) if path.stat().st_size <= block_bytes
                 else reverse_event_lines(path, block_bytes=block_bytes))
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get('event') == 'workflow_started' and 'time' in event:
                return [event['time'], state.get('execution_revision', 0)]
    return [None, state.get('execution_revision', 0)]


def reverse_event_lines(path, *, block_bytes=EVENT_BLOCK_BYTES):
    """Match str.splitlines, retaining incomplete lines/UTF-8 delimiters across blocks."""
    with path.open('rb') as stream:
        position = stream.seek(0, os.SEEK_END)
        pending = b''
        while position:
            size = min(block_bytes, position)
            position -= size
            stream.seek(position)
            parts = _LINE_BOUNDARY.split(stream.read(size) + pending)
            pending = parts[0]
            for line in reversed(parts[1:]):
                yield line.decode('utf-8')
        yield pending.decode('utf-8')
