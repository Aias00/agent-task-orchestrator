"""Real owned subprocess tests; explicit fixtures, separate from native probes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from message_adapters import AdapterError, ClaudeAdapter, PiAdapter, CodexQueueAdapter
from message_bus import MessageBus
from transport import dispatch_one, receive_event


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ctask-adapter-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root/'project'
        self.project.mkdir()
        self.binary = self.root/'native-fixture'
        self.binary.write_text('#!'+sys.executable+'\n'+
            (Path(__file__).parent/'fixtures/mailbox_cli.py').read_text())
        self.binary.chmod(0o700)

    def adapter(self, cls, **kw):
        adapter = cls(endpoint_id='origin', session_id=str(uuid.uuid4()), project=self.project,
            runtime=self.root/('runtime-'+uuid.uuid4().hex), executable=str(self.binary), timeout=2, **kw)
        self.addCleanup(adapter.close)
        return adapter

    def ep(self, adapter):
        return dict(id=adapter.endpoint_id, session_id=adapter.session_id,
                    project=adapter.project, kind=adapter.kind)

    def message(self, body='hello'):
        return dict(id='msg-'+uuid.uuid4().hex, target='origin', kind='request', body=body,
                    task_id='task', contract_hash='hash', correlation_id='correlation', reply_to=None)

    def test_two_correlated_turns_and_unicode_framing(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind), self.adapter(cls) as adapter:
                pid = adapter.process.pid
                for turn in (1, 2):
                    message = self.message()
                    self.assertEqual(adapter.send(self.ep(adapter), message).state, 'delivered')
                    events = adapter.events()
                    self.assertEqual([e.kind for e in events], ['accepted', 'reply', 'completed'])
                    value = json.loads(events[1].text)
                    self.assertEqual(value['turns'], turn)
                    self.assertEqual(value['message_id'], message['id'])
                    self.assertEqual(value['text'], 'reply\u2028intact')
                    self.assertEqual({e.session_id for e in events}, {adapter.session_id})
                    self.assertEqual(adapter.process.pid, pid)

    def test_restart_resumes_exact_native_transcript(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind):
                adapter = self.adapter(cls)
                with adapter:
                    self.assertEqual(adapter.send(self.ep(adapter), self.message()).state, 'delivered')
                    adapter.events()
                replacement = cls(endpoint_id=adapter.endpoint_id, session_id=adapter.session_id,
                    project=adapter.project, runtime=adapter.runtime, executable=str(self.binary), timeout=2)
                self.addCleanup(replacement.close)
                with replacement:
                    self.assertEqual(replacement.send(self.ep(replacement), self.message()).state, 'delivered')
                    result = next(e for e in replacement.events() if e.kind == 'reply')
                    self.assertEqual(json.loads(result.text)['turns'], 2)
                stored = [json.loads(line) for line in (adapter.runtime/'events.jsonl').read_text().splitlines()]
                self.assertEqual(len([e for e in stored if e['kind'] == 'reply']), 2)
                self.assertEqual([e.receipt_id for e in adapter.recorded_events()],
                                 [e['receipt_id'] for e in stored])

    def test_missing_transcript_does_not_create_blank_same_id(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind):
                adapter = self.adapter(cls)
                with adapter:
                    adapter.send(self.ep(adapter), self.message())
                    adapter.events()
                Path(adapter.native_session_file).unlink()
                with self.assertRaisesRegex(AdapterError, 'transcript missing'):
                    adapter.start()

    def test_exclusive_owner_and_identity_rebinding_refused(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind), self.adapter(cls) as adapter:
                duplicate = cls(endpoint_id=adapter.endpoint_id, session_id=adapter.session_id,
                    project=adapter.project, runtime=adapter.runtime, executable=str(self.binary))
                self.addCleanup(duplicate.close)
                with self.assertRaisesRegex(AdapterError, 'already owned'):
                    duplicate.start()
                with self.assertRaisesRegex(AdapterError, 'endpoint/project/session'):
                    adapter.send(dict(self.ep(adapter), project=str(self.root)), self.message())
                adapter.close()
                duplicate.session_id = str(uuid.uuid4())
                with self.assertRaisesRegex(AdapterError, 'identity or permission mismatch'):
                    duplicate.start()

    def test_busy_and_unknown_send_survive_restart(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind):
                adapter = self.adapter(cls)
                with adapter:
                    message = self.message('busy')
                    self.assertEqual(adapter.send(self.ep(adapter), message).state, 'delivered')
                    with self.assertRaisesRegex(AdapterError, 'busy'):
                        adapter.send(self.ep(adapter), self.message())
                    with self.assertRaisesRegex(AdapterError, 'deadline'):
                        adapter.events(timeout=.05)
                with self.assertRaisesRegex(AdapterError, 'unresolved native send'):
                    adapter.start()
                stored = json.loads((adapter.runtime/'session.json').read_text())
                self.assertEqual(stored['pending'], message['id'])
                self.assertEqual(stored['state'], 'delivery_unknown')

    def test_timeout_crash_invalid_record_and_wrong_reply_are_not_retried(self):
        for cls in (ClaudeAdapter, PiAdapter):
            for behavior in ('timeout', 'crash', 'malformed', 'wrong'):
                with self.subTest(kind=cls.kind, behavior=behavior), self.adapter(cls) as adapter:
                    adapter.timeout = .15
                    result = adapter.send(self.ep(adapter), self.message(behavior))
                    self.assertEqual(result.state, 'delivery_unknown')
                    self.assertIsNotNone(adapter.pending)
                    self.assertNotIn('reply', [e.kind for e in adapter._events])

    def test_failed_native_turn_never_becomes_success(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind), self.adapter(cls) as adapter:
                adapter.send(self.ep(adapter), self.message('failed'))
                with self.assertRaises(AdapterError):
                    adapter.events()
                self.assertIsNotNone(adapter.pending)
                self.assertNotIn('reply', [e.kind for e in adapter._events])

    def test_close_reaps_process_and_releases_lock(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind):
                adapter = self.adapter(cls).start()
                process = adapter.process
                reader = adapter._reader
                adapter.close()
                self.assertIsNotNone(process.poll())
                self.assertFalse(reader.is_alive())
                self.assertIsNone(adapter._lock)
                self.assertIsNone(adapter.process)
                adapter.close()

    def test_pi_native_identity_mismatch_reaps_startup(self):
        adapter = self.adapter(PiAdapter, provider_env={'CTASK_FIXTURE_BAD_ID': '1'})
        with self.assertRaisesRegex(AdapterError, 'native session identity mismatch'):
            adapter.start()
        self.assertIsNone(adapter.process)
        self.assertIsNone(adapter._lock)
        self.assertFalse(adapter._reader.is_alive())

    def test_isolated_environment_and_file_tool_policy(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'real-looking-never-inherit',
                                     'NODE_OPTIONS': '--require=unsafe'}):
            for cls in (ClaudeAdapter, PiAdapter):
                with self.subTest(kind=cls.kind), self.adapter(cls) as adapter:
                    self.assertNotIn('ANTHROPIC_API_KEY', adapter.env)
                    self.assertNotIn('NODE_OPTIONS', adapter.env)
                    self.assertNotIn('HOME', adapter.env)
                    self.assertEqual(Path(adapter.env['CLAUDE_CONFIG_DIR']), adapter.runtime/'claude')
                    self.assertEqual(Path(adapter.env['PI_CODING_AGENT_DIR']), adapter.runtime/'pi')
                    self.assertEqual(adapter.runtime.stat().st_mode & 0o777, 0o700)
                    self.assertEqual((adapter.runtime/'stderr.log').stat().st_mode & 0o777, 0o600)
                    argv = adapter._argv(False)
                    if cls.kind == 'claude':
                        self.assertEqual(argv[argv.index('--tools')+1], '')
                        self.assertIn('--restricted', argv)
                        self.assertIn('--strict-mcp-config', argv)
                    else:
                        self.assertIn('--no-tools', argv)
                        self.assertIn('--no-mcp', argv)
                file_adapter = self.adapter(cls, tools='workspace-write')
                with patch('message_adapters.Path.is_file', return_value=False):
                    with self.assertRaisesRegex(AdapterError, 'sandbox-exec'):
                        file_adapter.start()
        with self.assertRaisesRegex(AdapterError, 'tool policy'):
            self.adapter(PiAdapter, tools='bash')
        with self.assertRaisesRegex(AdapterError, 'home directories'):
            self.adapter(PiAdapter, provider_env={'HOME': str(self.root/'replacement')})

    def test_durable_dispatch_and_normalized_receipts_roundtrip(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind), self.adapter(cls) as adapter:
                bus = MessageBus(self.root/(cls.kind+'.sqlite'))
                sender = bus.register_endpoint('sender', kind='fixture', session_id='sender',
                    project=self.project, source='test host')
                receiver = bus.register_endpoint('origin', kind=cls.kind, session_id=adapter.session_id,
                    project=self.project, source='test host owned native subprocess')
                route = bus.bind_route(sender='sender', target='origin', task_id='task',
                    contract_hash='hash', source='test route')
                owner = bus.acquire_endpoint(receiver, owner='test')
                messages = [bus.enqueue(sender, target='origin', binding_id=route['id'], kind='request',
                    body='request', task_id='task', contract_hash='hash', idempotency_key=str(i)) for i in range(2)]
                for message in messages:
                    result = dispatch_one(bus, receiver, owner, {cls.kind: adapter})
                    self.assertEqual(result['id'], message['id'])
                    self.assertEqual(result['state'], 'delivered')
                    self.assertIsNone(dispatch_one(bus, receiver, owner, {cls.kind: adapter}))
                    for event in adapter.events():
                        if event.kind in ('accepted', 'reply'):
                            first = receive_event(bus, receiver, event)
                            again = receive_event(bus, receiver, event)
                            self.assertEqual(first['id'], again['id'])
                self.assertEqual([bus.message(m['id'])['state'] for m in messages], ['replied']*2)

    def test_cancellation_does_not_accept_late_native_reply_or_restart_work(self):
        for cls in (ClaudeAdapter, PiAdapter):
            with self.subTest(kind=cls.kind), self.adapter(cls) as adapter:
                bus = MessageBus(self.root/(cls.kind+'-late.sqlite'))
                sender = bus.register_endpoint('sender', kind='fixture', session_id='sender',
                    project=self.project, source='test host')
                receiver = bus.register_endpoint('origin', kind=cls.kind, session_id=adapter.session_id,
                    project=self.project, source='test host')
                route = bus.bind_route(sender='sender', target='origin', task_id='task',
                    contract_hash='hash', source='test route')
                owner = bus.acquire_endpoint(receiver, owner='test')
                message = bus.enqueue(sender, target='origin', binding_id=route['id'], kind='request',
                    body='request', task_id='task', contract_hash='hash', idempotency_key='late')
                dispatch_one(bus, receiver, owner, {cls.kind: adapter})
                bus.cancel(sender, message['id'], source='actual test stop')
                for event in adapter.events():
                    if event.kind in ('accepted', 'reply'):
                        receive_event(bus, receiver, event)
                self.assertEqual(bus.message(message['id'])['state'], 'cancelled')
                self.assertIsNone(bus.message(message['id'])['reply_id'])
                self.assertIsNone(dispatch_one(bus, receiver, owner, {cls.kind: adapter}))

    def test_codex_queue_exact_target_and_legacy_override(self):
        thread, external = str(uuid.uuid4()), str(uuid.uuid4())
        fixture = self.root/'queue'
        fixture.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\n'
            +'Path('+repr(str(self.root/'queue-argv'))+').write_text(repr(sys.argv))\n'
            +'print('+repr('Queued message '+external+' for thread '+thread)+')\n')
        fixture.chmod(0o700)
        verified = []
        with patch.dict(os.environ, {'CTASK_QUEUE_BIN': str(fixture)}):
            adapter = CodexQueueAdapter(home=self.root, verifier=lambda *args: verified.append(args))
            endpoint = dict(id='origin', kind='codex', session_id=thread, project=str(self.project))
            result = adapter.send(endpoint, self.message())
        self.assertEqual(result.state, 'delivered')
        self.assertEqual(result.external_id, external)
        self.assertEqual(verified, [(str(self.root), thread, str(self.project))])
        self.assertIn("'queue', '--thread', '"+thread, (self.root/'queue-argv').read_text())
        with self.assertRaises(AdapterError):
            adapter.send(dict(endpoint, kind='pi'), self.message())

    def test_codex_unknown_receipt_timeout_and_spawn_failure(self):
        endpoint = dict(id='origin', kind='codex', session_id=str(uuid.uuid4()), project=str(self.project))
        adapter = CodexQueueAdapter(home=self.root, verifier=lambda *args: None)
        for result in (subprocess.CompletedProcess([], 0, 'Queued message '+str(uuid.uuid4())+
                       ' for thread '+str(uuid.uuid4()), ''), subprocess.CompletedProcess([], 1, '', 'error')):
            with patch('message_adapters.subprocess.run', return_value=result):
                self.assertEqual(adapter.send(endpoint, self.message()).state, 'delivery_unknown')
        with patch('message_adapters.subprocess.run', side_effect=subprocess.TimeoutExpired([], 1)):
            self.assertEqual(adapter.send(endpoint, self.message()).state, 'delivery_unknown')
        with patch('message_adapters.subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual(adapter.send(endpoint, self.message()).state, 'failed')


if __name__ == '__main__':
    unittest.main()
