"""Deterministic clock coverage of reserve review at the enforcement boundary."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import budget
import runner
import workflow
import test_workflow as fixtures

class DeadlineProgressTests(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    criterion=fixtures.WorkflowTests.criterion
    contract=fixtures.WorkflowTests.contract
    create=fixtures.WorkflowTests.create

    def prepared(self):
        c=self.contract(keys=('first',));c['limits']['runtime_seconds']=1
        proof=self.repo/'measurements';proof.write_text('bounded synthetic independent check sample')
        c['budget']=dict(source='Synthetic existing reserve authorization',estimate=dict(risk_percent=0,
            items=[dict(id='remaining',description='Complete fixture',seconds=1,upper_seconds=6,
                        basis='Synthetic measured fixture',evidence=[str(proof)])]))
        d=self.create(c);w=workflow.Workflow(d)
        w._accounted_at=100;w.deadline=101;w.state['elapsed_seconds']=0
        return d,w

    def passing(self,w,label='one',code=0):
        log=str(w.dir/'evidence'/('first-'+label+'-check-0.log'))
        w.event('command_started',argv=['fixture-independent-check','first'],evidence=log)
        w.event('command_finished',code=code,evidence=log)

    def test_deadline_bypasses_throttle_for_new_independent_progress(self):
        d,w=self.prepared()
        with patch.object(runner,'now',return_value=100.1):budget.poll(w)
        self.assertEqual(w._next_budget_poll,101.1);self.passing(w)
        with patch.object(runner,'now',return_value=101):w.guard()
        ledger=budget.load(d,w.state['contract_hash'])
        self.assertEqual((ledger['extensions'],ledger['soft_seconds'],ledger['hard_seconds']),(1,6,6))
        self.assertEqual(w.deadline,106)
        self.assertEqual([v['kind'] for v in ledger['history']],['policy','auto_extension'])
        self.assertEqual(ledger['history'][1]['details']['spent_seconds'],1)

    def test_throttle_still_applies_before_deadline(self):
        d,w=self.prepared()
        with patch.object(runner,'now',return_value=100):budget.poll(w)
        self.passing(w)
        with patch.object(runner,'now',return_value=100.9):budget.poll(w)
        self.assertEqual(budget.load(d,w.state['contract_hash'])['extensions'],0)

    def test_no_progress_or_failed_check_does_not_release_reserve(self):
        for code in (None,1):
            with self.subTest(code=code):
                d,w=self.prepared()
                with patch.object(runner,'now',return_value=100.1):budget.poll(w)
                if code is not None:self.passing(w,code=code)
                with patch.object(runner,'now',return_value=101),self.assertRaisesRegex(runner.Halt,'limit'):w.guard()
                self.assertEqual(budget.load(d,w.state['contract_hash'])['extensions'],0)

    def test_duplicate_check_does_not_release_second_reserve(self):
        d,w=self.prepared();self.passing(w)
        with patch.object(runner,'now',return_value=100.1):budget.poll(w)
        self.passing(w,label='duplicate');before=copy.deepcopy(budget.load(d,w.state['contract_hash']))
        with patch.object(runner,'now',return_value=106),self.assertRaisesRegex(runner.Halt,'limit'):w.guard()
        self.assertEqual(budget.load(d,w.state['contract_hash']),before)

    def test_parent_review_precedes_child_deadline_and_preserves_child_ledger_absence(self):
        d,w=self.prepared()
        with patch.object(runner,'now',return_value=100):w.stage(w.contract['stages'][0]['plan'],['first'])
        child=runner.Runner(w.state['active_run']);child.parent_guard=w.guard
        child._accounted_at=100.8;child.deadline=float('inf')
        with patch.object(runner,'now',return_value=100.1):budget.poll(w)
        self.passing(w)
        with patch.object(runner,'now',return_value=101):child.guard()
        self.assertEqual(w.deadline,106);self.assertFalse((child.dir/'budget.json').exists())
        with patch.object(runner,'now',return_value=106),self.assertRaisesRegex(runner.Halt,'limit'):child.guard()

    def test_stop_takes_precedence_over_reserve_review(self):
        d,w=self.prepared();self.passing(w);(d/'STOP').touch()
        with patch.object(runner,'now',return_value=101),self.assertRaisesRegex(runner.Halt,'stopped'):w.guard()
        self.assertEqual(budget.load(d,w.state['contract_hash'])['extensions'],0)

    def test_fixed_deadline_stays_enforced_without_ledger(self):
        c=self.contract(keys=('first',));c['limits']['runtime_seconds']=1;d=self.create(c)
        w=workflow.Workflow(d);w._accounted_at=100;w.deadline=101;w._next_budget_poll=102
        self.passing(w)
        with patch.object(runner,'now',return_value=101),self.assertRaisesRegex(runner.Halt,'limit'):w.guard()
        self.assertFalse((d/'budget.json').exists())

    def test_advisory_policy_keeps_elapsed_and_never_mints_extension(self):
        d,w=self.prepared();self.passing(w);before=(d/'budget.json').read_bytes()
        with patch.object(budget,'time_enforced',return_value=False),patch.object(runner,'now',return_value=1000):budget.poll(w)
        self.assertEqual(w.deadline,float('inf'));self.assertEqual(w.state['elapsed_seconds'],0)
        self.assertEqual((d/'budget.json').read_bytes(),before)

    def test_validation_overhead_crossing_deadline_still_reviews_existing_progress(self):
        import runtime_selection
        d,w=self.prepared();clock=[100.1]
        with patch.object(runner,'now',side_effect=lambda:clock[0]):budget.poll(w)
        self.passing(w)
        def validate(directory):clock[0]=101;return None
        with patch.object(runner,'now',side_effect=lambda:clock[0]),patch.object(runtime_selection,'require_current',side_effect=validate) as check:
            w.guard()
        check.assert_called_once_with(d);self.assertEqual(w.deadline,106)

    def test_runtime_drift_is_not_overridden_by_passing_checks(self):
        import runtime_selection
        d,w=self.prepared();self.passing(w)
        with patch.object(runner,'now',return_value=101),patch.object(runtime_selection,'require_current',side_effect=ValueError('synthetic drift')):
            with self.assertRaisesRegex(ValueError,'drift'):w.guard()
        self.assertEqual(budget.load(d,w.state['contract_hash'])['extensions'],0)

    def crossing_read(self,owner):
        owner._accounted_at=100;owner.deadline=101;owner.state['elapsed_seconds']=0
        with patch.object(runner,'now',return_value=100.1):budget.poll(owner)
        self.passing(owner);reads=[]
        def current():
            value=100.99 if not reads else 101.01
            reads.append(value);return value
        with patch.object(runner,'now',side_effect=current):owner.guard()
        self.assertEqual(reads[:2],[100.99,101.01]);self.assertEqual(owner.deadline,106)
        ledger=budget.load(owner.dir,owner.state.get('contract_hash',owner.state.get('plan_hash')))
        self.assertEqual((ledger['extensions'],ledger['soft_seconds'],ledger['hard_seconds']),(1,6,6))

    def test_workflow_crossing_deadline_between_poll_and_enforcement_reviews_progress(self):
        d,w=self.prepared();self.crossing_read(w)

    def test_standalone_crossing_deadline_between_poll_and_enforcement_reviews_progress(self):
        d,w=self.prepared()
        plan=dict(objective='Standalone existing reserve fixture',memory=False,
            tasks=w.contract['stages'][0]['plan']['tasks'],budget=w.contract['budget'],
            limits={k:w.contract['limits'][k] for k in ('runtime_seconds','attempt_seconds','check_seconds','max_attempts')})
        source=self.repo/'standalone.json';runner.save(source,plan)
        standalone=runner.create(self.repo,source);self.crossing_read(runner.Runner(standalone))
