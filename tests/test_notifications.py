import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_interaction as fixtures
from test_interaction import ORIGIN, HUB
import test_alerts
from ato_workflow import control
from ato_workflow import interaction
from ato_workflow import notifications
import recovery
import runner
from legacy_support import LEGACY_CLI


class NotificationPolicyTests(unittest.TestCase):
    setUp = fixtures.InteractionTests.setUp
    tearDown = fixtures.InteractionTests.tearDown
    transport = fixtures.InteractionTests.transport

    def enable(self):
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            notifications.configure('user_input_only', 'Actual fixture user: only interrupt for my input')
            control.enable('Existing fixture observation authorization', 60, 2)

    def tick(self):
        with patch('ato_workflow.control.subprocess.run', side_effect=self.transport) as send:
            control.tick()
        return [c.args[0] for c in send.call_args_list]

    def test_policy_requires_hub_actor_and_source_and_preserves_task_files(self):
        before = {p.name: p.read_bytes() for p in self.d.iterdir() if p.is_file()}
        with self.assertRaises(ValueError):
            notifications.configure('user_input_only', 'Not the hub')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            with self.assertRaises(ValueError):
                notifications.configure('user_input_only', '')
            with self.assertRaises(ValueError):
                notifications.configure('unknown', 'Actual user')
        self.enable()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.d.iterdir() if p.is_file()})
        self.assertFalse(recovery.summary(self.d)['enabled'])
        self.assertEqual(notifications.status()['mode'], 'user_input_only')

    def test_generic_status_is_silent_but_still_visible_and_not_resolved(self):
        self.enable(); control.register(self.d)
        self.assertEqual(self.tick(), [])
        with patch('ato_workflow.control.core.now', return_value=runner.now()+10000):
            self.assertEqual(self.tick(), [])
        action = next(a for a in control.rows() if a['kind'] == 'workflow')
        self.assertEqual(action['state'], 'unpresented')
        self.assertNotIn('presented_at', action)
        self.assertNotIn('notice', action)
        self.assertEqual(action['reminders'], 0)
        self.assertFalse(action['hub_notification_policy']['notify'])

    def test_completed_result_records_without_interrupt_or_fake_report(self):
        self.enable(); control.register(self.d)
        s = runner.read(self.d/'state.json'); s['status'] = 'completed'; runner.save(self.d/'state.json', s)
        self.assertEqual(self.tick(), [])
        a = next(a for a in control.rows() if a['kind'] == 'workflow_result')
        self.assertEqual(a['state'], 'unpresented')
        self.assertNotIn('presented_at', a)

    def test_host_handoff_still_reaches_origin_once_but_never_hub(self):
        self.enable()
        c = runner.read(self.d/'contract.json')
        c['host_handoff'] = dict(source='Existing fixture authorization', instructions='Origin host fixture only')
        runner.save(self.d/'contract.json', c)
        s = runner.read(self.d/'state.json')
        s.update(contract_hash=runner.digest(c), status='needs_host', host_handoff={'tasks': {'file': {'next_step': 'Install'}}})
        runner.save(self.d/'state.json', s); control.register(self.d)
        calls = self.tick()+self.tick()+self.tick()
        self.assertEqual([x[x.index('--thread')+1] for x in calls], [ORIGIN])
        a = next(a for a in control.rows() if a['kind'] == 'host_handoff')
        self.assertEqual(a['origin_delivery']['state'], 'queued')
        self.assertNotIn('notice', a)
        self.assertEqual(runner.read(self.d/'state.json'), s)

    def test_real_question_answer_and_acceptance_survive_quiet_policy(self):
        self.enable()
        with patch('ato_workflow.interaction.subprocess.run', side_effect=self.transport) as send:
            q = interaction.ask(self.d, 'Approve this exact new permission?', 'Keep old permissions')
            self.assertEqual(send.call_args.args[0][3], HUB)
            with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
                interaction.answer(q['id'], 'Keep old permissions', 'Actual user answer')
                self.assertEqual(send.call_args.args[0][3], ORIGIN)
        self.assertEqual(self.tick(), [])
        interaction.accept(q['id'])
        self.assertEqual(self.tick(), [])
        self.assertEqual(next(a for a in control.rows() if a['id'] == q['id'])['state'], 'awaiting_execution')
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_pending_question_keeps_bounded_reminders_and_stop_suppresses_them(self):
        self.enable()
        with patch('ato_workflow.interaction.subprocess.run', side_effect=self.transport):
            q = interaction.ask(self.d, 'Which actual user option?')
        calls = self.tick()
        self.assertTrue(calls)
        self.assertTrue(all(x[x.index('--thread')+1] == HUB for x in calls))
        self.assertEqual(runner.read(interaction.question_path(q['id']))['state'], 'pending')
        (self.d/'STOP').touch()
        with patch('ato_workflow.control.core.now', return_value=runner.now()+10000):
            self.assertEqual(self.tick(), [])
        self.assertTrue((self.d/'STOP').exists())

    def test_existing_status_notice_is_retained_but_not_reminded(self):
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.enable('Original observation', 60, 2)
        control.register(self.d); self.tick()
        old = next(a for a in control.rows() if a['kind'] == 'workflow')
        self.enable()
        with patch('ato_workflow.control.core.now', return_value=runner.now()+10000):
            self.assertEqual(self.tick(), [])
        a = next(a for a in control.rows() if a['id'] == old['id'])
        self.assertEqual(a['notice'], old['notice'])
        self.assertEqual(a['reminders'], old['reminders'])

    def test_origin_status_reply_is_retained_without_chat_delivery(self):
        self.enable()
        with patch('ato_workflow.interaction.subprocess.run', side_effect=self.transport):
            q = interaction.ask(self.d, 'Actual choice?')
            with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
                interaction.answer(q['id'], 'A', 'Actual user')
        interaction.accept(q['id']); self.tick()
        with control.database() as con, con:
            a = next(a for a in control.rows() if a['id'] == q['id'])
            a['origin_reply'] = dict(turn='fixture', text='Received; normal work continues')
            control.put(con, a)
        self.assertEqual(self.tick(), [])
        a = next(a for a in control.rows() if a['id'] == q['id'])
        self.assertEqual(a['origin_reply']['text'], 'Received; normal work continues')
        self.assertEqual(a['origin_reply']['delivery']['state'], 'recorded_only')
        self.assertNotIn('message_id', a['origin_reply']['delivery'])

    def test_automatic_supervisor_alert_keeps_ledger_without_user_interrupt(self):
        self.enable()
        before = test_alerts.AlertTests.prepare(self)
        with patch('recovery.subprocess.run', side_effect=self.transport) as send:
            recovery.tick(self.d); recovery.tick(self.d)
            send.assert_not_called()
        after = runner.read(self.d/'recovery.json')
        for key in ['deadline', 'max_requests', 'same_blocker_limit', 'response_seconds']:
            self.assertEqual(before[key], after[key])
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            a = interaction.inbox()[0]
        self.assertEqual(a['kind'], 'supervision_alert')
        self.assertEqual(a['state'], 'pending')
        self.assertEqual(a['question_delivery']['state'], 'recorded_only')
        self.assertEqual(self.tick(), [])

    def ledger(self):
        return runner.read(self.d/'recovery.json')

    def test_recovery_needs_user_input_is_still_delivered(self):
        self.enable(); recovery.enable(self.d, 'Original automatic recovery authorization')
        with patch('recovery.subprocess.run', side_effect=self.transport) as send:
            recovery.tick(self.d)
            request = runner.read(self.d/'recovery.json')['requests'][-1]['id']
            result = recovery.takeover(self.d, request, 'needs_input', 'Need your actual acceptance?')
        self.assertEqual([c.args[0][3] for c in send.call_args_list], [ORIGIN, HUB])
        self.assertEqual(result['interaction']['question_delivery']['state'], 'queued')

    def test_real_cli_policy_persists_and_does_not_follow_new_hub(self):
        env = dict(os.environ, CODEX_THREAD_ID=HUB)
        cli = [sys.executable, str(LEGACY_CLI), 'notification-policy']
        r = subprocess.run(cli+['set', '--mode', 'user_input_only', '--source', 'Actual fixture user'],
                           env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(r.stdout)['mode'], 'user_input_only')
        r = subprocess.run(cli+['status'], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(r.stdout)['mode'], 'user_input_only')
        interaction.configure(ORIGIN, 'Explicit new hub binding')
        self.assertFalse(notifications.status()['configured'])


if __name__ == '__main__':
    unittest.main()
