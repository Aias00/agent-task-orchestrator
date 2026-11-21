"""Synthetic ACP subprocess, not Pi or a provider. All state is temporary."""
import json
import os
from pathlib import Path
import sys
import threading
import uuid

runtime = Path(sys.argv[1])
identity = 'fixture-native-'+uuid.uuid4().hex
lock = threading.Lock()
cancelled = threading.Event()


def send(value):
    with lock:
        print(json.dumps(dict(jsonrpc='2.0', **value)), flush=True)


def update(session, turn, value):
    value['_meta'] = dict(value.get('_meta', {}), ctaskTurn=turn)
    send(dict(method='session/update', params=dict(sessionId=session, update=value)))


def prompt(request):
    params, rid = request['params'], request['id']
    session, turn = params['sessionId'], params['_meta']['ctaskTurn']
    text = params['prompt'][0]['text']
    with (runtime/'admissions.jsonl').open('a') as f:
        f.write(json.dumps(dict(session=session, turn=turn))+'\n')
    if 'disconnect' in text:
        os._exit(23)
    if 'timeout' in text or 'cancel-me' in text:
        cancelled.wait(30)
        return
    if 'native-rpc-error' in text:
        send(dict(id=rid, error=dict(code=-32603, message='SECRET_RAW_ERROR_DO_NOT_JOURNAL')))
        return
    sid = 'wrong-native-id' if 'wrong-session' in text else session
    nonce = 'stale-turn' if 'wrong-turn' in text else turn
    if 'tool' in text:
        Path('fixture-artifact.txt').write_text('fixture artifact\n')
        update(sid, nonce, dict(sessionUpdate='tool_call', toolCallId='write-1', title='write', status='in_progress'))
        update(sid, nonce, dict(sessionUpdate='tool_call_update', toolCallId='write-1', status='completed'))
    update(sid, nonce, dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='fixture reply '+turn)))
    if 'missing-native' not in text:
        reason = 'error' if 'native-error' in text else 'stop'
        update(sid, nonce, dict(sessionUpdate='session_info_update', _meta=dict(
            ctaskNativeReceipt=dict(turn=nonce, stopReason=reason))))
        if 'sticky-error' in text:
            update(sid, nonce, dict(sessionUpdate='session_info_update', _meta=dict(
                ctaskNativeReceipt=dict(turn=nonce, stopReason='error'))))
    send(dict(id=rid, result=dict(stopReason='end_turn')))


for line in sys.stdin:
    value = json.loads(line)
    method, params = value.get('method'), value.get('params', {})
    if method == 'initialize':
        send(dict(id=value['id'], result=dict(protocolVersion=1, agentCapabilities=dict(loadSession=True),
            agentInfo=dict(name='synthetic-fixture', version='1'))))
    elif method == 'session/new':
        # Separate native state file is evidence only for this synthetic fixture.
        (runtime/'native-identity.json').write_text(json.dumps(dict(nativeId=identity, project=params['cwd'])))
        send(dict(id=value['id'], result=dict(sessionId=identity,
            _meta=dict(atoIdentity=dict(nativeId=identity, project=params['cwd'])))))
    elif method == 'session/prompt':
        threading.Thread(target=prompt, args=(value,), daemon=True).start()
    elif method == 'session/cancel':
        (runtime/'cancelled').write_text(identity)
        cancelled.set()
    elif 'id' in value:
        send(dict(id=value['id'], error=dict(code=-32601, message='unsupported fixture method')))
