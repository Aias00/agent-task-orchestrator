#!/usr/bin/env python3
"""Pi RPC wire fixture. It replays the recorded native auth stop reason."""
import json
from pathlib import Path
import sys

model = {'provider': 'offline', 'id': 'fixture', 'name': 'fixture'}
replay = Path(__file__).parent/'pi-error-end-turn.json'

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    row = json.loads(line)
    kind = row['type']
    data = {'get_state': {'model': model, 'thinkingLevel': 'off'},
            'get_available_models': {'models': [model]},
            'get_available_thinking_levels': {'levels': ['off']},
            'get_commands': {'commands': []}, 'get_session_stats': {}}.get(kind, {})
    emit(dict(type='response', command=kind, id=row.get('id'), success=True, data=data))
    if kind == 'prompt':
        native = json.loads(replay.read_text())
        success = 'fixture-pi-success' in row.get('message', '')
        output = dict(status='complete', summary='fixture completion', next_step='', blocker='', retry_after_seconds=1)
        emit(dict(type='agent_start'))
        if success:
            tool = dict(id='one-write', name='write', arguments=dict(path='result', content='fixture output'))
            emit(dict(type='message_update', assistantMessageEvent=dict(type='toolcall_start', toolCall=tool)))
            emit(dict(type='tool_execution_start', toolCallId='one-write', toolName='write', args=tool['arguments']))
            Path('result').write_text('fixture output')
            emit(dict(type='tool_execution_end', toolCallId='one-write', result={}, isError=False))
        emit(dict(type='message_update', assistantMessageEvent=dict(type='text_delta', delta=json.dumps(output))))
        emit(dict(type='message_end', message=dict(role='assistant',
                  stopReason='stop' if success else native['actual_native_first_stop_reason'],
                  content=[dict(type='text', text=json.dumps(output))])))
        emit(dict(type='agent_end'))
        emit(dict(type='agent_settled'))
