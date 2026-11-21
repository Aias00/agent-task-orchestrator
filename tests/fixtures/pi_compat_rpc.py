#!/usr/bin/env python3
"""Synthetic native Pi RPC for the actual derived adapter; no model or tools."""
import json
import os
from pathlib import Path
import sys

if sys.argv[1:] != ['--mode', 'rpc', '--no-themes']:
    raise SystemExit(2)

state = Path(os.environ['CTASK_PI_ACP_STATE_DIR'])
state.mkdir(mode=0o700, parents=True, exist_ok=True)
model = dict(provider='synthetic', id='compat', name='compat')
identity = dict(sessionId='synthetic-native-compat', sessionFile=str(state/'native.jsonl'))
if os.environ.get('CTASK_FIXTURE_MISSING_ID'):
    identity = {}


def emit(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    row = json.loads(line)
    kind = row['type']
    data = {
        'get_state': dict(model=model, thinkingLevel='off', **identity),
        'get_available_models': dict(models=[model]),
        'get_available_thinking_levels': dict(levels=['off']),
        'get_commands': dict(commands=[]),
        'get_session_stats': {},
    }.get(kind, {})
    emit(dict(type='response', command=kind, id=row.get('id'), success=True, data=data))
    if kind == 'prompt':
        with (state/'admissions.jsonl').open('a') as handle:
            handle.write(json.dumps(dict(message=row.get('message', ''), session=identity))+'\n')
        emit(dict(type='agent_start'))
        if 'error-then-stop' in row.get('message', ''):
            emit(dict(type='message_end', message=dict(role='assistant', stopReason='error', content=[])))
        emit(dict(type='message_update', assistantMessageEvent=dict(type='text_delta', delta='synthetic answer')))
        emit(dict(type='message_end', message=dict(role='assistant', stopReason='stop',
            content=[dict(type='text', text='synthetic answer')])))
        emit(dict(type='agent_end'))
        emit(dict(type='agent_settled'))
