"""Workflow authority + real protocol subprocess fixtures, never live sessions.

Native executable qualification is separately owned by the external host.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import control
import delegation
import interaction
import mailbox_routes as mailbox
from message_adapters import CodexQueueAdapter
from message_bus import MessageBus
import recovery
import runner
import workflow

HUB = '22222222-2222-4222-8222-222222222222'
LEGACY = '11111111-1111-4111-8111-111111111111'


class MailboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ctask-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.home = self.root/'codex-home'; self.home.mkdir()
        with sqlite3.connect(self.home/'state_5.sqlite') as con:
            con.execute('create table threads(id text, rollout_path text)')
            for thread in (HUB, LEGACY):
                path = self.home/(thread+'.jsonl')
                path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=thread)))+'\n')
                con.execute('insert into threads values(?,?)', (thread, str(path)))
        self.native = self.root/'native-fixture'
        self.native.write_text('#!'+sys.executable+'\n'+(ROOT/'tests/fixtures/mailbox_cli.py').read_text())
        self.native.chmod(0o700)
        self.calls = self.root/'calls.jsonl'
        self.queue = self.root/'queue-fixture'
        self.queue.write_text('#!'+sys.executable+'\n'+f'''
import json, os, sys
from pathlib import Path
target = sys.argv[sys.argv.index('--thread')+1]
with open({str(self.calls)!r},'a') as f:
    f.write(json.dumps(dict(argv=sys.argv[1:],cwd=os.getcwd()))+'\\n')
if os.environ.get('FIXTURE_UNKNOWN'):
    print('uncertain')
else:
    print('Queued message '+target+' for thread '+target+'.')
''')
        self.queue.chmod(0o700)
        env = dict(CODEX_HOME=str(self.home), CODEX_THREAD_ID=HUB,
                   CTASK_QUEUE_BIN=str(self.queue), CTASK_ENDPOINT_ID='', CTASK_ENDPOINT_TOKEN='',
                   CTASK_SESSION_ID='', CTASK_PROJECT='')
        self.env = patch.dict(os.environ, env); self.env.start(); self.addCleanup(self.env.stop)
        self.plan = self.repo/'contract-input.json'
        runner.save(self.plan, dict(objective='Preserve every final criterion', scope='Isolated fixture only',
            authorization='User authorized local fixture implementation', memory=False,
            acceptance=[dict(id='result', description='Independent final check', checks=[['test', '-f', 'result']])],
            limits=dict(runtime_seconds=600, attempt_seconds=10, check_seconds=2, max_attempts=1)))
        interaction.configure(HUB, 'Fixture user explicitly selected this hub')
        control.enable('Fixture user requested bounded observation', reminder_seconds=60, max_reminders=1)

    @contextmanager
    def actor(self, reference):
        env = (mailbox.actor_environment(reference) if reference.startswith('managed:') else
               dict(CODEX_THREAD_ID=reference, CTASK_ENDPOINT_ID='', CTASK_ENDPOINT_TOKEN='',
                    CTASK_SESSION_ID='', CTASK_PROJECT=''))
        with patch.dict(os.environ, env):
            yield

    def managed(self, kind='claude'):
        return mailbox.register_managed(kind=kind, session_id=str(uuid.uuid4()), project=self.repo,
            runtime=self.root/('runtime-'+uuid.uuid4().hex), source='Framework-owned isolated native fixture',
            executable=str(self.native))

    def create(self, origin):
        d = workflow.create(self.repo, self.plan, **({'origin_endpoint': origin} if origin.startswith('managed:') else
                                                  {'origin_thread': origin}))
        s = runner.read(d/'state.json'); s.update(status='blocked', elapsed_seconds=271.5)
        runner.save(d/'state.json', s)
        return d

    def ask(self, d, origin, text='Select compatibility option?'):
        with self.actor(origin):
            return interaction.ask(d, text, 'Preserve compatibility')

    def answer(self, q):
        with self.actor(HUB):
            return interaction.answer(q['id'], 'Keep existing scope; do not add budget', 'Actual fixture user reply')

    def test_both_native_origins_question_answer_result_two_turns_and_restart(self):
        for kind in ('claude', 'pi'):
            with self.subTest(kind=kind):
                origin = self.managed(kind); d = self.create(origin)
                before = (d/'contract.json').read_bytes()
                for turn in (1, 2):
                    q = self.ask(d, origin, f'Compatibility choice {turn}?')
                    answer = self.answer(q)
                    self.assertEqual(answer['answer_delivery']['state'], 'queued')
                    with self.actor(origin), self.assertRaisesRegex(ValueError, 'not been delivered'):
                        interaction.accept(q['id'])
                    result = mailbox.pump(origin)
                    reply = mailbox.response(result['id'])
                    value = json.loads(reply['body'])
                    self.assertEqual(value['turns'], turn)
                    self.assertEqual(value['message_id'], answer['answer_delivery']['mailbox_id'])
                    self.assertEqual(reply['reply_to'], result['id'])
                    with self.actor(origin):
                        self.assertEqual(interaction.accept(q['id'])['state'], 'accepted')
                    control.tick(); control.tick()
                    action = next(a for a in control.rows(True) if a['id'] == q['id'])
                    self.assertEqual(action['state'], 'awaiting_execution')
                    self.assertEqual(action['origin_reply']['turn'], reply['id'])
                    self.assertEqual(action['origin_reply']['delivery']['state'], 'queued')
                    self.assertEqual(runner.read(d/'state.json')['elapsed_seconds'], 271.5)
                self.assertEqual((d/'contract.json').read_bytes(), before)
                origin_data = runner.read(d/'origin.json')
                self.assertIsNone(origin_data['origin_thread_id'])
                self.assertEqual(origin_data['origin_endpoint_id'], origin)
                # Opening a fresh store reconstructs the same persisted response.
                reopened = MessageBus(mailbox.bus().path)
                self.assertEqual(reopened.message(result['id'])['reply_id'], reply['id'])

    def test_native_generated_question_routes_to_hub_without_model_user_authority(self):
        for kind in ('claude', 'pi'):
            origin = self.managed(kind); d = self.create(origin)
            delivery = mailbox.send(target=origin, text='fixture-question:May I alter compatibility?',
                key='fixture-native-question:'+kind, task=str(d), contract_hash=runner.read(d/'state.json')['contract_hash'],
                source='Explicit isolated framework message')
            mailbox.pump(origin)
            with self.actor(HUB):
                q = next(q for q in interaction.inbox() if q['workflow'] == str(d))
                self.assertEqual(q['question'], 'May I alter compatibility?')
                self.assertIsNone(q['answer'])
            self.answer(q); mailbox.pump(origin)
            with self.actor(origin):
                interaction.accept(q['id'])
            control.tick()
            self.assertEqual(runner.read(d/'state.json')['status'], 'blocked')
            self.assertEqual(mailbox.bus().message(delivery['mailbox_id'])['state'], 'replied')

    def test_expired_pending_delivery_and_disabled_recovery_do_not_start_cli(self):
        origin = self.managed('pi'); d = self.create(origin)
        delivery = mailbox.send(target=origin, text='Expired work', key='fixture-expiry', task=str(d),
            contract_hash=runner.read(d/'state.json')['contract_hash'], source='Isolated fixture authority',
            expires_at=runner.now()+10)
        MessageBus(mailbox.bus().path, clock=lambda: runner.now()+20).recover()
        self.assertIsNone(mailbox.pump(origin))
        self.assertEqual(mailbox.bus().message(delivery['mailbox_id'])['state'], 'expired')
        recovery.enable(d, 'Bounded fixture recovery')
        recovery.tick(d)
        request = runner.read(d/'recovery.json')['requests'][-1]
        recovery.disable(d)
        self.assertIsNone(mailbox.pump(origin))
        self.assertEqual(mailbox.bus().message(request['delivery']['mailbox_id'])['state'], 'cancelled')
        self.assertFalse(Path(mailbox._record(origin)['runtime']).exists())

    def test_stop_racing_native_result_records_late_receipts_without_relay(self):
        origin = self.managed('pi'); d = self.create(origin)
        q = self.ask(d, origin); answer = self.answer(q)
        from message_adapters import PiAdapter
        events = PiAdapter.events
        def stopped(adapter, **kwargs):
            result = events(adapter, **kwargs)
            (d/'STOP').touch()
            return result
        with patch.object(PiAdapter, 'events', stopped):
            mailbox.pump(origin)
        b = mailbox.bus(); mid = answer['answer_delivery']['mailbox_id']
        self.assertEqual(b.message(mid)['state'], 'cancelled')
        self.assertIsNone(mailbox.response(mid))
        self.assertTrue(any(r['late'] for r in b.receipts(mid)))
        before = self.calls.read_text(); control.tick(); control.tick()
        self.assertEqual(self.calls.read_text(), before)

    def test_native_origin_explicit_accept_event_finishes_actual_user_answer_flow(self):
        origin = self.managed('pi'); d = self.create(origin)
        mailbox.send(target=origin, text='fixture-question:Preserve compatibility?', key='fixture-auto-flow',
            task=str(d), contract_hash=runner.read(d/'state.json')['contract_hash'], source='Explicit fixture work')
        mailbox.pump(origin)
        with self.actor(HUB):
            q = next(q for q in interaction.inbox() if q['workflow'] == str(d))
            interaction.answer(q['id'], 'Keep compatibility; fixture-native-auto-accept', 'Actual user fixture reply')
        mailbox.pump(origin)
        self.assertEqual(runner.read(interaction.question_path(q['id']))['state'], 'accepted')
        control.tick()
        self.assertEqual(next(a for a in control.rows(True) if a['id'] == q['id'])['state'], 'awaiting_execution')
        self.assertEqual(runner.read(d/'state.json')['elapsed_seconds'], 271.5)

    def test_native_delegation_accept_event_does_not_start_another_workflow(self):
        origin = self.managed('claude')
        with patch('delegation.start_bridge'):
            result = delegation.delegate(self.repo, self.plan, 'Managed coordinator',
                'Explicit fixture-native-auto-delegation instruction', origin_endpoint=origin)
        mailbox.pump(origin)
        self.assertEqual(runner.read(Path(result['directory'])/'delegation.json')['state'], 'accepted')
        self.assertEqual(runner.read(Path(result['workflow_dir'])/'state.json')['status'], 'created')

    def test_mixed_managed_hub_and_pi_origin(self):
        hub = self.managed('claude'); origin = self.managed('pi')
        interaction.configure(hub, 'Fixture user explicitly moved future interaction to managed hub')
        d = self.create(origin); q = self.ask(d, origin)
        self.assertEqual(q['hub_thread'], hub)
        mailbox.pump(hub)
        with self.actor(hub):
            self.assertEqual(interaction.inbox()[0]['id'], q['id'])
            interaction.answer(q['id'], 'Preserve original acceptance', 'Actual managed-hub user reply')
        mailbox.pump(origin)
        with self.actor(origin):
            self.assertEqual(interaction.accept(q['id'])['answer'], 'Preserve original acceptance')
        self.assertEqual(runner.read(d/'state.json')['status'], 'blocked')

    def test_recovery_native_needs_input_round_trip_preserves_counters_and_limits(self):
        origin = self.managed('pi'); d = self.create(origin)
        recovery.enable(d, 'User explicitly authorized bounded fixture recovery', max_requests=2, response_seconds=120)
        initial = runner.read(d/'recovery.json')
        self.assertTrue(recovery.tick(d))
        request = runner.read(d/'recovery.json')['requests'][-1]
        mailbox.pump(origin)
        with self.actor(origin):
            routed = recovery.takeover(d, request['id'], 'needs_input', 'Keep compatibility?', recommendation='Keep it')
        q = routed['interaction']; self.answer(q); mailbox.pump(origin)
        with self.actor(origin):
            interaction.accept(q['id'])
        recovery.disable(d)
        recovery.enable(d, 'Explicit re-enable without extra grants', max_requests=99, window_seconds=99999)
        after = runner.read(d/'recovery.json')
        self.assertEqual(after['max_requests'], 2)
        self.assertEqual(after['deadline'], initial['deadline'])
        self.assertEqual(len(after['requests']), 1)
        self.assertEqual(after['requests'][0]['state'], 'needs_input')
        self.assertFalse(recovery.tick(d))
        self.assertEqual(runner.read(d/'state.json')['elapsed_seconds'], 271.5)

    def test_native_recovery_response_itself_routes_needs_input_with_no_new_grant(self):
        origin = self.managed('claude'); d = self.create(origin)
        state = runner.read(d/'state.json'); state['error'] = 'fixture-native-needs-input'
        runner.save(d/'state.json', state)
        recovery.enable(d, 'User explicitly authorized bounded fixture recovery', max_requests=2)
        recovery.tick(d); mailbox.pump(origin)
        ledger = runner.read(d/'recovery.json')
        self.assertEqual(ledger['requests'][-1]['state'], 'needs_input')
        self.assertEqual(ledger['max_requests'], 2)
        with self.actor(HUB):
            q = next(q for q in interaction.inbox() if q['workflow'] == str(d))
            self.assertEqual(q['request_id'], ledger['requests'][-1]['id'])
            self.assertIsNone(q['answer'])
        self.answer(q); mailbox.pump(origin)
        with self.actor(origin):
            interaction.accept(q['id'])
        self.assertFalse(recovery.tick(d))
        self.assertEqual(runner.read(d/'state.json')['elapsed_seconds'], 271.5)
        self.assertEqual(runner.read(d/'recovery.json')['deadline'], ledger['deadline'])

    def test_stop_before_pending_delivery_cancels_without_starting_native_cli(self):
        origin = self.managed(); d = self.create(origin)
        answer = self.answer(self.ask(d, origin)); mid = answer['answer_delivery']['mailbox_id']
        (d/'STOP').touch()
        self.assertIsNone(mailbox.pump(origin))
        self.assertEqual(mailbox.bus().message(mid)['state'], 'cancelled')
        with self.actor(origin), self.assertRaises(ValueError):
            interaction.accept(answer['id'])
        control.tick()
        self.assertEqual(next(a for a in control.rows(True) if a['id'] == answer['id'])['state'], 'stopped')

    def test_generation_contract_and_origin_changes_fence_pending_delivery(self):
        for change in ('generation', 'contract', 'origin'):
            with self.subTest(change=change):
                origin = self.managed(); d = self.create(origin)
                answer = self.answer(self.ask(d, origin))
                if change == 'generation':
                    s = runner.read(d/'state.json'); s['execution_revision'] = 1; runner.save(d/'state.json', s)
                elif change == 'contract':
                    c = runner.read(d/'contract.json'); c['scope'] += ' changed'; runner.save(d/'contract.json', c)
                else:
                    r = runner.read(d/'origin.json'); r['origin_endpoint_id'] = self.managed('pi'); runner.save(d/'origin.json', r)
                self.assertIsNone(mailbox.pump(origin))
                self.assertEqual(mailbox.bus().message(answer['answer_delivery']['mailbox_id'])['state'], 'cancelled')
                with self.actor(origin), self.assertRaises(ValueError):
                    interaction.accept(answer['id'])

    def test_paused_origin_no_native_send_reminder_or_automatic_recovery(self):
        origin = self.managed(); d = self.create(origin)
        q = self.ask(d, origin); control.tick(); self.answer(q)
        recovery.enable(d, 'Bounded fixture recovery')
        b = mailbox.bus(); b.set_endpoint_state(mailbox.credential(origin), 'paused', source='User explicitly deferred task')
        before = self.calls.read_text()
        with self.assertRaises(ValueError):
            mailbox.pump(origin)
        control.tick(); self.assertFalse(recovery.tick(d)); control.tick()
        self.assertEqual(self.calls.read_text(), before)
        self.assertEqual(next(a for a in control.rows(True) if a['id'] == q['id'])['state'], 'paused')
        self.assertEqual(runner.read(d/'state.json')['elapsed_seconds'], 271.5)
        with self.actor(HUB), self.assertRaises(ValueError):
            interaction.answer(q['id'], 'Different decision', 'A late answer')

    def test_crash_after_durable_send_barrier_never_replays(self):
        origin = self.managed(); d = self.create(origin)
        with patch.object(CodexQueueAdapter, 'send', side_effect=KeyboardInterrupt):
            with self.actor(origin), self.assertRaises(KeyboardInterrupt):
                interaction.ask(d, 'Crash window?', 'Do not resend')
        q = next((interaction.directory()/'questions').glob('*.json'))
        stored = runner.read(q)
        self.assertEqual(stored['question_delivery']['state'], 'dispatching')
        with patch.object(CodexQueueAdapter, 'send') as send:
            with self.actor(origin):
                self.assertEqual(interaction.ask(d, 'Crash window?', 'Do not resend')['id'], stored['id'])
            send.assert_not_called()
        with mailbox.bus()._transaction() as con:
            messages = con.execute('select id from messages').fetchall()
        self.assertEqual(len(messages), 1)
        self.assertEqual(mailbox.bus().message(messages[0]['id'])['state'], 'delivery_unknown')

    def test_unknown_native_codex_receipt_and_old_outbox_are_not_resent(self):
        d = self.create(LEGACY)
        with patch.dict(os.environ, FIXTURE_UNKNOWN='1'):
            q = self.ask(d, LEGACY)
        self.assertEqual(q['question_delivery']['state'], 'delivery_unknown')
        count = len(self.calls.read_text().splitlines())
        self.ask(d, LEGACY)
        self.assertEqual(len(self.calls.read_text().splitlines()), count)
        # Old records carry no mailbox ID. Reading/retrying does not migrate by sending.
        path = interaction.question_path(q['id']); old = runner.read(path)
        old['question_delivery'] = dict(state='queued', message_id=LEGACY, rollout_offset=0)
        runner.save(path, old); self.ask(d, LEGACY)
        self.assertEqual(len(self.calls.read_text().splitlines()), count)
        with self.actor(HUB):
            interaction.answer(q['id'], 'A', 'Actual user reply to old JSON')
        with self.actor(LEGACY):
            self.assertEqual(interaction.accept(q['id'])['state'], 'accepted')

    def test_wrong_managed_capability_project_and_session_are_rejected(self):
        origin = self.managed(); d = self.create(origin)
        for key in ('CTASK_ENDPOINT_TOKEN', 'CTASK_SESSION_ID', 'CTASK_PROJECT'):
            with self.actor(origin), patch.dict(os.environ, {key: 'wrong'}), self.assertRaises(ValueError):
                interaction.ask(d, 'Spoof?', '')
        other = self.root/'other'; other.mkdir()
        with self.assertRaises(ValueError):
            mailbox.identity(origin, project=other)
        with self.actor(HUB), self.assertRaises(ValueError):
            interaction.ask(d, 'Wrong actor?', '')
        with self.assertRaises(ValueError):
            workflow.create(self.repo, self.plan, LEGACY, origin_endpoint=origin)
        self.assertFalse(self.calls.exists())

    def test_managed_delegation_uses_same_mailbox_and_acceptance_binding(self):
        origin = self.managed('pi')
        with patch('delegation.start_bridge') as bridge:
            result = delegation.delegate(self.repo, self.plan, 'Managed fixture coordinator',
                'Explicit fixture user delegation', origin_endpoint=origin)
            again = delegation.delegate(self.repo, self.plan, 'Managed fixture coordinator',
                'Explicit fixture user delegation', origin_endpoint=origin)
            self.assertEqual(result['directory'], again['directory']); self.assertEqual(bridge.call_count, 1)
        d = Path(result['workflow_dir'])
        self.assertEqual(runner.read(d/'origin.json')['origin_endpoint_id'], origin)
        mailbox.pump(origin)
        with self.actor(origin):
            self.assertEqual(delegation.accept(result['directory'])['state'], 'accepted')
            with self.assertRaises(ValueError):
                delegation.accept(result['directory'])
        self.assertEqual(runner.read(d/'state.json')['status'], 'created')
        self.assertEqual(runner.read(d/'recovery.json')['requests'], [])

    def test_busy_codex_enqueue_is_persistent_and_explicit_pump_sends_once(self):
        d = self.create(LEGACY)
        mailbox.identity(HUB)
        b = mailbox.bus(); c = mailbox.credential(HUB)
        owner = b.acquire_endpoint(c, owner='Other owned foreground host')
        try:
            delivery = mailbox.send(target=HUB, text='A distinct authorized notification',
                key='fixture-pending-codex', task=str(d), contract_hash=runner.read(d/'state.json')['contract_hash'],
                source='Fixture explicit delivery')
            self.assertEqual(b.message(delivery['mailbox_id'])['state'], 'queued')
            self.assertFalse(self.calls.exists())
        finally:
            b.release_endpoint(c, owner)
        result = mailbox.pump(HUB)
        self.assertEqual(result['id'], delivery['mailbox_id'])
        self.assertEqual(result['state'], 'delivered')
        self.assertIsNone(mailbox.pump(HUB))
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(json.loads(self.calls.read_text())['cwd'], str(self.repo))

    def test_legacy_origin_json_budgets_and_contract_are_untouched(self):
        d = self.create(LEGACY)
        before = {name: (d/name).read_bytes() for name in ('origin.json', 'contract.json', 'state.json')}
        q = self.ask(d, LEGACY); self.answer(q)
        with self.actor(LEGACY):
            interaction.accept(q['id'])
        control.tick(); control.tick()
        for name, content in before.items():
            self.assertEqual((d/name).read_bytes(), content)
        self.assertEqual([json.loads(line)['argv'][0] for line in self.calls.read_text().splitlines()],
                         ['queue', 'queue', 'queue'])


if __name__ == '__main__':
    unittest.main()
