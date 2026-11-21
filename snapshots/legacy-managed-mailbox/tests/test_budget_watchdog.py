import json
import math
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_interaction as fixtures
from test_interaction import ORIGIN, HUB
import budget
import control
import runner
import recovery


class BudgetMathTests(unittest.TestCase):
    setUp = fixtures.InteractionTests.setUp
    tearDown = fixtures.InteractionTests.tearDown

    def check(self, used, status='running', alive=True, **extra):
        c=runner.read(self.d/'contract.json');c['limits']['runtime_seconds']=1000
        runner.save(self.d/'contract.json',c)
        s=runner.read(self.d/'state.json')
        s.update(dict(status=status,elapsed_seconds=used,updated_at=100) | extra)
        s['contract_hash']=runner.digest(c);runner.save(self.d/'state.json',s)
        return budget.observation(self.d,s,110,alive)

    def test_live_heartbeat_is_charged_not_creation_time(self):
        self.assertEqual(self.check(790)['level'], 'warning')
        self.assertEqual(self.check(890)['level'], 'critical')
        self.assertEqual(self.check(990)['level'], 'exhausted')
        self.assertEqual(self.check(0)['observed_used_seconds'], 10)

    def test_idle_user_wait_and_dead_owner_do_not_accrue(self):
        self.assertEqual(self.check(790, 'needs_input')['observed_used_seconds'],790)
        self.assertEqual(self.check(790, alive=False)['observed_used_seconds'],790)
        self.assertIsNone(self.check(890, 'needs_input')['level'])
        self.assertEqual(self.check(790, accounting_paused=True)['observed_used_seconds'],790)

    def test_frozen_exhaustion_report_survives_absent_owner(self):
        self.assertEqual(self.check(1001, 'blocked', False)['level'],'exhausted')
        self.assertEqual(self.check(2, 'limit', False)['level'],'exhausted')

    def test_invalid_accounting_is_not_silently_zero(self):
        for value in [None,True,float('nan'),float('inf'),-1,'900']:
            with self.subTest(value=value),self.assertRaises(ValueError):self.check(value)

    def test_future_watermark_does_not_refund_elapsed(self):
        self.assertEqual(self.check(900,updated_at=1000)['observed_used_seconds'],900)


class BudgetControllerTests(unittest.TestCase):
    setUp = fixtures.InteractionTests.setUp
    tearDown = fixtures.InteractionTests.tearDown
    transport = fixtures.InteractionTests.transport

    def prepare(self, used=810, status='running'):
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            control.enable('User requests watchdog budget follow-up',60,2)
        s=runner.read(self.d/'state.json');s.update(status=status,elapsed_seconds=used,
            updated_at=runner.now(),owner_pid=os.getpid(),active_process=None)
        c=runner.read(self.d/'contract.json');c['limits']['runtime_seconds']=1000
        runner.save(self.d/'contract.json',c);s['contract_hash']=runner.digest(c)
        runner.save(self.d/'state.json',s);control.register(self.d)

    def tick(self):
        with patch('control.subprocess.run',side_effect=self.transport): control.tick()

    def budget_actions(self):
        return [x for x in control.rows(True) if x['kind']=='budget_watchdog']

    def update(self, **changes):
        s=runner.read(self.d/'state.json');s.update(changes);runner.save(self.d/'state.json',s)

    def test_budget_alert_works_without_model_process_or_repair_supervisor(self):
        self.prepare();self.tick()
        a=self.budget_actions()[0]
        self.assertEqual(a['budget_level'],'warning')
        self.assertEqual(a['notice']['state'],'queued')
        self.assertNotIn('presented_at',a)
        self.assertEqual(a['origin_delivery']['state'],'queued')
        self.assertFalse((self.d/'recovery.json').exists())

    def test_presentation_requires_hub_and_never_grants_budget(self):
        self.prepare();self.tick();a=self.budget_actions()[0]
        before=(self.d/'contract.json').read_bytes()
        with self.assertRaises(ValueError): control.record(a['id'],state='presented',note='wrong actor')
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            control.record(a['id'],state='presented',note='Actually shown to user')
        self.tick();self.assertEqual((self.d/'contract.json').read_bytes(),before)
        self.assertIn('presented_at',self.budget_actions()[0])

    def test_deduplicates_restart_and_escalates_to_exhaustion(self):
        self.prepare();self.tick();self.tick()
        self.assertEqual(len(self.budget_actions()),1)
        self.update(elapsed_seconds=1001,status='limit',owner_pid=None);self.tick()
        actions=self.budget_actions();self.assertEqual(len(actions),2)
        self.assertEqual(next(a for a in actions if a['budget_level']=='warning')['state'],'superseded')
        exhausted=next(a for a in actions if a['budget_level']=='exhausted')
        self.assertEqual(exhausted['notice']['state'],'queued')
        self.assertNotIn('origin_delivery',exhausted)
        self.tick();self.assertEqual(len(self.budget_actions()),2)

    def test_unknown_delivery_not_blindly_retried(self):
        self.prepare(status='limit')
        with patch('control.subprocess.run',side_effect=subprocess.TimeoutExpired('queue',25)) as send:
            control.tick();control.tick()
            self.assertEqual(send.call_count,1)
        self.assertEqual(self.budget_actions()[0]['notice']['state'],'delivery_unknown')

    def test_stop_pause_complete_do_not_wake_origin(self):
        for status in ['stopped','paused','completed']:
            with self.subTest(status=status):
                self.prepare(status=status)
                self.tick()
                self.assertFalse(self.budget_actions())

    def test_start_event_alone_does_not_resolve_budget_alert(self):
        self.prepare();self.tick()
        with (self.d/'events.jsonl').open('a') as f:
            f.write(json.dumps({'event':'workflow_started','time':runner.now()})+'\n')
        self.update(status='blocked',owner_pid=None);self.tick()
        self.assertNotEqual(self.budget_actions()[0]['state'],'resolved')

    def test_live_process_requires_subsequent_progress(self):
        self.prepare();self.tick()
        a=self.budget_actions()[0]
        baseline=a['baseline_generation'];evidence=self.repo/'actual.log';evidence.write_text('one')
        self.update(active_process={'pid':os.getpid(),'argv':['fixture-check'],'evidence':str(evidence)})
        s=runner.read(self.d/'state.json');g=[baseline[0],baseline[1]+1]
        self.assertFalse(control.resumed(a,self.d,s,g))
        self.assertFalse(control.resumed(a,self.d,s,g))
        evidence.write_text('one\ntwo')
        self.assertTrue(control.resumed(a,self.d,s,g))
        self.assertEqual(a['resumption_evidence']['current']['pid'],os.getpid())

    def test_owner_without_live_worker_is_not_recovery(self):
        self.prepare();s=runner.read(self.d/'state.json')
        a={'baseline_generation':[0,0]}
        self.assertFalse(control.resumed(a,self.d,s,[1,1]))

    def test_heartbeat_alone_is_not_post_start_progress(self):
        self.prepare()
        log=self.repo/'heartbeat.log';log.write_text('unchanged')
        s=runner.read(self.d/'state.json')
        s['active_process']=dict(pid=os.getpid(),argv=['fixture-check'],evidence=str(log))
        a=dict(kind='workflow',baseline_generation=[0,0])
        self.assertFalse(control.resumed(a,self.d,s,[1,1]))
        s['updated_at']=runner.now()+100
        self.assertFalse(control.resumed(a,self.d,s,[1,1]))

    def test_child_exit_clears_resume_probe_without_claiming_recovery(self):
        self.prepare()
        log=self.repo/'probe.log';log.write_text('one')
        s=runner.read(self.d/'state.json')
        s['active_process']=dict(pid=os.getpid(),argv=['fixture-check'],evidence=str(log))
        a=dict(baseline_generation=[0,0])
        self.assertFalse(control.resumed(a,self.d,s,[1,1]))
        log.write_text('more output')
        with patch('control.recovery.alive',return_value=False):
            self.assertFalse(control.resumed(a,self.d,s,[1,1]))
        self.assertNotIn('resume_probe',a)

    def adaptive(self):
        self.prepare(used=0,status='blocked')
        evidence=self.repo/'estimate.log';evidence.write_text('Actual old checks and required remaining verification')
        forecast=dict(items=[dict(id='remaining',description='Remaining original checks',seconds=100,
            upper_seconds=2000,basis='Existing checks and bounded uncertainty',evidence=[str(evidence)])],risk_percent=0)
        budget.configure(self.d,dict(source='Actual fixture task envelope authorization',estimate=forecast),request='adaptive')

    def test_soft_exhaustion_with_reserve_is_not_a_hard_exhaustion_alert(self):
        self.adaptive()
        self.update(elapsed_seconds=1001,status='limit',owner_pid=None)
        self.tick()
        self.assertFalse(any(a['budget_level']=='exhausted' for a in self.budget_actions()))
        self.assertTrue(any(a['kind']=='budget_review' for a in control.rows(True)))

    def test_hard_threshold_includes_charged_host_work(self):
        self.prepare(used=5,status='blocked')
        proof=self.repo/'host-receipt';proof.write_text('Measured actual host work 1000 seconds')
        budget.charge(self.d,1000,'Actual fixture host timing',proof,'charge')
        self.tick()
        a=self.budget_actions()[0]
        self.assertEqual(a['budget_level'],'exhausted')
        self.assertEqual(a['budget']['observed_used_seconds'],1005)
        self.assertNotIn('origin_delivery',a)

    def test_old_exhaustion_does_not_resolve_after_new_envelope_and_start_event_alone(self):
        self.prepare(used=1001,status='limit');self.tick()
        exhausted=self.budget_actions()[0]
        evidence=self.repo/'forecast';evidence.write_text('Remaining evidence-backed work')
        budget.configure(self.d,dict(source='Actual fixture new envelope approval',estimate=dict(items=[
            dict(id='finish',description='Original remaining verification',seconds=100,upper_seconds=300,
                 basis='Known original commands',evidence=[str(evidence)])])),request='new')
        with (self.d/'events.jsonl').open('a') as f:
            f.write(json.dumps(dict(event='workflow_started',time=runner.now()))+'\n')
        self.update(status='blocked',owner_pid=None)
        self.tick()
        current=next(a for a in self.budget_actions() if a['id']==exhausted['id'])
        self.assertNotEqual(current['state'],'resolved')

    def test_watchdog_does_not_modify_state_or_contract(self):
        self.prepare();before=[(self.d/f).read_bytes() for f in ['state.json','contract.json']]
        self.tick();self.tick()
        self.assertEqual(before,[(self.d/f).read_bytes() for f in ['state.json','contract.json']])


if __name__=='__main__':unittest.main()
