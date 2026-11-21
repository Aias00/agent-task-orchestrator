#!/usr/bin/env python3
"""Qualify installed native CLIs against a loopback-only Anthropic fixture.

No real credentials/config/hub are used. Evidence persists beside --output;
temporary projects and native session/config files are removed after cleanup.
Cloud authentication is deliberately not tested.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from message_adapters import ClaudeAdapter, PiAdapter
from message_bus import MessageBus
from transport import dispatch_one, receive_event


class LocalProvider:
    def __init__(self):
        self.requests = []
        self.responses = []
        self.nonce = secrets.token_hex(16)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.split('?')[0].endswith('/count_tokens'):
                    body = b'{"input_tokens":100}'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.split('?')[0] not in ('/v1/messages', '/messages'):
                    self.send_error(404)
                    return
                fixture.requests.append(data)
                # Persistence is proved by actual provider input, including the
                # first assistant response, not by repeating a chosen UUID.
                messages = data['messages']
                prior = any(m.get('role') == 'assistant' and fixture.nonce in json.dumps(m)
                            for m in messages)
                last = messages[-1]['content']
                if isinstance(last, list):
                    last = ''.join(c.get('text', '') for c in last if c.get('type') == 'text')
                envelope = None
                # Claude may attach separate system-reminder content blocks.
                # Select the actual adapter envelope, never a history-derived ID.
                for pos, char in enumerate(last):
                    if char != '{':
                        continue
                    try:
                        value, _ = json.JSONDecoder().raw_decode(last[pos:])
                    except ValueError:
                        continue
                    if isinstance(value, dict) and 'id' in value and 'body' in value:
                        envelope = value
                        break
                if envelope is None:
                    self.send_error(400, 'missing mailbox envelope')
                    return
                text = json.dumps(dict(message_id=envelope['id'], memory=fixture.nonce,
                                       prior_assistant_seen=prior))
                fixture.responses.append(text)
                message = dict(id='msg_'+uuid.uuid4().hex, type='message', role='assistant',
                    model=data['model'], content=[], stop_reason=None, stop_sequence=None,
                    usage=dict(input_tokens=100, output_tokens=0))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.end_headers()
                events = [
                    ('message_start', dict(type='message_start', message=message)),
                    ('content_block_start', dict(type='content_block_start', index=0,
                                                content_block=dict(type='text', text=''))),
                    ('content_block_delta', dict(type='content_block_delta', index=0,
                        delta=dict(type='text_delta', text=text))),
                    ('content_block_stop', dict(type='content_block_stop', index=0)),
                    ('message_delta', dict(type='message_delta', delta=dict(stop_reason='end_turn',
                        stop_sequence=None), usage=dict(output_tokens=30))),
                    ('message_stop', dict(type='message_stop'))]
                for name, value in events:
                    self.wfile.write(('event: '+name+'\ndata: '+json.dumps(value)+'\n\n').encode())
                self.wfile.flush()

        try:
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except PermissionError:
            raise RuntimeError('local provider socket bind denied by worker sandbox; '
                               'run native qualification from the stable host') from None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def probe(kind, evidence):
    binary = Path(shutil.which(kind)).resolve(strict=True)
    row = dict(kind=kind, executable=str(binary),
        executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        passed=False, real_cli=True, provider='local-fixture', same_session=False,
        request_count=0, response_count=0)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ctask-native-'+kind+'-') as directory, LocalProvider() as fixture:
        root = Path(directory)
        project = root/'project'
        project.mkdir()
        runtime = root/'runtime'
        env = dict(ANTHROPIC_BASE_URL=fixture.url, ANTHROPIC_API_KEY='ctask-invented-local-fixture-key',
                   API_TIMEOUT_MS='15000', CLAUDE_CODE_MAX_OUTPUT_TOKENS='1024')
        session_id = str(uuid.uuid4())
        adapter_cls = ClaudeAdapter if kind == 'claude' else PiAdapter
        adapter = adapter_cls(endpoint_id=kind, session_id=session_id, project=project,
            runtime=runtime, executable=str(binary), provider_env=env,
            model='claude-sonnet-4-6' if kind == 'claude' else 'ctask-fixture/mailbox-model', timeout=40)
        (runtime/'pi').mkdir(parents=True)
        (runtime/'pi/models.json').write_text(json.dumps(dict(providers={'ctask-fixture': dict(
            baseUrl=fixture.url, api='anthropic-messages', apiKey='ctask-invented-local-fixture-key',
            models=[dict(id='mailbox-model', name='Mailbox fixture', reasoning=False, input=['text'],
                cost=dict(input=0, output=0, cacheRead=0, cacheWrite=0), contextWindow=32000,
                maxTokens=1024)])})))
        clean_env = dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'),
            PI_CODING_AGENT_DIR=str(runtime/'pi'), CLAUDE_CONFIG_DIR=str(runtime/'claude'),
            PI_OFFLINE='1', PI_TELEMETRY='0')
        version = subprocess.run([str(binary), '--version'], env=clean_env, cwd=project,
                                 capture_output=True, text=True, timeout=20)
        help_result = subprocess.run([str(binary), '--help'], env=clean_env, cwd=project,
                                     capture_output=True, text=True, timeout=20)
        row['version'] = version.stdout.strip()
        (evidence/'help.txt').write_text(help_result.stdout)
        bus = MessageBus(root/'mailbox.sqlite')
        sender = bus.register_endpoint('hub-fixture', kind='fixture', session_id='fixture',
                                       project=project, source='isolated probe host')
        target = bus.register_endpoint(kind, kind=kind, session_id=session_id, project=project,
                                       source='explicit isolated native CLI session')
        route = bus.bind_route(sender=sender.endpoint_id, target=kind, task_id='probe',
                                contract_hash='fixture-contract', source='isolated local probe')
        owner = bus.acquire_endpoint(target, owner='native-probe', lease_seconds=180)
        received = []
        try:
            with adapter:
                for turn in range(2):
                    message = bus.enqueue(sender, binding_id=route['id'], target=kind, kind='request',
                        body='First isolated turn' if turn == 0 else 'Recall the previous assistant memory',
                        idempotency_key=str(turn), task_id='probe', contract_hash='fixture-contract')
                    receipt = dispatch_one(bus, target, owner, {kind: adapter})
                    if receipt['state'] != 'delivered':
                        raise AssertionError('native delivery not acknowledged')
                    events = adapter.events()
                    for event in events:
                        # reply is terminal in the bus; completed is a separate
                        # normalized observation, not a second transition.
                        if event.kind in ('accepted', 'reply'):
                            receive_event(bus, target, event)
                    reply = next(e for e in events if e.kind == 'reply')
                    value = json.loads(reply.text)
                    assert value['message_id'] == message['id']
                    assert value['prior_assistant_seen'] is (turn == 1)
                    received.append(dict(message_id=message['id'], session_id=reply.session_id,
                                         reply=value, receipt=receipt['external_id']))
            assert len(fixture.requests) == 2
            assert received[0]['reply']['memory'] == received[1]['reply']['memory']
            assert all(r['session_id'] == session_id for r in received)
            assert adapter.process is None
            row.update(passed=True, same_session=True, request_count=len(fixture.requests),
                       response_count=len(received), session_id=session_id)
        except Exception as exc:
            # No raw native diagnostics in the public report; isolated private
            # evidence contains only invented credentials and fixture work.
            row['error'] = type(exc).__name__+': '+str(exc)
        finally:
            adapter.close()
            for name in ('events.jsonl', 'stderr.log', 'session.json'):
                if (runtime/name).exists():
                    shutil.copy2(runtime/name, evidence/name)
            (evidence/'provider-requests.json').write_text(json.dumps(fixture.requests, indent=2))
            (evidence/'receipts.json').write_text(json.dumps(received, indent=2))
            row['evidence'] = str(evidence)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = output.parent/(output.stem+'-evidence')
    rows = []
    for kind in ('claude', 'pi'):
        try:
            row = probe(kind, evidence/kind)
        except Exception as exc:
            row = dict(kind=kind, passed=False, error=type(exc).__name__+': '+str(exc))
        rows.append(row)
        print(json.dumps(row), flush=True)
    output.write_text(json.dumps(dict(schema_version=1, runs=rows), indent=2)+'\n')
    return 0 if all(r['passed'] for r in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
