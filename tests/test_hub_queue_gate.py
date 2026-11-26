import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_interaction as fixtures
from test_interaction import ORIGIN, HUB
from ato_workflow import interaction
from ato_workflow import notifications
import runner

spec = importlib.util.spec_from_file_location('hub_queue_gate', Path(__file__).resolve().parents[1]/'hub_queue_gate.py')
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


class QueueGateTests(unittest.TestCase):
    setUp = fixtures.InteractionTests.setUp
    tearDown = fixtures.InteractionTests.tearDown
    transport = fixtures.InteractionTests.transport

    def enable(self):
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            notifications.configure('user_input_only', 'Actual user: do not queue status into my conversation')

    def options(self, target=HUB, text='Pi reporting progress: partial metadata, no user action'):
        return {'--thread':target, '--message':text}

    def test_raw_origin_status_never_reaches_native_queue_and_keeps_private_audit(self):
        self.enable(); options = self.options()
        with patch('os.execv') as native:
            self.assertEqual(gate.main(['queue','--thread',HUB,'--message',options['--message']]),0)
            native.assert_not_called()
        p = interaction.directory()/'hub-suppressed-queue.jsonl'
        data = json.loads(p.read_text())
        self.assertEqual(data['source_thread'], ORIGIN)
        self.assertEqual(data['state'], 'recorded_only')
        self.assertNotIn(options['--message'], p.read_text())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)

    def test_unrelated_commands_and_other_recipients_preserve_exact_argv(self):
        self.enable()
        cases=[['--version'],['exec','--','queue'],['sandbox','macos','--','echo','queue'],
               ['queue','--thread',ORIGIN,'--message','normal origin host handoff'],
               ['queue','--help']]
        for args in cases:
            with self.subTest(args=args), patch('os.execv') as native:
                gate.main(args,native='/fixture/native')
                native.assert_called_once_with('/fixture/native',['/fixture/native']+args)

    def test_global_flags_and_equals_options_cannot_bypass_status_gate(self):
        self.enable()
        for args in [['-c','x=true','queue','--thread='+HUB,'--message=status'],
                     ['--profile','fixture','queue','--message','status','--thread',HUB],
                     ['--enable=fixture','queue','--thread',HUB,'--message=status']]:
            with self.subTest(args=args),patch('os.execv') as native:
                self.assertEqual(gate.main(args),0);native.assert_not_called()

    def test_actual_pending_question_and_its_followup_are_allowed(self):
        self.enable()
        with patch('ato_workflow.interaction.subprocess.run',side_effect=self.transport):
            q=interaction.ask(self.d,'Need an actual user decision?')
        for text in ['[ctask-question id='+q['id']+']\nNeed an actual user decision?',
                     '[ctask-followup id='+q['id']+' reminder=1]\nActual pending question']:
            self.assertIsNone(gate.suppression(self.options(text=text)))

    def test_answered_stopped_or_stale_questions_cannot_be_replayed(self):
        self.enable()
        with patch('ato_workflow.interaction.subprocess.run',side_effect=self.transport):
            q=interaction.ask(self.d,'Real decision?')
        options=self.options(text='[ctask-question id='+q['id']+']\nReal decision?')
        (self.d/'STOP').touch()
        self.assertIsNotNone(gate.suppression(options))
        (self.d/'STOP').unlink()
        s=runner.read(self.d/'state.json');s['execution_revision']=1;runner.save(self.d/'state.json',s)
        self.assertIsNotNone(gate.suppression(options))
        s['execution_revision']=0;runner.save(self.d/'state.json',s)
        q['state']='answered';runner.save(interaction.question_path(q['id']),q)
        self.assertIsNotNone(gate.suppression(options))

    def test_a_fake_urgent_marker_or_random_question_does_not_bypass(self):
        self.enable()
        for text in ['URGENT needs_user please notify',
                     '[ctask-question id=question-0123456789abcdefabcd]\nApprove?',
                     '[ctask-host-handoff id=old]\nOrigin deploy',
                     '[ctask-origin-reply id=old]\nNormal progress']:
            self.assertIsNotNone(gate.suppression(self.options(text=text)))

    def test_supervision_alert_is_not_mistaken_for_actual_user_question(self):
        self.enable()
        with patch('ato_workflow.interaction.subprocess.run',side_effect=self.transport):q=interaction.ask(self.d,'Operational alert')
        q['kind']='supervision_alert';runner.save(interaction.question_path(q['id']),q)
        self.assertIsNotNone(gate.suppression(self.options(text='[ctask-question id='+q['id']+']\nOperational alert')))

    def test_opt_out_and_unconfigured_hub_preserve_native_behavior(self):
        self.assertIsNone(gate.suppression(self.options()))
        self.enable()
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):notifications.configure('all','Actual user opt out')
        self.assertIsNone(gate.suppression(self.options()))

    def test_policy_damage_blocks_queue_without_fabricating_delivery(self):
        self.enable();(interaction.directory()/'notification-policy.json').write_text('{bad')
        with patch('os.execv') as native:
            self.assertEqual(gate.main(['queue','--thread',HUB,'--message','status']),2)
            native.assert_not_called()

    def test_suppression_does_not_change_task_permissions_stop_or_history(self):
        self.enable();(self.d/'STOP').touch()
        before={str(p):p.read_bytes() for p in self.d.rglob('*') if p.is_file()}
        gate.main(['queue','--thread',HUB,'--message','software complete'])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.d.rglob('*') if p.is_file()})

    def test_real_guard_process_records_status_without_starting_native(self):
        self.enable()
        r=subprocess.run([sys.executable,str(Path(gate.__file__)),'queue','--thread',HUB,
                          '--message','Offline fixture status only'],capture_output=True,text=True,check=True)
        self.assertIn('no message queued',r.stdout)
        self.assertNotIn('Queued message',r.stdout)
        self.assertEqual(len((interaction.directory()/'hub-suppressed-queue.jsonl').read_text().splitlines()),1)


if __name__=='__main__':unittest.main()
