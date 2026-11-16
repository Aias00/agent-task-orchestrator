import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_interaction as fixtures
from test_interaction import ORIGIN, HUB
import interaction
import recovery
import runner

ROOT = Path(__file__).resolve().parents[1]


class AlertTests(unittest.TestCase):
    setUp = fixtures.InteractionTests.setUp
    tearDown = fixtures.InteractionTests.tearDown
    transport = fixtures.InteractionTests.transport

    def ledger(self):
        return runner.read(self.d/'recovery.json')

    def prepare(self, state='working'):
        recovery.enable(self.d, 'Fixture user enabled automatic supervision')
        with patch('recovery.subprocess.run', side_effect=self.transport):
            recovery.tick(self.d)
        r = self.ledger()
        r['requests'][-1].update(state=state, note='Fixture diagnosis', expires_at=0)
        runner.save(self.d/'recovery.json', r)
        return r

    def inbox(self):
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            return interaction.inbox()

    def test_expired_claim_routes_once_and_preserves_all_budgets(self):
        before = self.prepare()
        with patch('recovery.subprocess.run', side_effect=self.transport) as send:
            self.assertFalse(recovery.tick(self.d))
            for _ in range(3):
                self.assertFalse(recovery.tick(self.d))
            recovery.enable(self.d, 'Re-enable only', max_requests=99, window_seconds=99999)
            recovery.tick(self.d)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0][3], HUB)
        r = self.ledger()
        self.assertEqual(r['status'], 'stalled')
        for key in ['deadline', 'max_requests', 'same_blocker_limit', 'response_seconds']:
            self.assertEqual(before[key], r[key])
        self.assertEqual(len(r['requests']), 1)
        q = self.inbox()[0]
        self.assertEqual(q['kind'], 'supervision_alert')
        self.assertEqual(q['recovery_state'], 'stalled')
        self.assertNotIn('stale_reason', q)
        self.assertEqual(recovery.summary(self.d)['latest']['notification']['id'], q['id'])
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_failed_origin_delivery_notifies_hub_without_resending_origin(self):
        recovery.enable(self.d, 'Fixture user authorization')
        def send(argv, **kwargs):
            if argv[3] == ORIGIN:
                raise subprocess.TimeoutExpired('queue', 25)
            return self.transport(argv, **kwargs)
        with patch('recovery.subprocess.run', side_effect=send) as transport:
            recovery.tick(self.d)
            recovery.tick(self.d)
            self.assertEqual([c.args[0][3] for c in transport.call_args_list], [ORIGIN, HUB])
        self.assertEqual(self.inbox()[0]['recovery_state'], 'delivery_unknown')

    def test_unknown_alert_delivery_is_durable_and_never_retried(self):
        self.prepare()
        with patch('recovery.subprocess.run', side_effect=subprocess.TimeoutExpired('queue', 25)) as send:
            recovery.tick(self.d)
            recovery.tick(self.d)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.inbox()[0]['question_delivery']['state'], 'delivery_unknown')
        self.assertEqual(self.ledger()['requests'][-1]['notification']['question_delivery']['state'],
                         'delivery_unknown')

    def test_crash_during_alert_dispatch_is_not_replayed(self):
        self.prepare()
        with patch('recovery.subprocess.run', side_effect=KeyboardInterrupt) as send:
            with self.assertRaises(KeyboardInterrupt):
                recovery.tick(self.d)
            self.assertEqual(send.call_count, 1)
        with patch('recovery.subprocess.run') as send:
            recovery.tick(self.d)
            send.assert_not_called()
        self.assertEqual(self.inbox()[0]['question_delivery']['state'], 'dispatching')

    def assert_terminal_alert(self, state):
        self.prepare(state)
        with patch('recovery.subprocess.run', side_effect=self.transport) as send:
            recovery.tick(self.d)
            recovery.tick(self.d)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.inbox()[0]['recovery_state'], state)
        self.assertNotIn('stale_reason', self.inbox()[0])

    def test_blocked_is_visible(self):
        self.assert_terminal_alert('blocked')

    def test_resume_failure_is_visible(self):
        self.assert_terminal_alert('resume_failed')

    def test_send_failure_is_visible(self):
        self.assert_terminal_alert('send_failed')

    def test_same_hub_and_origin_still_queues_background_alert(self):
        interaction.configure(ORIGIN, 'Fixture user selected origin as hub')
        self.prepare()
        with patch('recovery.subprocess.run', side_effect=self.transport) as send:
            recovery.tick(self.d)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0][3], ORIGIN)
        self.assertEqual(interaction.inbox()[0]['question_delivery']['state'], 'queued')

    def test_stop_disable_or_changed_binding_never_alert(self):
        self.prepare()
        original = runner.read(self.d/'origin.json')
        contract = runner.read(self.d/'contract.json')
        with patch('recovery.subprocess.run') as send:
            (self.d/'STOP').touch()
            recovery.tick(self.d)
            (self.d/'STOP').unlink()
            recovery.disable(self.d)
            recovery.tick(self.d)
            recovery.enable(self.d, 'Resume supervision fixture')
            runner.save(self.d/'origin.json', dict(original, origin_thread_id=HUB))
            recovery.tick(self.d)
            runner.save(self.d/'origin.json', original)
            runner.save(self.d/'contract.json', dict(contract, objective='changed'))
            recovery.tick(self.d)
            send.assert_not_called()
        self.assertEqual(self.inbox(), [])

    def test_changed_repair_proof_alerts_without_launching(self):
        r = self.prepare('repaired')
        proof = self.repo/'repair.md'; proof.write_text('Changed proof')
        r['requests'][-1].update(expires_at=runner.now()+60, repair_evidence=str(proof), repair_checksum='wrong')
        runner.save(self.d/'recovery.json', r)
        with patch('recovery.subprocess.run', side_effect=self.transport) as send:
            recovery.tick(self.d)
            self.assertEqual(send.call_count, 1)
            self.assertIn('--thread', send.call_args.args[0])
        self.assertEqual(self.inbox()[0]['recovery_state'], 'stalled')

    def test_user_reply_returns_to_origin_and_does_not_renew_old_request(self):
        before = self.prepare()
        with patch('recovery.subprocess.run', side_effect=self.transport) as send:
            recovery.tick(self.d)
            q = self.inbox()[0]
            with self.assertRaises(ValueError):
                interaction.answer(q['id'], 'Continue', 'wrong conversation')
            with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
                interaction.answer(q['id'], 'Continue within original scope', 'Actual fixture user reply')
                with self.assertRaises(ValueError):
                    interaction.accept(q['id'])
            self.assertEqual(send.call_args.args[0][3], ORIGIN)
            result = interaction.accept(q['id'])
            self.assertEqual(result['state'], 'accepted')
            with self.assertRaises(ValueError):
                recovery.takeover(self.d, result['request_id'], 'repaired', 'late')
        after = self.ledger()
        self.assertEqual(after['deadline'], before['deadline'])
        self.assertEqual(after['requests'][-1]['expires_at'], 0)
        self.assertEqual(after['requests'][-1]['state'], 'stalled')
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_resumption_rejects_stale_alert_answers(self):
        self.prepare()
        with patch('recovery.subprocess.run', side_effect=self.transport):
            recovery.tick(self.d)
            q = self.inbox()[0]
        s = runner.read(self.d/'state.json')
        s.update(execution_revision=1, owner_pid=os.getpid(), status='running')
        runner.save(self.d/'state.json', s)
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB), patch('recovery.subprocess.run') as send:
            self.assertIn('stale_reason', interaction.inbox()[0])
            with self.assertRaises(ValueError):
                interaction.answer(q['id'], 'Continue', 'Reply after recovery')
            send.assert_not_called()

    def test_missing_hub_is_explicit_and_does_not_crash_supervision(self):
        self.prepare()
        (interaction.directory()/'hub.json').unlink()
        with patch('recovery.subprocess.run') as send:
            self.assertFalse(recovery.tick(self.d))
            send.assert_not_called()
        note = recovery.summary(self.d)['latest']['notification']
        self.assertFalse(note['routed'])
        self.assertIn('hub', note['reason'])

    def test_real_supervisor_process_exits_only_after_persisted_hub_notification(self):
        self.prepare()
        queue = self.repo/'queue-fixture'
        calls = self.repo/'transport.jsonl'
        queue.write_text(f'''#!{sys.executable}
import sys,json
with open({str(calls)!r},'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
target=sys.argv[sys.argv.index('--thread')+1]
print('Queued message '+target+' for thread '+target+'.')
''')
        queue.chmod(0o755)
        env = dict(self.env, CTASK_QUEUE_BIN=str(queue))
        command = [sys.executable, str(ROOT/'runner.py'), '_supervisor', str(self.d)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        watch = runner.read(self.d/'supervisor.json')
        self.assertIsNone(watch['pid'])
        self.assertIsNone(watch['error'])
        q = self.inbox()[0]
        self.assertEqual(q['question_delivery']['state'], 'queued')
        # Concurrent rechecks after a restart also share the durable outbox.
        command = [sys.executable, '-c', f'import recovery; recovery.tick({str(self.d)!r})']
        procs = [subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for _ in range(2)]
        for proc in procs:
            _, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
        rows = [json.loads(x) for x in calls.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][rows[0].index('--thread')+1], HUB)


if __name__ == '__main__':
    unittest.main()
