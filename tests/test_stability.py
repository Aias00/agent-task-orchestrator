"""Deterministic budget-boundary and command wakeup regressions."""
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import budget
import runner
import workflow
import test_budget as budgets
import test_runner as runners


class StabilityBudgetTests(unittest.TestCase):
    setUp = budgets.BudgetTests.setUp
    tearDown = budgets.BudgetTests.tearDown
    criterion = budgets.BudgetTests.criterion
    contract = budgets.BudgetTests.contract
    create = budgets.BudgetTests.create
    forecast = budgets.BudgetTests.forecast
    prepared = budgets.BudgetTests.prepared
    configure = budgets.BudgetTests.configure
    successful_check = budgets.BudgetTests.successful_check

    def owner(self):
        d = self.prepared(runtime=1)
        self.configure(d, seconds=1, upper=6)
        w = workflow.Workflow(d)
        w._accounted_at = 100
        w.deadline = 101
        w.state['elapsed_seconds'] = 0
        # Initialization/identity/persistence can delay the first poll.
        with patch.object(runner, 'now', return_value=100.05):
            w.guard()
        return d, w

    def test_progress_is_rechecked_at_deadline_even_before_next_poll(self):
        d, w = self.owner()
        self.successful_check(d)
        with patch.object(runner, 'now', return_value=101.001):
            w.guard()
        self.assertGreater(w.deadline, 101.001)
        self.assertEqual(budget.summary(d)['extensions'], 1)
        self.assertEqual(budget.summary(d)['hard_seconds'], 8)

    def test_deadline_without_new_progress_still_stops(self):
        d, w = self.owner()
        with patch.object(runner, 'now', return_value=101.001):
            with self.assertRaisesRegex(runner.Halt, 'limit'):
                w.guard()
        self.assertEqual(budget.summary(d)['extensions'], 0)

    def test_new_progress_cannot_bypass_stop_or_hard_ceiling(self):
        d, w = self.owner()
        self.successful_check(d)
        with patch.object(runner, 'now', return_value=109):
            with self.assertRaisesRegex(runner.Halt, 'limit'):
                w.guard()
        (d/'STOP').touch()
        with self.assertRaisesRegex(runner.Halt, 'stopped'):
            w.guard()


class CommandWakeupTests(unittest.TestCase):
    setUp = runners.RunnerTests.setUp
    tearDown = runners.RunnerTests.tearDown
    create = runners.RunnerTests.create
    task = runners.RunnerTests.task

    def test_short_command_waits_on_process_instead_of_fixed_sleep(self):
        d = self.create([self.task('a')])
        w = runner.Runner(d)
        with patch.object(runner, 'time', SimpleNamespace(sleep=lambda *_: self.fail('unrelated polling sleep'))):
            code, _ = w.command([sys.executable, '-c', 'pass'], 'fast-check-0', 3)
        self.assertEqual(code, 0)
        self.assertIsNone(w.state['active_process'])

    def test_wait_timeout_still_checks_stop_and_reaps_child(self):
        d = self.create([self.task('a')])
        w = runner.Runner(d)
        original = w.guard
        def guard():
            if w.process is not None:
                (d/'STOP').touch()
            original()
        with patch.object(w, 'guard', side_effect=guard):
            with self.assertRaisesRegex(runner.Halt, 'stopped'):
                w.command([sys.executable, '-c', 'import time;time.sleep(30)'], 'slow', 3)
        self.assertIsNone(w.state['active_process'])
        self.assertIsNone(w.process)
