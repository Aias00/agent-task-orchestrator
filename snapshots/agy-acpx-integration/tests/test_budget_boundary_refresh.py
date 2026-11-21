"""A soft deadline must recheck already-authorized progress despite poll cadence."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import budget
import runner
import workflow
import test_budget as fixtures

class BoundaryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.home=tempfile.TemporaryDirectory(prefix='ato-boundary-isolation-')
        self.addCleanup(self.home.cleanup)
        self.env=patch.dict(os.environ,ATO_HOME=self.home.name+'/ato',CODEX_HOME=self.home.name+'/native',CODEX_THREAD_ID='')
        self.env.start();self.addCleanup(self.env.stop)
        self.fixture=fixtures.BudgetTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)

    def owner(self,kind,progress):
        if kind=='workflow':
            d=self.fixture.prepared(runtime=1);self.fixture.configure(d,1,6);owner=workflow.Workflow(d)
        else:
            p=self.fixture.repo/'standalone.json'
            runner.save(p,dict(objective='bounded fixture',memory=False,tasks=[dict(id='a',prompt='fixture',checks=[['true']])],limits=dict(runtime_seconds=1)))
            d=runner.create(self.fixture.repo,p)
            budget.configure(d,dict(source='Existing bounded fixture authorization',estimate=self.fixture.forecast(1,6)),request='fixture')
            owner=runner.Runner(d)
        if progress:
            evidence=str(d/'evidence'/'first-boundary-check-0.log')
            owner.event('command_started',argv=['independent-check','first'],evidence=evidence)
            owner.event('command_finished',code=0,evidence=evidence)
        owner._accounted_at=100;owner.deadline=101;owner._next_budget_poll=101
        owner.state['elapsed_seconds']=0
        return d,owner

    def guard_at_clock_boundary(self,owner):
        values=iter((100.9,101.1))
        with patch.object(runner,'now',side_effect=lambda:next(values,101.1)):
            owner.guard()

    def test_workflow_rechecks_progress_at_soft_boundary(self):
        d,owner=self.owner('workflow',True)
        self.guard_at_clock_boundary(owner)
        self.assertGreater(owner.deadline,101.1)
        self.assertEqual(budget.summary(d)['extensions'],1)
        self.assertEqual(budget.summary(d)['hard_seconds'],8)

    def test_standalone_rechecks_progress_at_soft_boundary(self):
        d,owner=self.owner('standalone',True)
        self.guard_at_clock_boundary(owner)
        self.assertGreater(owner.deadline,101.1)
        self.assertEqual(budget.summary(d)['extensions'],1)
        self.assertEqual(budget.summary(d)['hard_seconds'],8)

    def test_no_independent_progress_still_stops(self):
        d,owner=self.owner('workflow',False)
        with self.assertRaisesRegex(runner.Halt,'limit'):self.guard_at_clock_boundary(owner)
        self.assertEqual(budget.summary(d)['extensions'],0)

    def test_stop_is_never_bypassed_by_available_reserve(self):
        d,owner=self.owner('workflow',True);(d/'STOP').touch()
        with self.assertRaisesRegex(runner.Halt,'stopped'):self.guard_at_clock_boundary(owner)
        self.assertEqual(budget.summary(d)['extensions'],0)
