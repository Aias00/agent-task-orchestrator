"""Protocol subprocess fixture ONLY. Native qualification never invokes this file."""
import json
import os
import re
from pathlib import Path
import sys


def option(key):
    return sys.argv[sys.argv.index(key)+1]


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


pi = '--mode' in sys.argv
session = option('--session-id') if '--session-id' in sys.argv else option('--resume')
if pi:
    transcript = Path(option('--session-dir'))/(session+'.jsonl')
else:
    transcript = Path(os.environ['CLAUDE_CONFIG_DIR'])/'projects/fixture'/(session+'.jsonl')
transcript.parent.mkdir(parents=True, exist_ok=True)
turns = len(transcript.read_text().splitlines()) if transcript.exists() else 0
for line in sys.stdin.buffer:
    row = json.loads(line)
    if pi and row['type'] == 'get_state':
        emit(dict(type='response', id=row['id'], command='get_state', success=True,
                  data=dict(sessionId='wrong' if os.environ.get('CTASK_FIXTURE_BAD_ID') else session,
                            sessionFile=str(transcript))))
        continue
    prompt = row['message'] if pi else row['message']['content']
    envelope = json.loads(prompt)
    behavior = envelope['body']
    if behavior == 'crash':
        sys.exit(7)
    if behavior == 'malformed':
        print('not JSON', flush=True)
        continue
    if behavior == 'timeout':
        continue
    if pi:
        emit(dict(type='response', command='prompt', id='wrong' if behavior == 'wrong' else row['id'],
                  success=True, data=dict(disposition='started')))
    else:
        emit(dict(type='user', uuid='wrong' if behavior == 'wrong' else row['uuid'],
                  session_id=session))
    if behavior == 'busy':
        continue
    turns += 1
    with transcript.open('a') as handle:
        handle.write(json.dumps(dict(sessionId=session, cwd=os.getcwd(), turns=turns))+'\n')
    response = json.dumps(dict(message_id=envelope['id'], turns=turns, text='reply\u2028intact'))
    if behavior.startswith('fixture-question:'):
        response = json.dumps(dict(ctask=dict(kind='question', question=behavior.split(':', 1)[1],
                                              recommendation='Preserve existing scope')))
    if 'fixture-native-needs-input' in behavior:
        response = json.dumps(dict(ctask=dict(kind='needs_input', question='Keep compatibility?',
                                              recommendation='Keep the frozen requirement')))
    if 'fixture-native-auto-accept' in behavior:
        match = re.search(r'\[ctask-user-answer id=(question-[0-9a-f]{20})\]', behavior)
        if match:
            response = json.dumps(dict(ctask=dict(kind='accept_answer', question_id=match[1])))
    if 'fixture-native-auto-delegation' in behavior and '[ctask-delegation' in behavior:
        response = json.dumps(dict(ctask=dict(kind='accept_delegation')))
    if pi:
        emit(dict(type='message_end', message=dict(role='assistant', content=[dict(type='text', text=response)],
                                                   stopReason='error' if behavior == 'failed' else 'stop')))
        emit(dict(type='agent_settled', aborted=False))
    else:
        emit(dict(type='result', session_id=session, result=response,
                  subtype='error' if behavior == 'failed' else 'success', is_error=behavior == 'failed'))
