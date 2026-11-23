"""Read-only diagnostics and claim validation against isolated workflow fixtures."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import recovery
import runner
import test_recovery
from legacy_support import LEGACY_CLI


class TakeoverDiagnostics(unittest.TestCase):
    def setUp(self):
        self.f = test_recovery.RecoveryTests('runTest')
        self.f.setUp()
        with patch('recovery.subprocess.run', return_value=self.f.queued()):
            recovery.tick(self.f.d)
        self.q = self.f.ledger()['requests'][-1]

    def tearDown(self):
        self.f.tearDown()

    def diagnostic(self):
        return recovery.takeover_status(self.f.d, self.q['id'])

    def test_public_cli_is_read_only(self):
        before = (self.f.d/'recovery.json').read_bytes()
        result = subprocess.run([sys.executable, str(LEGACY_CLI), 'takeover-status', str(self.f.d),
                                 '--request', self.q['id']], env=self.f.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['eligible'])
        self.assertEqual(before, (self.f.d/'recovery.json').read_bytes())

    def test_cli_distinguishes_generation_and_stop_without_ledger_mutation(self):
        self.f.resumed_then_blocked()
        before = (self.f.d/'recovery.json').read_bytes()
        argv = [sys.executable, str(LEGACY_CLI), 'takeover-status', str(self.f.d), '--request', self.q['id']]
        result = subprocess.run(argv, env=self.f.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('generation_changed', json.loads(result.stdout)['reasons'])
        (self.f.d/'STOP').touch()
        result = subprocess.run(argv, env=self.f.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('stopped', json.loads(result.stdout)['reasons'])
        self.assertEqual(before, (self.f.d/'recovery.json').read_bytes())

    def test_resume_changes_generation(self):
        self.f.resumed_then_blocked()
        self.assertIn('generation_changed', self.diagnostic()['reasons'])
        with self.assertRaisesRegex(ValueError, 'generation_changed'):
            recovery.takeover(self.f.d, self.q['id'], 'working', 'late')

    def test_stop_distinct_from_expiration(self):
        (self.f.d/'STOP').touch()
        reasons = self.diagnostic()['reasons']
        self.assertIn('stopped', reasons)
        self.assertNotIn('expired', reasons)
        with self.assertRaisesRegex(ValueError, 'stopped'):
            recovery.takeover(self.f.d, self.q['id'], 'working', 'late')

    def test_expiry_is_explicit(self):
        r = self.f.ledger(); r['requests'][-1]['expires_at'] = 0
        runner.save(self.f.d/'recovery.json', r)
        self.assertIn('expired', self.diagnostic()['reasons'])

    def test_unknown_request(self):
        self.assertIn('unknown_request', recovery.takeover_status(self.f.d, 'missing')['reasons'])

    def test_live_owner_prevents_claim(self):
        s = runner.read(self.f.d/'state.json'); s['owner_pid'] = os.getpid()
        runner.save(self.f.d/'state.json', s)
        self.assertIn('owner_active', self.diagnostic()['reasons'])

    def test_binding_drift(self):
        origin = runner.read(self.f.d/'origin.json'); origin['origin_thread_id'] = 'other'
        runner.save(self.f.d/'origin.json', origin)
        self.assertIn('binding_changed', self.diagnostic()['reasons'])

    def test_handled_request(self):
        r = self.f.ledger(); r['requests'][-1]['state'] = 'resumed'
        runner.save(self.f.d/'recovery.json', r)
        self.assertIn('already_handled', self.diagnostic()['reasons'])


class OperationalFollowthrough(unittest.TestCase):
    def setUp(self):
        import test_interaction
        import interaction
        import control
        import notifications
        self.fixture_module = test_interaction
        self.f = test_interaction.InteractionTests('runTest'); self.f.setUp()
        with patch.dict(os.environ, CODEX_THREAD_ID=test_interaction.HUB):
            control.enable('fixture followthrough', 60, 0)
            notifications.configure('user_input_only', 'fixture quiet policy')
        control.register(self.f.d)
        self.rollout = self.f.home/(test_interaction.ORIGIN+'.jsonl')
        with self.rollout.open('a') as f:
            f.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'done'}})+'\n')

    def tearDown(self):
        self.f.tearDown()

    def test_diagnostic_delivery_is_once_only_and_response_is_not_progress(self):
        import control
        with patch('control.subprocess.run', side_effect=self.f.transport) as send:
            control.tick(); control.tick()
            self.assertEqual(send.call_count, 1)
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        self.assertEqual(action['state'], 'pending_operational')
        with self.rollout.open('a') as f:
            for payload in [dict(type='task_started',turn_id='t'),
                            dict(type='user_message',message=action['id']),
                            dict(type='task_complete',turn_id='t',last_agent_message='investigating')]:
                f.write(json.dumps({'type':'event_msg','payload':payload})+'\n')
        with patch('control.subprocess.run') as send:
            control.tick(); send.assert_not_called()
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        self.assertIn('origin_reply', action)
        self.assertEqual(action['state'], 'pending_operational')

    def test_active_origin_turn_stays_pending_without_send(self):
        import control
        with self.rollout.open('a') as f:
            f.write(json.dumps({'type':'event_msg','payload':{'type':'task_started','turn_id':'busy'}})+'\n')
        with patch('control.subprocess.run') as send:
            control.tick(); send.assert_not_called()
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        self.assertIn('active or activity unknown', action['reason'])

    def test_unknown_delivery_is_never_replayed(self):
        import control
        with patch('control.subprocess.run', side_effect=subprocess.TimeoutExpired('queue', 25)) as send:
            control.tick(); control.tick()
            self.assertEqual(send.call_count, 1)
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        self.assertEqual(action['origin_delivery']['state'], 'delivery_unknown')

    def test_stop_prevents_operational_delivery(self):
        import control
        (self.f.d/'STOP').touch()
        with patch('control.subprocess.run') as send:
            control.tick(); send.assert_not_called()
        self.assertFalse([a for a in control.rows(True) if a['kind'] == 'operational'])

    def test_specific_intervention_is_canonical_once_and_never_auto_answered(self):
        import control
        import interaction
        with patch('control.subprocess.run', side_effect=self.f.transport):
            control.tick()
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        evidence = self.f.repo/'diagnosis.txt'; evidence.write_text('fixture diagnosis')
        args = (action['id'], 'decision', 'Which reviewed option should be used?',
                'Checked existing constraints', [str(evidence)], 'Choose option A', 'actual diagnosis')
        with patch('interaction.subprocess.run', side_effect=self.f.transport) as send:
            first = control.operational_disposition(*args)
            second = control.operational_disposition(*args)
            self.assertEqual(first['id'], second['id']); self.assertEqual(send.call_count, 1)
        q = runner.read(interaction.question_path(first['id']))
        self.assertEqual(q['provenance'], 'controller_escalation')
        self.assertEqual(q['state'], 'pending'); self.assertNotIn('answer', q)
        evidence.write_text('tampered')
        with self.assertRaises(ValueError): interaction.validate_question(q)

    def test_unrelated_actor_cannot_create_intervention(self):
        import control
        with patch('control.subprocess.run', side_effect=self.f.transport): control.tick()
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        evidence = self.f.repo/'diagnosis.txt'; evidence.write_text('fixture diagnosis')
        with patch.dict(os.environ, CODEX_THREAD_ID='unrelated'):
            with self.assertRaises(ValueError):
                control.operational_disposition(action['id'], 'decision', 'Decision?', 'Investigated',
                                                [str(evidence)], 'Choose A', 'diagnosis')

    def test_repository_lock_and_live_owner_prevent_wakeup(self):
        import control
        with recovery.locked(self.f.repo/'.codex/tasks/.worker.lock'):
            with patch('control.subprocess.run') as send:
                control.tick(); send.assert_not_called()
        state = runner.read(self.f.d/'state.json'); state['owner_pid'] = os.getpid()
        runner.save(self.f.d/'state.json', state)
        with patch('control.subprocess.run') as send:
            control.tick(); send.assert_not_called()

    def test_origin_equal_to_hub_is_never_queued(self):
        import control
        import interaction
        import notifications
        with patch.dict(os.environ, CODEX_THREAD_ID=self.fixture_module.ORIGIN):
            interaction.configure(self.fixture_module.ORIGIN, 'fixture same conversation')
            notifications.configure('user_input_only', 'fixture quiet')
        state = runner.read(self.f.d/'state.json')
        contract = runner.read(self.f.d/'contract.json')
        row = dict(origin=self.fixture_module.ORIGIN, hub=self.fixture_module.ORIGIN,
                   contract_hash=state['contract_hash'])
        with patch('control.subprocess.run') as send:
            control.operational_followthrough(row, self.f.d, state, contract, recovery.generation(self.f.d, state))
            send.assert_not_called()
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        self.assertIn('origin_is_hub', action['reason'])

    def test_exhausted_handler_completed_turn_without_progress_remains_pending(self):
        import control
        request = dict(id='recover-fixture', state='stalled', message_id='receipt',
                       expires_at=0, rollout_offset=self.rollout.stat().st_size)
        runner.save(self.f.d/'recovery.json', dict(thread_id=self.fixture_module.ORIGIN,
            contract_hash=runner.read(self.f.d/'state.json')['contract_hash'],
            rollout=str(self.rollout), requests=[request], enabled=True, status='budget_exhausted'))
        with self.rollout.open('a') as f:
            for payload in [dict(type='user_message',message='recover-fixture'), dict(type='task_complete')]:
                f.write(json.dumps({'type':'event_msg','payload':payload})+'\n')
        with patch('control.subprocess.run', side_effect=self.f.transport) as send:
            control.tick(); control.tick(); self.assertEqual(send.call_count, 1)
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        self.assertEqual(action['state'], 'pending_operational')
        self.assertIn('turn_completed_at', action['observed_request'])

    def test_live_supervisor_is_a_handler_not_a_missing_handler(self):
        import control
        runner.save(self.f.d/'supervisor.json', {'pid':os.getpid()})
        with patch('control.subprocess.run') as send:
            control.tick(); send.assert_not_called()
        action = next(a for a in control.rows(True) if a['kind'] == 'operational')
        self.assertIn('remains active', action['reason'])
