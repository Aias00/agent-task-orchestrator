import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from ato_workflow import budget
from ato_workflow import control
from ato_workflow import interaction
import runner
from ato_workflow import workflow
import test_interaction as fixtures
from test_interaction import ORIGIN, HUB
from legacy_support import LEGACY_CLI


class BudgetStopHandoffTests(unittest.TestCase):
    setUp=fixtures.InteractionTests.setUp
    tearDown=fixtures.InteractionTests.tearDown
    transport=fixtures.InteractionTests.transport

    def prepare(self, manual=False):
        if manual:
            c=runner.read(self.repo/'input.json')
            c['acceptance'].append(dict(id='human',description='Actual user acceptance',kind='manual'))
            runner.save(self.repo/'manual.json',c)
            self.d=workflow.create(self.repo,self.repo/'manual.json',ORIGIN)
        with patch('ato_workflow.interaction.subprocess.run',side_effect=self.transport):
            q=interaction.ask(self.d,'Approve up to 50 more active minutes for this exact goal?',
                              'Use a task-specific bounded envelope; preserve all prior charges')
            with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
                interaction.answer(q['id'],'同意','Actual fixture hub user agrees to this bounded task extension')
        q=runner.read(interaction.question_path(q['id']))
        stop=self.d/'STOP';stop.touch()
        stamp=q['answered_at']+.1
        os.utime(stop,(stamp,stamp))
        proof=self.repo/'guard-receipt.json'
        runner.save(proof,dict(status='active_budget_stop_requested',deadline=stamp,
            observed_at=stamp+.02,outcomes=[dict(directory=str(self.d),exit=0)]))
        s=runner.read(self.d/'state.json')
        s.update(status='cleanup_blocked',elapsed_seconds=runner.read(self.d/'contract.json')['limits']['runtime_seconds'])
        runner.save(self.d/'state.json',s)
        self.q=q;self.proof=proof
        return q

    def forecast(self):
        return dict(items=[dict(id='finish',description='Remaining original checks and delivery',
            seconds=10,upper_seconds=20,basis='Actual isolated check timings',evidence=[str(self.proof)])])

    def grant(self):
        return budget.configure(self.d,dict(source='Actual recorded bounded user approval',estimate=self.forecast()),
            request='grant-1',hard_limit_seconds=10200,accepted_answer=self.q['id'])

    def accept(self):
        return interaction.accept(self.q['id'],budget_stop_evidence=self.proof)

    def test_normal_accept_still_rejects_stop_but_evidenced_accept_preserves_stop_and_state(self):
        self.prepare();before=(self.d/'state.json').read_bytes();contract=(self.d/'contract.json').read_bytes()
        with self.assertRaises(ValueError):interaction.accept(self.q['id'])
        accepted=self.accept()
        self.assertEqual(accepted['state'],'accepted')
        self.assertTrue((self.d/'STOP').exists())
        self.assertEqual(before,(self.d/'state.json').read_bytes())
        self.assertEqual(contract,(self.d/'contract.json').read_bytes())
        self.assertFalse((self.d/'budget.json').exists())

    def test_hub_cannot_accept_for_origin(self):
        self.prepare()
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB),self.assertRaises(ValueError):self.accept()

    def test_answer_after_stop_is_not_reused(self):
        self.prepare();p=interaction.question_path(self.q['id']);q=runner.read(p)
        q['answered_at']=(self.d/'STOP').stat().st_mtime+2;runner.save(p,q)
        with self.assertRaises(ValueError):self.accept()

    def test_missing_manual_or_unrelated_stop_evidence_is_rejected(self):
        self.prepare();original=runner.read(self.proof)
        for modify in [lambda p:p.update(status='user_stopped'),lambda p:p.update(outcomes=[]),
                       lambda p:p['outcomes'][0].update(directory=str(self.repo/'another')),
                       lambda p:p['outcomes'][0].update(exit=1)]:
            value=copy.deepcopy(original);modify(value);runner.save(self.proof,value)
            with self.subTest(value=value),self.assertRaises(ValueError):self.accept()

    def test_new_generation_or_contract_cannot_be_bypassed(self):
        self.prepare();p=self.d/'state.json';s=runner.read(p);s['execution_revision']=1;runner.save(p,s)
        with self.assertRaises(ValueError):self.accept()
        s['execution_revision']=0;runner.save(p,s)
        c=runner.read(self.d/'contract.json');c['scope']='Changed scope';runner.save(self.d/'contract.json',c)
        with self.assertRaises(ValueError):self.accept()

    def test_completed_or_paused_task_cannot_be_reopened(self):
        self.prepare()
        for status in ['completed','paused']:
            s=runner.read(self.d/'state.json');s['status']=status;runner.save(self.d/'state.json',s)
            with self.subTest(status=status),self.assertRaises(ValueError):self.accept()

    def test_accepted_answer_does_not_itself_grant_a_budget(self):
        self.prepare();self.accept()
        with self.assertRaises(ValueError):budget.check_approved_resume(self.d,self.q['id'])
        self.assertTrue((self.d/'STOP').exists())

    def test_explicit_cap_preserves_used_time_and_does_not_edit_frozen_contract_or_stop(self):
        self.prepare();self.accept();before=(self.d/'contract.json').read_bytes()
        ledger=self.grant()
        self.assertEqual(ledger['hard_seconds'],10200)
        self.assertEqual(budget.summary(self.d)['used_seconds'],7200)
        self.assertTrue((self.d/'STOP').exists())
        self.assertEqual((self.d/'contract.json').read_bytes(),before)
        self.assertEqual(self.grant(),ledger)
        self.assertFalse((self.d/'recovery.json').exists())
        budget.check_approved_resume(self.d,self.q['id'])

    def test_later_stop_or_changed_guard_proof_blocks_configuration_and_resume(self):
        self.prepare();self.accept();self.grant()
        original=runner.read(self.proof);runner.save(self.proof,dict(original,observed_at=original['observed_at']+.01))
        with self.assertRaises(ValueError):budget.check_approved_resume(self.d,self.q['id'])
        runner.save(self.proof,original)
        stop=self.d/'STOP';stamp=stop.stat().st_mtime+5;os.utime(stop,(stamp,stamp))
        with self.assertRaises(ValueError):self.grant()
        with self.assertRaises(ValueError):budget.check_approved_resume(self.d,self.q['id'])

    def test_accepted_stop_adoption_requires_explicit_cap_and_origin_identity(self):
        self.prepare();self.accept()
        with self.assertRaises(ValueError):
            budget.configure(self.d,dict(source='Actual user approval',estimate=self.forecast()),
                             request='missing-cap',accepted_answer=self.q['id'])
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB),self.assertRaises(ValueError):self.grant()

    def test_estimate_cannot_raise_explicit_approved_cap(self):
        self.prepare();self.accept();f=self.forecast();f['items'][0].update(seconds=10000,upper_seconds=20000)
        ledger=budget.configure(self.d,dict(source='Actual bounded user approval',estimate=f),
            request='bounded',hard_limit_seconds=10200,accepted_answer=self.q['id'])
        self.assertEqual(ledger['hard_seconds'],10200)
        self.assertEqual(ledger['soft_seconds'],10200)

    def test_supported_cli_resume_keeps_manual_gate_and_does_not_reset_old_usage(self):
        self.prepare(manual=True);self.accept();self.grant();(self.repo/'result').write_text('Actual fixture output')
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):control.enable('Actual fixture continuous hub follow-up')
        control.register(self.d)
        with patch('ato_workflow.control.subprocess.run',side_effect=self.transport):control.tick()
        self.assertEqual(next(a for a in control.rows(True) if a['id']==self.q['id'])['state'],'stopped')
        result=subprocess.run([sys.executable,str(LEGACY_CLI),'resume',str(self.d),
                              '--accepted-answer',self.q['id'],'--retry-failed'],
                              capture_output=True,text=True,env=self.env,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            s=runner.read(self.d/'state.json')
            if s['status']=='needs_input' and s['owner_pid'] is None:break
            time.sleep(.05)
        self.assertEqual(s['status'],'needs_input',s)
        self.assertEqual(s['criteria']['human']['status'],'needs_input')
        self.assertGreater(s['elapsed_seconds'],7200)
        self.assertFalse((self.d/'STOP').exists())
        self.assertEqual(runner.read(self.d/'launch.json')['approved_budget_answer'],self.q['id'])
        with patch('ato_workflow.control.subprocess.run',side_effect=self.transport):control.tick()
        # A fast ended run is not falsely labelled live recovery.
        self.assertEqual(next(a for a in control.rows(True) if a['id']==self.q['id'])['state'],'awaiting_execution')


if __name__=='__main__':unittest.main()
