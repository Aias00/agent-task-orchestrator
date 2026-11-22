import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import budget
import runner
import workflow
import test_workflow as fixtures


class BudgetTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    criterion = fixtures.WorkflowTests.criterion
    contract = fixtures.WorkflowTests.contract
    create = fixtures.WorkflowTests.create

    def forecast(self, seconds=10, upper=20):
        proof = self.repo/'measurements.txt'
        proof.write_text('Fixture: original real check took 10 seconds; remaining repair bounded by 20')
        return dict(items=[dict(id='remaining', description='Finish remaining implementation and original checks',
                    seconds=seconds, upper_seconds=upper, basis='Actual fixture check sample plus bounded repair',
                    evidence=[str(proof)])], risk_percent=25)

    def prepared(self, runtime=5):
        c = self.contract(keys=('first',))
        c['limits']['runtime_seconds'] = runtime
        return self.create(c)

    def configure(self, d, seconds=10, upper=20):
        return budget.configure(d, dict(source='Actual fixture authorization for task-based reserve',
                                estimate=self.forecast(seconds, upper)), request='fixture-policy')

    def successful_check(self, d, argv=None, label='one'):
        w = workflow.Workflow(d)
        log = str(d/'evidence'/('first-'+label+'-check-0.log'))
        w.event('command_started', argv=argv or ['actual-check','first'], evidence=log)
        w.event('command_finished', code=0, evidence=log)

    def test_estimate_is_task_specific_and_rejects_unbounded_or_unmeasured_inputs(self):
        self.assertEqual(budget.estimate(self.forecast())['upper_seconds'], 25)
        self.assertEqual(budget.estimate(self.forecast(70, 100))['upper_seconds'], 125)
        for f in [dict(items=[]), self.forecast(-1, 2), self.forecast(3, 2), self.forecast(1, float('inf'))]:
            with self.subTest(f=f), self.assertRaises(ValueError): budget.estimate(f)
        f=self.forecast();f['items'][0]['evidence']=[]
        with self.assertRaises(ValueError):budget.estimate(f)

    def test_legacy_tasks_keep_their_fixed_limits(self):
        d=self.prepared()
        self.assertEqual(budget.summary(d)['hard_seconds'],5)
        self.assertEqual(budget.summary(d)['mode'],'fixed')
        self.assertFalse((d/'budget.json').exists())

    def test_policy_preserves_frozen_contract_elapsed_and_all_history(self):
        d=self.prepared();before=(d/'contract.json').read_bytes()
        s=runner.read(d/'state.json');s['elapsed_seconds']=7;runner.save(d/'state.json',s)
        ledger=self.configure(d)
        self.assertEqual(ledger['soft_seconds'],17)
        self.assertEqual(ledger['hard_seconds'],32)
        self.assertEqual((d/'contract.json').read_bytes(),before)
        self.assertEqual(runner.read(d/'state.json')['elapsed_seconds'],7)
        self.assertEqual(budget.summary(d)['used_seconds'],7)
        self.assertEqual(len(ledger['history']),1)

    def test_configuration_is_idempotent_and_conflicting_retry_rejected(self):
        d=self.prepared();a=self.configure(d);b=self.configure(d)
        self.assertEqual(a,b)
        with self.assertRaises(ValueError):self.configure(d,12,25)

    def test_frozen_budget_initializes_but_stage_cannot_mint_a_second_envelope(self):
        c=self.contract(keys=('first',));c['budget']=dict(source='Task fixture approval',estimate=self.forecast())
        d=self.create(c);self.assertEqual(budget.summary(d)['mode'],'adaptive')
        w=workflow.Workflow(d);w.stage(c['stages'][0]['plan'],['first'])
        stage=Path(w.state['active_run'])
        self.assertNotIn('budget',runner.read(stage/'plan.json'))
        self.assertFalse((stage/'budget.json').exists())

    def test_only_new_independently_passing_checks_release_reserve(self):
        d=self.prepared();self.configure(d)
        w=workflow.Workflow(d);w._accounted_at=100;w.state['elapsed_seconds']=9
        with patch.object(runner,'now',return_value=100):budget.poll(w)
        self.assertEqual(budget.summary(d)['soft_seconds'],10)
        self.successful_check(d)
        with patch.object(runner,'now',return_value=102):budget.poll(w)
        s=budget.summary(d)
        self.assertEqual(s['soft_seconds'],25)
        self.assertEqual(s['extensions'],1)
        # Same command rerun under a different evidence filename is not new progress.
        sig=budget.progress_signature(d);self.successful_check(d,label='two')
        self.assertEqual(budget.progress_signature(d),sig)

    def test_output_alone_and_worker_claims_do_not_release_reserve(self):
        d=self.prepared();self.configure(d)
        w=workflow.Workflow(d);w.state['elapsed_seconds']=10;w._accounted_at=100
        w.event('task_result',status='completed',summary='Model says done')
        with patch.object(runner,'now',return_value=100):budget.poll(w)
        self.assertEqual(budget.summary(d)['soft_seconds'],10)
        self.assertIn('budget_review_required',(d/'events.jsonl').read_text())

    def test_reforecast_inside_existing_cap_does_not_need_another_budget_grant(self):
        d=self.prepared();self.configure(d)
        s=runner.read(d/'state.json');s['elapsed_seconds']=11;runner.save(d/'state.json',s)
        ledger=budget.reforecast(d,self.forecast(6,10),'Origin actual evidence review','reforecast-1')
        self.assertEqual(ledger['soft_seconds'],17)
        self.assertEqual(ledger['hard_seconds'],25)
        self.assertEqual(ledger['extensions'],0)
        with self.assertRaises(ValueError):
            budget.reforecast(d,self.forecast(30,40),'Not enough remaining reserve','reforecast-2')

    def test_host_charges_are_idempotent_and_count_before_renewal(self):
        d=self.prepared();self.configure(d)
        p=self.repo/'host-evidence';p.write_text('Measured origin recovery 4 seconds')
        a=budget.charge(d,4,'Actual host work',p,'host-1')
        b=budget.charge(d,4,'Actual host work',p,'host-1')
        self.assertEqual(a,b)
        self.assertEqual(budget.summary(d)['used_seconds'],4)
        with self.assertRaises(ValueError):budget.charge(d,8,'Actual host work',p,'host-1')

    def test_tampered_history_and_changed_contract_are_rejected(self):
        d=self.prepared();self.configure(d)
        data=runner.read(d/'budget.json');data['soft_seconds']=99999;runner.save(d/'budget.json',data)
        with self.assertRaises(ValueError):budget.summary(d)
        data['soft_seconds']=10;runner.save(d/'budget.json',data)
        c=runner.read(d/'contract.json');c['acceptance']=[];runner.save(d/'contract.json',c)
        with self.assertRaises(ValueError):budget.summary(d)

    def test_stop_or_busy_repo_cannot_be_bypassed_by_budget_configuration(self):
        import fcntl
        d=self.prepared();(d/'STOP').touch()
        with self.assertRaises(ValueError):self.configure(d)
        (d/'STOP').unlink()
        with (self.repo/'.codex/tasks/.worker.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.configure(d)

    def test_external_wait_pauses_parent_and_child_but_still_honors_stop(self):
        d=self.prepared();w=workflow.Workflow(d);w._accounted_at=100;w.deadline=105
        w.state['elapsed_seconds']=2
        clock=[101]
        with patch.object(runner,'now',side_effect=lambda:clock[0]):
            with budget.external_wait(w):
                clock[0]=1000
                w.guard()
        self.assertEqual(w.state['elapsed_seconds'],3)
        self.assertEqual(w.state['waiting_seconds'],899)
        self.assertEqual(w.deadline,1004)
        with patch.object(runner,'now',return_value=1001):
            with self.assertRaises(runner.Halt):
                with budget.external_wait(w):
                    (d/'STOP').touch();w.guard()

    def test_real_workflow_continues_past_soft_limit_when_independent_checks_advance(self):
        c=self.contract(keys=('first','second'))
        c['limits']['runtime_seconds']=1
        # Cross the soft boundary once. Stage/final/overall verification reuse
        # this exact check; repeating the delay tests storage/scheduler speed
        # against the hard cap instead of independent-progress renewal.
        c['acceptance'][1]['checks']=[[sys.executable,'-c',
            'from pathlib import Path; assert Path("second").exists(); import time; '
            'marker=Path("budget-delay-once"); '
            'time.sleep(0 if marker.exists() else 1.1); marker.touch()']]
        c['budget']=dict(source='Actual bounded fixture reserve authorization',estimate=self.forecast(1,6))
        (self.repo/'first').write_text('already implemented')
        d=self.create(c)
        env=dict(self.env,TEST_PLAN_MODE='normal')
        result=subprocess.run([sys.executable,str(Path(runner.__file__)),'run',str(d)],env=env,
                              capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        state=runner.read(d/'state.json')
        self.assertEqual(state['status'],'completed',state)
        self.assertGreater(state['elapsed_seconds'],1)
        self.assertGreater(budget.summary(d)['extensions'],0)
        self.assertEqual(budget.summary(d)['hard_seconds'],8)

    def test_real_external_wait_does_not_consume_the_active_runtime(self):
        import test_runner as fixture
        self.fake.write_text(fixture.FAKE.replace('retry_after_seconds=1','retry_after_seconds=4'))
        c=dict(objective='Wait without burning active budget',memory=False,
               limits=dict(runtime_seconds=3,attempt_seconds=10,check_seconds=2,max_attempts=3),
               tasks=[dict(id='wait-done',prompt='wait',checks=[[sys.executable,'-c',
                       'from pathlib import Path; assert Path("wait-done").exists()']])])
        p=self.repo/'standalone.json';runner.save(p,c);d=runner.create(self.repo,p)
        result=subprocess.run([sys.executable,str(Path(runner.__file__)),'run',str(d)],env=self.env,
                              capture_output=True,text=True,timeout=12)
        self.assertEqual(result.returncode,0,result.stderr)
        s=runner.read(d/'state.json')
        self.assertEqual(s['status'],'completed',s)
        self.assertGreater(s['waiting_seconds'],3.5)
        self.assertLess(s['elapsed_seconds'],3)

    def test_standalone_resume_does_not_reset_elapsed_time(self):
        c=dict(objective='No runtime reset',memory=False,tasks=[dict(id='a',prompt='a',checks=[['true']])],
               limits=dict(runtime_seconds=1))
        p=self.repo/'input-plan.json';runner.save(p,c);d=runner.create(self.repo,p)
        s=runner.read(d/'state.json');s['elapsed_seconds']=1;runner.save(d/'state.json',s)
        runner.Runner(d).execute(retry=True)
        s=runner.read(d/'state.json')
        self.assertEqual(s['status'],'limit')
        self.assertEqual(s['tasks']['a']['attempts'],0)

    def test_origin_command_is_metered_and_cannot_acknowledge_manual_gate(self):
        d=self.prepared(runtime=10)
        result=budget.host_run(d,[sys.executable,'-c','import time;time.sleep(.2)'],'Actual local host verification')
        self.assertEqual(result['code'],0)
        s=runner.read(d/'state.json')
        self.assertGreaterEqual(s['elapsed_seconds'],.2)
        self.assertNotEqual(s['status'],'completed')
        self.assertIsNone(s['owner_pid'])
        (d/'STOP').touch()
        with self.assertRaises(runner.Halt):budget.host_run(d,['true'],'Must not run')

    def test_host_command_stops_at_existing_fixed_hard_limit(self):
        d=self.prepared(runtime=1)
        with self.assertRaises(runner.Halt):
            budget.host_run(d,[sys.executable,'-c','import time;time.sleep(5)'],'Bounded local check')
        self.assertEqual(runner.read(d/'state.json')['status'],'limit')
        self.assertIsNone(runner.read(d/'state.json')['active_process'])


class BudgetControllerTests(unittest.TestCase):
    def test_origin_review_is_deduplicated_and_does_not_grant_repair_or_ask_user_again(self):
        import test_control as fixtures
        import control
        fixture=fixtures.ControlTests()
        fixture.setUp()
        try:
            fixture.prepare()
            proof=fixture.repo/'measurements';proof.write_text('Actual checks take 10 seconds')
            forecast=dict(items=[dict(id='finish',description='Finish existing checks',seconds=10,
                upper_seconds=30,basis='Actual check sample',evidence=[str(proof)])])
            budget.configure(fixture.d,dict(source='Actual task reserve authorization',estimate=forecast),request='one')
            s=runner.read(fixture.d/'state.json')
            soft=budget.summary(fixture.d)['soft_seconds']
            s.update(status='limit',elapsed_seconds=soft-1)
            runner.save(fixture.d/'state.json',s)
            with patch.object(control,'send',return_value=dict(state='queued',message_id='fixture')) as send:
                fixture.tick();fixture.tick()
                messages=[c for c in send.call_args_list if c.args[0].startswith('budget-review:')]
                self.assertEqual(len(messages),1)
            actions=[a for a in control.rows(True) if a['kind']=='budget_review']
            self.assertEqual(len(actions),1)
            self.assertEqual(actions[0]['notice']['state'],'origin_only')
            self.assertFalse((fixture.d/'recovery.json').exists())
            self.assertEqual(budget.summary(fixture.d)['extensions'],0)
        finally:
            fixture.tearDown()


if __name__=='__main__':unittest.main()
