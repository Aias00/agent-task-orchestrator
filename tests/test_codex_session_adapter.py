"""Bounded local fixtures for trusted Codex sessions; no daemon/provider calls."""
import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import agent_registry
import agents
import codex_legacy
import codex_session_adapter as native
import control
import interaction
import recovery
import runner
import session_router
import workflow
from message_bus import MessageBus, ConflictError
from transport import DeliveryResult, dispatch_registered

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = '11111111-1111-4111-8111-111111111111'
HUB = '22222222-2222-4222-8222-222222222222'
RECEIPT = '33333333-3333-4333-8333-333333333333'


class CodexSessionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-codex-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.home = self.root/'native'; self.home.mkdir()
        self.ato = self.root/'ato'
        self.environment = patch.dict(os.environ, dict(CODEX_HOME=str(self.home), ATO_HOME=str(self.ato),
            CODEX_THREAD_ID=ORIGIN), clear=False)
        self.environment.start(); self.addCleanup(self.environment.stop)
        for key in ('CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            os.environ.pop(key, None)
        self.db = self.home/'state_5.sqlite'
        with sqlite3.connect(self.db) as con:
            con.execute('create table threads(id text, rollout_path text)')
        self.paths = {}
        for target in (ORIGIN, HUB):
            self.index(target)
        self.adapter = agent_registry.get('codex')

    def index(self, target, *, cwd=None, header_id=None):
        path = self.home/(target+'.jsonl')
        path.write_text(json.dumps(dict(type='session_meta', payload=dict(
            id=header_id or target, cwd=str(cwd or self.repo))))+'\n')
        with sqlite3.connect(self.db) as con:
            con.execute('insert into threads values (?, ?)', (target, str(path)))
        self.paths[target] = path
        return path

    def append(self, *events, target=ORIGIN):
        with self.paths[target].open('a') as stream:
            for kind, fields in events:
                stream.write(json.dumps(dict(type='event_msg', payload=dict(type=kind, **fields)))+'\n')

    def receipt(self, argv, **kwargs):
        target = argv[argv.index('--thread')+1]
        return subprocess.CompletedProcess(argv, 0, f'Queued message {RECEIPT} for thread {target}.\n', '')

    def reference(self):
        return session_router.register_v2(agent_registry.SessionIdentity(
            'codex', ORIGIN, str(self.repo), 'local fixture index/header'))

    def make_workflow(self):
        plan = self.root/'plan.json'
        runner.save(plan, dict(objective='Full fixture objective', scope='Temporary fixture repository',
            authorization='Synthetic local fixture only', memory=False,
            acceptance=[dict(id='artifact', description='independent file check', checks=[['true']])]))
        directory = workflow.create(self.repo, plan, ORIGIN)
        state = runner.read(directory/'state.json'); state['status'] = 'blocked'
        runner.save(directory/'state.json', state)
        return directory

    def test_exact_index_header_project_and_read_only_lookup(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(codex_legacy.session_path(self.home, ORIGIN), str(self.paths[ORIGIN]))
        self.assertTrue(self.adapter.verify_session(ORIGIN, self.repo))
        self.assertFalse(self.adapter.verify_session(ORIGIN, self.root))
        self.assertFalse(self.adapter.verify_session('latest', self.repo))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertEqual(list(self.home.glob('*journal*')), [])

    def test_batch0_verify_session_requires_native_header_exact_id_and_absolute_project(self):
        for kind, native_id, cwd in (('other', ORIGIN, str(self.repo)),
                                     ('session_meta', HUB, str(self.repo)),
                                     ('session_meta', ORIGIN, '.')):
            with self.subTest(kind=kind, native_id=native_id, cwd=cwd):
                self.paths[ORIGIN].write_text(json.dumps(dict(
                    type=kind, payload=dict(id=native_id, cwd=cwd))) + '\n')
                self.assertFalse(self.adapter.verify_session(ORIGIN, self.repo))
                self.assertIsNone(self.adapter.session_project(ORIGIN))

    def test_wrong_index_identity_and_unindexed_latest_are_never_used(self):
        missing = str(uuid.uuid4())
        self.index(missing, header_id=HUB)
        self.assertFalse(self.adapter.verify_session(missing, self.repo))
        unknown = str(uuid.uuid4())
        (self.home/'latest.jsonl').write_text(json.dumps(dict(payload=dict(id=unknown, cwd=str(self.repo)))))
        with self.assertRaises(ValueError):
            recovery.session_path(self.home, unknown)
        empty = self.root/'empty'; empty.mkdir()
        with self.assertRaises(ValueError):
            codex_legacy.session_path(empty, ORIGIN)
        self.assertEqual(list(empty.iterdir()), [])

    def test_legacy_headers_without_project_remain_compatible_but_not_v2_verified(self):
        self.paths[ORIGIN].write_text(json.dumps({'payload': {'id': ORIGIN}})+'\n')
        self.assertEqual(interaction.verify_target(ORIGIN), str(self.paths[ORIGIN]))
        self.assertFalse(self.adapter.verify_session(ORIGIN, self.repo))
        with self.assertRaisesRegex(ValueError, 'could not be verified'):
            self.reference()

    def test_queue_uses_exact_cli_receipt_and_configured_native_home(self):
        queue = self.root/'queue-fixture.py'
        queue.write_text(f'''#!{sys.executable}
import sys,os
assert os.environ['CODEX_HOME'] == {str(self.home)!r}
assert sys.argv[1:4] == ['queue','--thread',{ORIGIN!r}]
assert sys.argv[4:6] == ['--message','local fixture']
print('Queued message {RECEIPT} for thread {ORIGIN}.')
'''); queue.chmod(0o700)
        with patch.dict(os.environ, CTASK_QUEUE_BIN=str(queue)):
            result = session_router.send(ORIGIN, 'local fixture')
        self.assertEqual((result.state, result.external_id), ('delivered', RECEIPT))
        self.assertIn(RECEIPT, result.receipt)
        self.assertEqual(native.home(), self.home)
        self.assertEqual(interaction.home(), self.ato)

    def test_wrong_duplicate_malformed_and_nonzero_receipts_are_unknown(self):
        samples = [f'Queued message {RECEIPT} for thread {HUB}.',
                   f'Queued message bad for thread {ORIGIN}.',
                   f'Queued message {RECEIPT} for thread {ORIGIN}.\n'*2]
        for output in samples:
            with self.subTest(output=output), patch('codex_session_adapter.subprocess.run',
                    return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertEqual(session_router.send(ORIGIN, 'fixture').state, 'delivery_unknown')
        with patch('codex_session_adapter.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 1, f'Queued message {RECEIPT} for thread {ORIGIN}.', '')):
            self.assertEqual(session_router.send(ORIGIN, 'fixture').state, 'delivery_unknown')

    def test_no_effect_failure_is_separate_from_ambiguous_native_attempt(self):
        with patch('codex_session_adapter.subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual(session_router.send(ORIGIN, 'fixture').state, 'failed')
        for error in (subprocess.TimeoutExpired('fixture', 1), OSError('ambiguous pipe failure')):
            with self.subTest(error=type(error).__name__), patch('codex_session_adapter.subprocess.run', side_effect=error):
                self.assertEqual(session_router.send(ORIGIN, 'fixture').state, 'delivery_unknown')
        with patch('codex_session_adapter.subprocess.run') as execute:
            self.assertEqual(session_router.send(str(uuid.uuid4()), 'fixture').state, 'failed')
            execute.assert_not_called()

    def test_router_opaque_reference_sends_and_rejects_wrong_project_before_native_effect(self):
        reference = self.reference().reference
        with patch('codex_session_adapter.subprocess.run', side_effect=self.receipt) as execute:
            self.assertEqual(session_router.send(reference, 'fixture').external_id, RECEIPT)
            self.assertEqual(execute.call_count, 1)
        with patch('codex_session_adapter.subprocess.run') as execute:
            result = self.adapter.send(dict(session_id=ORIGIN, project=str(self.root)), dict(body='fixture'))
            self.assertEqual(result.state, 'failed'); execute.assert_not_called()

    def test_environment_locators_cannot_authenticate_v2_actor(self):
        reference = self.reference().reference
        with patch.dict(os.environ, CTASK_SESSION_REF=reference):
            self.assertEqual(session_router.current_reference().reference, reference)
            with self.assertRaises(agent_registry.CapabilityUnavailable):
                session_router.authenticate_actor(reference,
                    agent_registry.ActorProof('fixture', 'untrusted-env', 'dummy'), role='user',
                    project=self.repo, workflow_id='fixture', contract_hash='hash', generation=0)
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            with self.assertRaises(ValueError): interaction.actor(ORIGIN)
        capabilities = agent_registry.session_capabilities('codex')
        self.assertEqual(capabilities.state('actor.authenticate').value, 'unknown')
        self.assertEqual(capabilities.state('session.load_resume').value, 'unsupported')

    def test_busy_idle_and_unknown_activity_are_adapter_evidence(self):
        self.assertEqual(session_router.activity(ORIGIN), 'unknown')
        self.append(('task_started', dict(turn_id='a')))
        # Longer than the first reverse-read block: lifecycle evidence is still found.
        with self.paths[ORIGIN].open('a') as stream:
            stream.write(json.dumps(dict(type='response_item', payload=dict(text='x'*700000)))+'\n')
        self.assertEqual(session_router.activity(ORIGIN), 'active')
        self.append(('task_complete', dict(turn_id='a')))
        reference = self.reference().reference
        self.assertEqual(session_router.activity_v2(reference, generation=2).state.value, 'idle')
        with self.paths[ORIGIN].open('a') as stream: stream.write('{partial')
        self.assertEqual(session_router.activity(ORIGIN), 'unknown')

    def test_reply_requires_matching_request_and_exact_completed_turn(self):
        checkpoint = session_router.delivery_checkpoint(ORIGIN)
        tracker = dict(offset=checkpoint['rollout_offset'])
        self.append(('task_started', dict(turn_id='unrelated')),
            ('agent_message', dict(message='private unrelated text')),
            ('task_complete', dict(turn_id='unrelated')),
            ('task_started', dict(turn_id='owned')),
            ('user_message', dict(message='request-fixture')),
            ('agent_message', dict(message='matching answer')),
            ('task_complete', dict(turn_id='wrong')))
        self.assertIsNone(session_router.observe_reply(ORIGIN, tracker, 'request-fixture', lambda: 10))
        self.append(('task_complete', dict(turn_id='owned')),
            ('task_started', dict(turn_id='next')),
            ('agent_message', dict(message='do not relay')),
            ('task_complete', dict(turn_id='next')))
        reply = session_router.observe_reply(ORIGIN, tracker, 'request-fixture', lambda: 11)
        self.assertEqual(reply, dict(turn='owned', text='matching answer', at=11))
        self.assertIsNone(session_router.observe_reply(ORIGIN, tracker, 'request-fixture', lambda: 12))

    def test_native_reply_and_recovery_reject_another_sessions_checkpoint(self):
        tracker = dict(offset=0)
        with self.assertRaisesRegex(ValueError, 'checkpoint identity mismatch'):
            session_router.observe_reply(ORIGIN, tracker, 'fixture', lambda: 0, path=self.paths[HUB])
        with self.assertRaisesRegex(ValueError, 'checkpoint identity mismatch'):
            session_router.observe_recovery(dict(thread_id=ORIGIN, rollout=str(self.paths[HUB])),
                dict(id='fixture', rollout_offset=0), lambda: 0)
        cursor = self.adapter.session_cursor(HUB, self.repo)
        with self.assertRaisesRegex(ValueError, 'identity or project mismatch'):
            self.adapter.observe_session(ORIGIN, self.repo, 'fixture', cursor)

    def test_request_id_prefix_cannot_attribute_another_messages_reply(self):
        tracker = dict(offset=self.paths[ORIGIN].stat().st_size)
        self.append(('task_started', dict(turn_id='other')),
            ('user_message', dict(message='[id=request-fixture-suffix]')),
            ('agent_message', dict(message='other message answer')),
            ('task_complete', dict(turn_id='other')))
        self.assertIsNone(session_router.observe_reply(ORIGIN, tracker, 'request-fixture', lambda: 0))

    def test_recovery_observation_resets_correlation_at_new_turn(self):
        request = dict(id='recover-fixture', rollout_offset=self.paths[ORIGIN].stat().st_size)
        self.append(('task_started', dict(turn_id='owned')),
            ('user_message', dict(message=request['id'])),
            ('agent_message', dict(message='diagnosis')),
            ('task_complete', dict(turn_id='wrong')),
            ('task_started', dict(turn_id='unrelated')),
            ('task_complete', dict(turn_id='unrelated')))
        recovery.observe_response(dict(thread_id=ORIGIN, rollout=str(self.paths[ORIGIN])), request)
        self.assertIn('responded_at', request)
        self.assertNotIn('turn_completed_at', request)

    def test_v2_events_cursor_are_correlated_and_json_serializable(self):
        reference = self.reference().reference
        cursor = session_router.cursor(reference)
        self.append(('task_started', dict(turn_id='turn-1')),
            ('user_message', dict(message='message-fixture')),
            ('agent_message', dict(message='response')),
            ('task_complete', dict(turn_id='turn-1')))
        events, cursor = session_router.observe_v2(reference, 'message-fixture', generation=4, cursor=cursor)
        self.assertEqual([e.kind for e in events], ['message_received','response_completed','turn_completed'])
        self.assertTrue(all(e.generation == 4 and e.turn_id == 'turn-1' for e in events))
        self.assertNotIn('workflow_accepted', [e.kind for e in events])
        json.dumps(cursor, allow_nan=False)
        self.assertEqual(session_router.observe_v2(reference, 'message-fixture', generation=4, cursor=cursor)[0], [])
        with self.assertRaisesRegex(ValueError, 'generation mismatch'):
            session_router.observe_v2(reference, 'message-fixture', generation=5, cursor=cursor)
        with self.assertRaisesRegex(ValueError, 'correlation mismatch'):
            session_router.observe_v2(reference, 'different-message', generation=4, cursor=cursor)

    def test_partial_lines_are_retried_and_truncated_cursors_fail_closed(self):
        tracker = dict(offset=self.paths[ORIGIN].stat().st_size)
        with self.paths[ORIGIN].open('a') as stream: stream.write('{"type":"event_msg",')
        original = dict(tracker)
        self.assertIsNone(native.observe_reply(self.paths[ORIGIN], tracker, 'fixture', lambda: 0))
        self.assertEqual(tracker, original)
        self.paths[ORIGIN].write_text('')
        with self.assertRaisesRegex(ValueError, 'truncated'):
            native.observe_reply(self.paths[ORIGIN], tracker, 'fixture', lambda: 0)

    def test_legacy_question_controller_recovery_roundtrip_and_no_replay(self):
        directory = self.make_workflow()
        interaction.configure(HUB, 'synthetic fixture user binding')
        with patch('interaction.subprocess.run', side_effect=self.receipt) as execute:
            question = interaction.ask(directory, 'Fixture question?', 'A')
            self.assertEqual(question['question_delivery']['state'], 'queued')
            with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
                interaction.answer(question['id'], 'A', 'synthetic fixture user chose A')
            interaction.accept(question['id'])
            self.assertEqual(execute.call_count, 2)
        with patch('control.subprocess.run', side_effect=subprocess.TimeoutExpired('fixture', 1)) as execute:
            first = control.send('fixture-once', ORIGIN, 'fixture work')
            second = control.send('fixture-once', ORIGIN, 'fixture work')
            self.assertEqual(first, second); self.assertEqual(execute.call_count, 1)
            self.assertEqual(first['state'], 'delivery_unknown')
        recovery.enable(directory, 'synthetic bounded fixture authorization')
        with patch('recovery.subprocess.run', side_effect=self.receipt) as execute:
            recovery.tick(directory); recovery.tick(directory)
            self.assertEqual(execute.call_count, 1)
        record = runner.read(directory/'recovery.json')
        self.assertEqual(record['requests'][0]['state'], 'queued')
        self.assertEqual(record['thread_id'], ORIGIN)
        self.assertEqual(runner.read(directory/'state.json')['status'], 'blocked')

    def test_native_queue_capability_releases_busy_slot_without_replaying_unknown(self):
        bus = MessageBus(self.ato/'mailbox.sqlite')
        sender = bus.register_endpoint('sender', kind='fixture', session_id='sender', project=self.repo, source='fixture')
        receiver = bus.register_endpoint('receiver', kind='codex', session_id=ORIGIN, project=self.repo, source='fixture')
        route = bus.bind_route(sender='sender', target='receiver', task_id='fixture', contract_hash='hash', source='fixture')['id']
        owner = bus.acquire_endpoint(receiver, owner='fixture')
        def enqueue(key):
            return bus.enqueue(sender, binding_id=route, target='receiver', kind='request', body=key,
                idempotency_key=key, task_id='fixture', contract_hash='hash')
        first = enqueue('first')
        with patch('codex_session_adapter.subprocess.run', side_effect=subprocess.TimeoutExpired('fixture',1)) as execute:
            self.assertEqual(dispatch_registered(bus, receiver, owner)['state'], 'delivery_unknown')
            enqueue('second')
            self.assertEqual(dispatch_registered(bus, receiver, owner)['state'], 'delivery_unknown')
            self.assertIsNone(dispatch_registered(bus, receiver, owner))
            self.assertEqual(execute.call_count, 2)
        self.assertEqual(bus.message(first['id'])['state'], 'delivery_unknown')
        self.assertFalse(bus.message(first['id'])['busy'])

    def test_queue_release_is_capability_based_and_rejected_for_stream_adapter(self):
        kind = 'fixture_native_queue_'+uuid.uuid4().hex
        class QueueFixture:
            capabilities = frozenset({'transport', 'native_queue_serialization'})
            def __init__(self): self.kind = kind
            def send(self, endpoint, message): return DeliveryResult('delivered', 'fixture')
        agent_registry.register(QueueFixture())
        self.addCleanup(agent_registry._adapters.pop, kind)
        bus = MessageBus(self.ato/'bus.sqlite')
        sender = bus.register_endpoint('sender', kind='fixture', session_id='sender', project=self.repo, source='fixture')
        receiver = bus.register_endpoint('receiver', kind=kind, session_id='native-fixture', project=self.repo, source='fixture')
        route = bus.bind_route(sender='sender', target='receiver', task_id='t', contract_hash='h', source='fixture')['id']
        message = bus.enqueue(sender, binding_id=route, target='receiver', kind='request', body='fixture',
            idempotency_key='one', task_id='t', contract_hash='h')
        owner = bus.acquire_endpoint(receiver, owner='fixture')
        dispatch_registered(bus, receiver, owner)
        self.assertFalse(bus.message(message['id'])['busy'])
        agent_registry._adapters[kind].capabilities = frozenset({'transport'})
        with self.assertRaises(ConflictError): bus.queued_natively(receiver, message['id'])

    def test_native_receiver_acceptance_racing_send_receipt_is_not_regressed(self):
        kind = 'fixture_native_race_'+uuid.uuid4().hex
        bus = MessageBus(self.ato/'race.sqlite')
        class NativeFixture:
            capabilities = frozenset({'transport', 'native_queue_serialization'})
            def __init__(self): self.kind = kind
            def send(self, endpoint, message):
                bus.accept(receiver, message['id'], receipt_id='receiver-accepted')
                return DeliveryResult('delivered', 'native-receipt')
        agent_registry.register(NativeFixture())
        self.addCleanup(agent_registry._adapters.pop, kind)
        sender = bus.register_endpoint('sender', kind='fixture', session_id='sender', project=self.repo, source='fixture')
        receiver = bus.register_endpoint('receiver', kind=kind, session_id='native', project=self.repo, source='fixture')
        route = bus.bind_route(sender='sender', target='receiver', task_id='t', contract_hash='h', source='fixture')['id']
        message = bus.enqueue(sender, binding_id=route, target='receiver', kind='request', body='fixture',
            idempotency_key='race', task_id='t', contract_hash='h')
        owner = bus.acquire_endpoint(receiver, owner='fixture')
        result = dispatch_registered(bus, receiver, owner)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(bus.message(message['id'])['external_id'], 'native-receipt')
        self.assertIsNone(dispatch_registered(bus, receiver, owner))

    def test_core_ast_has_no_vendor_imports_or_native_event_decoders(self):
        for name in ('control','recovery','interaction','delegation','workflow','session_router','message_bus','transport'):
            tree = ast.parse((ROOT/(name+'.py')).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(a.name in ('codex_legacy','appserver','codex_session_adapter') for a in node.names), name)
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, ('codex_legacy','appserver','codex_session_adapter'), name)
        self.assertIs(codex_legacy.observe_reply, native.observe_reply)
        self.assertIs(codex_legacy.create_session, native.create_session)

    def create_with_fixture(self, mode='normal'):
        fixture = self
        calls = []
        persisted = []
        created_id = str(uuid.uuid4())
        class RPCFixture:
            def __init__(self, home): fixture.assertEqual(home, fixture.home)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def request(self, method, params):
                calls.append((method, params))
                if method == 'thread/start':
                    fixture.index(created_id)
                    return dict(thread=dict(id=created_id, cwd=str(fixture.root if mode == 'cwd' else fixture.repo)),
                        sandbox=dict(type='dangerFullAccess' if mode == 'sandbox' else 'workspaceWrite'),
                        approvalPolicy='on-request' if mode == 'approval' else 'never')
                if method == 'thread/read':
                    return dict(thread=dict(id=HUB if mode == 'readback' else created_id, cwd=str(fixture.repo)))
                return {}
        extra = self.root/'extra'; extra.mkdir(exist_ok=True)
        contract = dict(sandbox='workspace-write', permissions=dict(network_access=False, writable_roots=[str(extra)]))
        with patch('appserver.Client', RPCFixture):
            result = self.adapter.create_session(self.repo, contract, 'fixture coordinator', 'fixture-delegation',
                                                lambda **fields: persisted.append(fields))
        return result, calls, persisted, extra

    def test_creation_uses_exact_project_private_roots_permissions_and_readback(self):
        created, calls, persisted, extra = self.create_with_fixture()
        self.assertEqual([method for method, _ in calls], ['thread/start','thread/name/set','thread/read'])
        start = calls[0][1]
        self.assertEqual(start['cwd'], str(self.repo))
        self.assertEqual((start['sandbox'], start['approvalPolicy'], start['ephemeral']),
                         ('workspace-write', 'never', False))
        self.assertEqual(start['config']['sandbox_workspace_write.writable_roots'],
            [str(self.repo/'.codex/tasks'), str(interaction.directory()), str(extra)])
        self.assertIs(start['config']['sandbox_workspace_write.network_access'], False)
        self.assertEqual(calls[-1][1], dict(threadId=created.identity, includeTurns=False))
        self.assertEqual(persisted[0]['origin_thread'], created.identity)
        self.assertEqual(persisted[-1]['verified_rollout'], created.legacy_rollout)
        self.assertEqual(session_router.created_reference('codex', created), created.identity)

    def test_creation_rejects_wrong_project_policy_approval_and_readback(self):
        for mode in ('cwd','sandbox','approval','readback'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.create_with_fixture(mode)


if __name__ == '__main__':
    unittest.main()
