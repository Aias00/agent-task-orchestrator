"""Observable continuation and guard behavior under user-selected advisory time."""
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
import test_budget_watchdog as watch_fixtures
from test_interaction import HUB
from legacy_support import LEGACY_CLI


class AdvisoryTimeTests(unittest.TestCase):
    criterion = fixtures.WorkflowTests.criterion
    contract = fixtures.WorkflowTests.contract
    create = fixtures.WorkflowTests.create

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.policy_env = patch.dict(os.environ, CODEX_HOME=str(self.repo/'local-home'))
        self.policy_env.start()
        self.env['CODEX_HOME'] = os.environ['CODEX_HOME']

    def tearDown(self):
        self.policy_env.stop()
        fixtures.WorkflowTests.tearDown(self)

    def advisory(self):
        return budget.set_time_policy('advisory', 'Actual user: do not stop on elapsed-time budget', 'advisory-v1')

    def prepared(self, runtime=1):
        c=self.contract(keys=('first',));c['limits']['runtime_seconds']=runtime
        return self.create(c)

    def test_old_exhausted_task_and_new_task_share_policy_without_rewriting_history(self):
        d=self.prepared();s=runner.read(d/'state.json');s['elapsed_seconds']=3
        runner.save(d/'state.json',s)
        proof=self.repo/'charge.txt';proof.write_text('Measured host work')
        budget.charge(d,2,'Measured host work',proof,'charge-v1')
        before={p.name:p.read_bytes() for p in [d/'contract.json',d/'state.json',d/'budget.json']}
        self.advisory()
        info=budget.summary(d)
        self.assertEqual(info['mode'],'advisory');self.assertEqual(info['used_seconds'],5)
        self.assertTrue(info['forecast_exceeded']);self.assertFalse(info['exhausted'])
        self.assertIsNone(info['remaining_seconds'])
        self.assertEqual(before,{name:(d/name).read_bytes() for name in before})
        self.assertEqual(budget.summary(self.prepared())['enforcement'],'advisory')

    def test_policy_is_durable_idempotent_and_conflicting_or_tampered_retries_fail(self):
        self.advisory()
        import interaction
        p=interaction.directory()/'time-policy.json';before=p.read_bytes()
        self.advisory();self.assertEqual(p.read_bytes(),before)
        with self.assertRaises(ValueError):budget.set_time_policy('enforced','Changed','advisory-v1')
        value=runner.read(p);value['mode']='enforced';runner.save(p,value)
        with self.assertRaises(ValueError):budget.time_policy()

    def test_policy_does_not_clear_stop_or_restore_processes(self):
        d=self.prepared();(d/'STOP').touch();before=(d/'state.json').read_bytes()
        self.advisory()
        self.assertTrue((d/'STOP').exists());self.assertEqual(before,(d/'state.json').read_bytes())
        with self.assertRaises(runner.Halt):budget.host_run(d,['true'],'Existing scope')

    def test_live_guard_adopts_policy_but_still_detects_contract_change(self):
        d=self.prepared();w=workflow.Workflow(d);w.deadline=0
        self.advisory();w.guard()
        c=runner.read(d/'contract.json');c['scope']='Changed';runner.save(d/'contract.json',c)
        with self.assertRaises(runner.Halt):w.guard()

    def test_real_host_command_continues_after_old_ceiling_and_keeps_accounting(self):
        d=self.prepared();self.advisory()
        result=budget.host_run(d,[sys.executable,'-c','import time;time.sleep(1.15)'],'Authorized host check')
        self.assertEqual(result['code'],0)
        self.assertGreater(budget.summary(d)['used_seconds'],1)
        self.assertIsNone(runner.read(d/'state.json')['active_process'])
        self.assertNotEqual(runner.read(d/'state.json')['status'],'completed')

    def test_real_workflow_and_child_continue_after_old_ceiling_and_preserve_manual_gate(self):
        c=self.contract(keys=('first','second'));c['limits']['runtime_seconds']=1
        c['acceptance'][0]['checks'][0][-1]+='; import time; time.sleep(1.1)'
        c['acceptance'].append(dict(id='human',description='Actual final user acceptance',kind='manual'))
        d=self.create(c);self.advisory()
        r=subprocess.run([sys.executable,str(LEGACY_CLI),'run',str(d)],env=self.env,
                         capture_output=True,text=True,timeout=20)
        self.assertEqual(r.returncode,0,r.stderr)
        s=runner.read(d/'state.json');self.assertEqual(s['status'],'needs_input',s)
        self.assertEqual(s['criteria']['second']['status'],'passed')
        self.assertEqual(s['criteria']['human']['status'],'needs_input')
        self.assertGreater(s['elapsed_seconds'],1)
        self.assertTrue(s['stages']);self.assertTrue(all(x['status']=='completed' for x in s['stages']))

    def test_real_standalone_run_is_not_cut_off_by_old_runtime(self):
        plan=dict(objective='Existing authorized check',memory=False,
            limits=dict(runtime_seconds=1,check_seconds=5),
            tasks=[dict(id='first',prompt='first',checks=[[sys.executable,'-c','import time;time.sleep(1.1)']])])
        p=self.repo/'standalone.json';runner.save(p,plan);d=runner.create(self.repo,p)
        self.advisory()
        r=subprocess.run([sys.executable,str(LEGACY_CLI),'run',str(d)],env=self.env,
                         capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(runner.read(d/'state.json')['status'],'completed')
        self.assertGreater(budget.summary(d)['used_seconds'],1)

    def test_command_timeout_and_process_cleanup_remain_effective(self):
        d=self.prepared();self.advisory();w=workflow.Workflow(d)
        code,_=w.command([sys.executable,'-c','import time;time.sleep(5)'],'hung-check',.2)
        self.assertEqual(code,124);self.assertIsNone(w.state['active_process'])

    def test_advisory_reforecast_is_not_rejected_for_crossing_old_hard_estimate(self):
        d=self.prepared();p=self.repo/'measurement';p.write_text('Actual timing sample')
        f=dict(items=[dict(id='remaining',description='Original final checks',seconds=1,upper_seconds=2,
                          basis='Actual timing sample',evidence=[str(p)])])
        budget.configure(d,dict(source='Original estimate',estimate=f),request='first-estimate')
        self.advisory();s=runner.read(d/'state.json');s['elapsed_seconds']=5;runner.save(d/'state.json',s)
        f['items'][0].update(seconds=10,upper_seconds=15)
        budget.reforecast(d,f,'Remaining work measured','reforecast-v1')
        self.assertEqual(budget.summary(d)['soft_seconds'],15)
        self.assertFalse(budget.summary(d)['exhausted'])
        self.assertIsNone(budget.review(d))


class AdvisoryControllerTests(unittest.TestCase):
    setUp = watch_fixtures.BudgetControllerTests.setUp
    tearDown = watch_fixtures.BudgetControllerTests.tearDown
    transport = watch_fixtures.BudgetControllerTests.transport
    prepare = watch_fixtures.BudgetControllerTests.prepare
    tick = watch_fixtures.BudgetControllerTests.tick
    budget_actions = watch_fixtures.BudgetControllerTests.budget_actions
    update = watch_fixtures.BudgetControllerTests.update

    def test_advisory_forecast_deduplicates_notifies_origin_and_never_requests_budget(self):
        import control
        self.prepare(used=1001,status='limit')
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            budget.set_time_policy('advisory','User removed elapsed-time stops','policy-v1')
        before=(self.d/'state.json').read_bytes()
        self.tick();self.tick()
        a=self.budget_actions();self.assertEqual(len(a),1)
        self.assertEqual(a[0]['budget_level'],'overrun')
        self.assertFalse(a[0]['budget']['blocks_execution'])
        self.assertEqual(a[0]['notice']['state'],'origin_only')
        self.assertEqual(a[0]['origin_delivery']['state'],'queued')
        self.assertEqual((self.d/'state.json').read_bytes(),before)
        self.assertFalse((self.d/'recovery.json').exists())

    def test_old_exhaustion_action_is_superseded_without_claiming_task_recovered(self):
        self.prepare(used=1001,status='limit');self.tick()
        exhausted=self.budget_actions()[0]['id']
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            budget.set_time_policy('advisory','User removed elapsed-time stops','policy-v1')
        self.tick()
        old=next(a for a in self.budget_actions() if a['id']==exhausted)
        self.assertEqual(old['state'],'superseded')
        self.assertNotIn('resumption_evidence',old)

    def test_only_hub_can_change_machine_policy(self):
        self.prepare()
        with self.assertRaises(ValueError):budget.set_time_policy('advisory','Not from hub','one')


if __name__ == '__main__':
    unittest.main()
