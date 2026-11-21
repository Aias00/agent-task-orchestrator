"""Offline ACP subprocess with native event scenarios, no provider access."""
import json
import sys
import time
import uuid
from pathlib import Path

def send(value):
    print(json.dumps(value), flush=True)

def update(sid, value):
    send(dict(jsonrpc='2.0', method='session/update', params=dict(sessionId=sid, update=value)))

for line in sys.stdin:
    request = json.loads(line)
    method, ident, params = request.get('method'), request.get('id'), request.get('params', {})
    if method == 'initialize':
        result = dict(protocolVersion=1, agentCapabilities={}, agentInfo=dict(name='offline-fixture', version='1'))
    elif method == 'session/new':
        result = dict(sessionId=uuid.uuid4().hex)
    elif method == 'session/prompt':
        sid = params['sessionId']
        turn = params['_meta']['ctaskTurn']
        text = params['prompt'][0]['text']
        with Path('fixture-admissions').open('a') as h:
            h.write(sid + '\n')
        admission = len(Path('fixture-admissions').read_text().splitlines())
        if 'fixture-timeout' in text:
            time.sleep(60)
        Path('result').write_text('fixture output')
        for i, state in enumerate(('pending', 'in_progress', 'completed')):
            update(sid, dict(sessionUpdate='tool_call' if i == 0 else 'tool_call_update',
                             toolCallId='one-write', status=state, **({'title': 'write', 'kind': 'edit'} if i == 0 else {})))
        output = dict(status='complete', summary='fixture done', next_step='', blocker='', retry_after_seconds=1)
        update(sid, dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=json.dumps(output))))
        reason = 'stop'
        if 'fixture-native-error' in text or ('fixture-success-then-error' in text and admission > 1):
            # Replay the actual Pi error/end_turn pair; neither the source nor its
            # evidence is modified, and the fixture never contacts a provider.
            replay = Path(__file__).parent/'pi-error-end-turn.json'
            record = json.loads(replay.read_bytes())
            reason = record['actual_native_first_stop_reason']
            assert record['raw_acp_first_stop_reason'] == 'end_turn'
        if 'fixture-missing-proof' not in text:
            receipt_sid = 'wrong-session' if 'fixture-wrong-session' in text else sid
            update(receipt_sid, dict(sessionUpdate='session_info_update', _meta=dict(
                ctaskNativeReceipt=dict(turn=('old-turn' if 'fixture-wrong-turn' in text else turn), stopReason=reason))))
        if 'fixture-late-error' in text:
            update(sid, dict(sessionUpdate='session_info_update', _meta=dict(
                ctaskNativeReceipt=dict(turn=turn, stopReason='error'))))
        result = dict(stopReason='end_turn')
    elif ident is None:
        continue
    else:
        send(dict(jsonrpc='2.0', id=ident, error=dict(code=-32601, message='unsupported')))
        continue
    send(dict(jsonrpc='2.0', id=ident, result=result))
