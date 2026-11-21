import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_interaction as fixtures
from test_interaction import ORIGIN, HUB
import control
import interaction
import recovery
import runner
import workflow


class ControlTests(unittest.TestCase):
    setUp = fixtures.InteractionTests.setUp
    tearDown = fixtures.InteractionTests.tearDown
    transport = fixtures.InteractionTests.transport

    def prepare(self):
        with patch('interaction.subprocess.run', side_effect=self.transport):
            q = interaction.ask(self.d, 'Choose A?', 'A')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.enable('Fixture user asked for persistent follow-up', 60, 2)
        return q

    def action(self, identifier):
        return next(x for x in control.rows(True) if x['id'] == identifier)

    def tick(self):
        with patch('control.subprocess.run', side_effect=self.transport):
            control.tick()

    def verify_running_progress(self, directory, question_id):
        evidence = self.repo/'verified-resume.log'; evidence.write_text('first')
        s=runner.read(directory/'state.json')
        s.update(status='running',owner_pid=os.getpid(),active_process=dict(
            pid=os.getpid(),argv=['fixture-running-check'],evidence=str(evidence)))
        runner.save(directory/'state.json',s)
        self.tick()
        self.assertEqual(self.action(question_id)['state'],'awaiting_execution')
        evidence.write_text('first\nprogress')
        self.tick()

    def test_durable_lifecycle_requires_real_execution_not_accept_or_presentation(self):
        q = self.prepare()
        self.tick()
        self.assertEqual(self.action(q['id'])['state'], 'unpresented')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            interaction.inbox()
            self.assertNotIn('presented_at', self.action(q['id']))
            control.record(q['id'], state='presented', note='Actually explained in hub at 12:00')
            with patch('interaction.subprocess.run', side_effect=self.transport):
                interaction.answer(q['id'], 'A', 'Actual user reply')
        self.tick()
        self.assertEqual(self.action(q['id'])['state'], 'awaiting_acceptance')
        interaction.accept(q['id'])
        self.tick()
        self.assertEqual(self.action(q['id'])['state'], 'awaiting_execution')
        # Process restarts reconstruct from canonical sources and persistent projection.
        with control.database() as con:
            self.assertGreater(con.execute('select count(*) from events').fetchone()[0], 3)
        with (self.d/'events.jsonl').open('a') as f:
            f.write(json.dumps(dict(event='workflow_started', time=runner.now()))+'\n')
        self.tick()
        self.assertEqual(self.action(q['id'])['state'], 'awaiting_execution')
        self.verify_running_progress(self.d, q['id'])
        self.assertEqual(self.action(q['id'])['state'], 'resolved')
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'running')

    def test_presented_is_hub_only_and_does_not_answer(self):
        q = self.prepare(); self.tick()
        with self.assertRaises(ValueError):
            control.record(q['id'], state='presented', note='wrong actor')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.record(q['id'], state='presented', note='Actual hub explanation')
        self.tick()
        self.assertEqual(self.action(q['id'])['state'], 'needs_user')
        self.assertIsNone(runner.read(interaction.question_path(q['id'])).get('answer'))

    def test_bounded_reminders_survive_restart_and_never_manufacture_answer(self):
        q = self.prepare()
        moment = runner.now()
        with patch('control.subprocess.run', side_effect=self.transport) as send:
            for delta in [0, 10, 61, 65, 122, 183, 999]:
                with patch('control.core.now', return_value=moment+delta):
                    control.tick()
            self.assertEqual(send.call_count, 3)  # one initial and two reminders
        self.assertEqual(self.action(q['id'])['reminders'], 2)
        self.assertEqual(runner.read(interaction.question_path(q['id']))['state'], 'pending')

    def test_unknown_send_and_dispatch_crash_never_retry_even_as_reminder(self):
        q = self.prepare()
        with patch('control.subprocess.run', side_effect=subprocess.TimeoutExpired('queue', 25)) as send:
            control.tick()
            with patch('control.core.now', return_value=runner.now()+10000):
                control.tick()
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.action(q['id'])['notice']['state'], 'delivery_unknown')
        # Durable claim also survives interruption before receipt persistence.
        with patch('control.subprocess.run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                control.send('crash-key', HUB, 'fixture')
        with patch('control.subprocess.run') as send:
            self.assertEqual(control.send('crash-key', HUB, 'fixture')['state'], 'dispatching')
            send.assert_not_called()

    def test_correlated_turn_reply_is_relayed_once_and_not_completion(self):
        q = self.prepare()
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB), patch('interaction.subprocess.run', side_effect=self.transport):
            interaction.answer(q['id'], 'A', 'Actual user reply')
        interaction.accept(q['id'])
        rollout = self.home/(ORIGIN+'.jsonl')
        events = [('task_started', dict(turn_id='owned')),
                  ('user_message', dict(message='[ctask-user-answer id='+q['id']+']')),
                  ('agent_message', dict(message='Root cause found; not yet resumed')),
                  ('task_complete', dict(turn_id='owned', last_agent_message='Diagnosis finished; still blocked')),
                  ('task_started', dict(turn_id='unrelated')),
                  ('agent_message', dict(message='Unrelated secret must not relay')),
                  ('task_complete', dict(turn_id='unrelated'))]
        with rollout.open('a') as f:
            for kind, fields in events:
                f.write(json.dumps(dict(type='event_msg', payload=dict(type=kind, **fields)))+'\n')
        with patch('control.subprocess.run', side_effect=self.transport) as send:
            control.tick(); control.tick()
            relays = [c.args[0][-1] for c in send.call_args_list if '[ctask-origin-reply' in c.args[0][-1]]
            self.assertEqual(len(relays), 1)
            self.assertIn('still blocked', relays[0])
            self.assertNotIn('Unrelated secret', relays[0])
        self.assertEqual(self.action(q['id'])['state'], 'awaiting_execution')

    def test_native_response_item_message_correlates_with_answer(self):
        q = self.prepare()
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB), patch('interaction.subprocess.run', side_effect=self.transport):
            interaction.answer(q['id'], 'A', 'Actual reply')
        with (self.home/(ORIGIN+'.jsonl')).open('a') as f:
            for item in [dict(type='event_msg', payload=dict(type='task_started', turn_id='native')),
                         dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text=q['id'])])),
                         dict(type='response_item', payload=dict(type='message', role='assistant', content=[dict(type='output_text', text='Native final reply')])),
                         dict(type='event_msg', payload=dict(type='task_complete', turn_id='native'))]:
                f.write(json.dumps(item)+'\n')
        self.tick()
        self.assertEqual(self.action(q['id'])['origin_reply']['text'], 'Native final reply')

    def test_stop_and_stale_question_are_not_reopened_or_reminded(self):
        q = self.prepare(); self.tick()
        (self.d/'STOP').touch()
        with patch('control.subprocess.run') as send:
            control.tick(); control.tick(); send.assert_not_called()
        self.assertEqual(self.action(q['id'])['state'], 'stopped')

    def test_same_task_continuation_explicit_link_preserves_final_acceptance(self):
        q = self.prepare(); self.tick()
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB), patch('interaction.subprocess.run', side_effect=self.transport):
            interaction.answer(q['id'], 'Continue original task', 'Actual fixture instruction')
        interaction.accept(q['id'])
        new = workflow.create(self.repo, self.repo/'input.json', ORIGIN)
        proof = self.repo/'continuation.json'; proof.write_text('Preserved original budget history')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.record(q['id'], workflow=new, note='Actual linked continuation evidence', evidence=proof)
        with (new/'events.jsonl').open('a') as f:
            f.write(json.dumps(dict(event='workflow_started', time=runner.now()))+'\n')
        self.tick()
        self.assertEqual(self.action(q['id'])['state'], 'awaiting_execution')
        self.verify_running_progress(new, q['id'])
        self.assertEqual(self.action(q['id'])['state'], 'resolved')
        other = runner.read(self.repo/'input.json'); other['acceptance'][0]['checks'] = [['true']]
        runner.save(self.repo/'bad.json', other)
        bad = workflow.create(self.repo, self.repo/'bad.json', ORIGIN)
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            with self.assertRaises(ValueError):
                control.record(q['id'], workflow=bad, note='weakened', evidence=proof)

    def test_registered_blocked_workflow_is_visible_after_repair_supervisor_exits(self):
        q = self.prepare(); control.register(self.d)
        self.tick()
        generic = [a for a in control.rows(True) if a['kind'] == 'workflow']
        self.assertEqual(len(generic), 1)
        self.assertEqual(generic[0]['state'], 'superseded')
        self.assertTrue(any(a['id'] == q['id'] for a in control.rows()))

    def test_no_configuration_no_monitoring_or_sends(self):
        with patch('control.subprocess.run') as send:
            self.assertFalse(control.tick()); send.assert_not_called()
        with self.assertRaises(ValueError):
            control.enable('not in hub')

    def test_repeated_source_failure_keeps_outbox_and_does_not_spam(self):
        q = self.prepare(); control.register(self.d)
        c = runner.read(self.d/'contract.json'); c['objective'] = 'tampered'
        runner.save(self.d/'contract.json', c)
        with patch('control.subprocess.run', side_effect=self.transport) as send:
            control.tick(); first = send.call_count
            control.tick(); control.tick()
            self.assertEqual(send.call_count, first)
        self.assertTrue(any(a['state'] == 'source_error' for a in control.rows()))
        c['objective'] = 'A complete task'; runner.save(self.d/'contract.json', c)
        self.tick()
        self.assertFalse(any(a['state'] == 'source_error' for a in control.rows()))

    def test_recent_presentation_defers_first_controller_notice(self):
        q = self.prepare()
        control.reconcile_questions(control.settings())
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.record(q['id'], state='presented', note='Just explained to user')
        with patch('control.subprocess.run') as send:
            control.tick(); send.assert_not_called()

    def test_completed_workflow_has_separate_unreported_result(self):
        self.prepare(); control.register(self.d)
        s = runner.read(self.d/'state.json'); s['status'] = 'completed'; runner.save(self.d/'state.json', s)
        self.tick()
        result = next(a for a in control.rows() if a['kind'] == 'workflow_result')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.record(result['id'], state='presented', note='Actually reported final acceptance')
        self.tick()
        self.assertFalse(any(a['kind'] == 'workflow_result' for a in control.rows()))

    def test_real_daemon_restart_keeps_single_delivery_and_stops_cleanly(self):
        self.prepare()
        queue = self.repo/'queue'; calls = self.repo/'calls'
        queue.write_text(f'''#!{sys.executable}
import sys
with open({str(calls)!r},'a') as f:f.write('sent\\n')
target=sys.argv[sys.argv.index('--thread')+1]
print('Queued message '+target+' for thread '+target+'.')
'''); queue.chmod(0o755)
        with patch.dict(os.environ, CTASK_QUEUE_BIN=str(queue)):
            for i in range(2):
                p = subprocess.Popen([sys.executable, str(Path(runner.__file__).resolve()), '_controller'],
                    env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic()+5
                    while time.monotonic() < deadline:
                        state = control.status()
                        if state['process'].get('pid') == p.pid and state['process'].get('state') == 'watching':
                            break
                        time.sleep(.05)
                    else:
                        self.fail('daemon failed to reach watching')
                    self.assertTrue(state['process_alive'])
                finally:
                    p.terminate(); out, err = p.communicate(timeout=7)
                self.assertEqual(p.returncode, 0, err)
        self.assertEqual(calls.read_text(), 'sent\n')
        self.assertEqual(control.status()['process']['state'], 'exited')

    def test_concurrent_controllers_share_outbox(self):
        q = self.prepare()
        queue = self.repo/'queue'; calls = self.repo/'calls'
        queue.write_text(f'''#!{sys.executable}
import sys
with open({str(calls)!r},'a') as f:f.write('sent\\n')
target=sys.argv[sys.argv.index('--thread')+1]
print('Queued message '+target+' for thread '+target+'.')
'''); queue.chmod(0o755)
        env = dict(os.environ, CTASK_QUEUE_BIN=str(queue))
        cmd = [sys.executable, '-c', 'import control; control.tick()']
        processes = [subprocess.Popen(cmd, cwd=Path(control.__file__).parent, env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for p in processes:
            out, err = p.communicate(timeout=8)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(calls.read_text(), 'sent\n')

    def test_quiet_host_check_is_not_treated_as_model_stream(self):
        q = self.prepare()
        s = runner.read(self.d/'state.json')
        s.update(status='verifying', owner_pid=os.getpid(), active_process=dict(pid=os.getpid(),
            argv=['make', 'test'], evidence=str(self.repo/'quiet.log')))
        runner.save(self.d/'state.json', s)
        row = dict(workflow=str(self.d), origin=ORIGIN, hub=HUB, contract_hash=s['contract_hash'])
        control.workflow_observation(row)
        self.assertIsNone(row['active_key'])
        self.assertFalse(any(a['kind'] == 'watchdog' for a in control.rows(True)))


if __name__ == '__main__':
    unittest.main()
