import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import runner
import workflow
from host_recheck import recheck
import test_workflow as fixtures
from legacy_support import LEGACY_CLI


class HostRecheckTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    criterion = fixtures.WorkflowTests.criterion
    contract = fixtures.WorkflowTests.contract
    create = fixtures.WorkflowTests.create

    def blocked(self, handoff=True):
        c = self.contract()
        if handoff:
            c['host_handoff'] = dict(source='Fixture actual local authorization', instructions='Origin runs frozen checks')
        c['acceptance'].append(dict(id='human', description='Actual user result', kind='manual'))
        d = self.create(c)
        w = workflow.Workflow(d)
        plan = dict(objective='Both existing tasks', memory=False, tasks=[
            dict(id='first', prompt='first', checks=self.criterion('first')['checks']),
            dict(id='second', prompt='second', depends_on=['first'], checks=self.criterion('second')['checks'])])
        w.stage(plan, ['first', 'second'])
        stage = Path(w.state['active_run'])
        s = runner.read(stage / 'state.json')
        s.update(status='blocked', elapsed_seconds=17)
        s['tasks']['first'].update(status='failed', attempts=2, feedback='Original exact failure', evidence=['old-failure.log'])
        s['tasks']['second'].update(status='dependency_blocked', attempts=0, feedback='Original dependency blocked')
        runner.save(stage / 'state.json', s)
        w.state.update(status='blocked', elapsed_seconds=31)
        w.state['stages'][0]['status'] = 'blocked'
        w.persist()
        (self.repo / 'first').write_text('fixed')
        return d, stage

    def test_success_preserves_attempts_history_frozen_inputs_and_unstarted_tasks(self):
        d, stage = self.blocked()
        before = runner.read(stage / 'state.json')
        frozen = {p: p.read_bytes() for p in [d/'contract.json', stage/'plan.json']}
        with patch.object(runner.Runner, 'attempt', side_effect=AssertionError('No worker launch')):
            result = recheck(d, 'first', 'Actual fixture repair')
        after = runner.read(stage/'state.json')
        self.assertTrue(result['passed'])
        self.assertEqual(after['tasks']['first']['status'], 'completed')
        self.assertEqual(after['tasks']['first']['attempts'], 2)
        self.assertEqual(after['tasks']['second'], before['tasks']['second'])
        self.assertEqual(after['host_rechecks'][0]['previous'], before['tasks']['first'])
        self.assertIn('old-failure.log', after['tasks']['first']['evidence'])
        self.assertTrue(all(p.read_bytes() == b for p, b in frozen.items()))
        self.assertGreater(after['elapsed_seconds'], 17)
        root = runner.read(d/'state.json')
        self.assertGreater(root['elapsed_seconds'], 31)
        self.assertEqual(root['criteria']['human']['status'], 'pending')
        self.assertEqual(root['status'], 'blocked')
        self.assertIsNone(root['owner_pid'])
        self.assertFalse((self.repo/'second').exists())

    def test_failed_check_appends_actual_evidence_and_keeps_failure_and_limits(self):
        d, stage = self.blocked()
        (self.repo/'first').unlink()
        result = recheck(d, 'first', 'Actual failed repair')
        row = runner.read(stage/'state.json')['tasks']['first']
        self.assertFalse(result['passed'])
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(row['feedback'], 'Original exact failure')
        self.assertEqual(len(row['evidence']), 2)
        self.assertTrue(Path(row['evidence'][-1]).exists())
        self.assertEqual(runner.read(stage/'plan.json')['limits']['max_attempts'], 2)

    def test_success_allows_normal_resume_without_retry_or_counter_reset(self):
        d, stage = self.blocked()
        recheck(d, 'first', 'Actual fixture repair')
        r = subprocess.run([sys.executable, str(LEGACY_CLI), 'run', str(d)],
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        s = runner.read(stage/'state.json')
        self.assertEqual(s['tasks']['first']['attempts'], 2)
        self.assertEqual(s['tasks']['second']['attempts'], 1)
        self.assertEqual(s['status'], 'completed')
        self.assertEqual(runner.read(d/'state.json')['status'], 'needs_input')

    def test_stop_parent_and_child_refused_without_state_changes(self):
        d, stage = self.blocked()
        for where in [d, stage]:
            before = [(p, p.read_bytes()) for p in [d/'state.json', stage/'state.json']]
            (where/'STOP').touch()
            with self.assertRaises(runner.Halt): recheck(d, 'first', 'Actual fixture repair')
            self.assertTrue((where/'STOP').exists())
            self.assertTrue(all(p.read_bytes() == b for p,b in before))
            (where/'STOP').unlink()

    def test_contract_and_plan_drift_refused(self):
        d, stage = self.blocked()
        for p, key in [(d/'contract.json', 'scope'), (stage/'plan.json', 'objective')]:
            old = p.read_bytes(); body = json.loads(old); body[key] += ' drift'; runner.save(p, body)
            with self.assertRaises(runner.Halt): recheck(d, 'first', 'Actual repair')
            p.write_bytes(old)

    def test_requires_handoff_and_actual_source(self):
        d, stage = self.blocked(False)
        with self.assertRaises(ValueError): recheck(d, 'first', 'Actual repair')
        with self.assertRaises(ValueError): recheck(d, 'first', ' ')

    def test_dependency_and_unknown_or_unstarted_task_refused(self):
        d, stage = self.blocked()
        for task in ['second', 'missing']:
            with self.assertRaises(ValueError): recheck(d, task, 'Actual repair')
        s = runner.read(stage/'state.json'); s['tasks']['second']['status'] = 'failed'; runner.save(stage/'state.json', s)
        with self.assertRaises(ValueError): recheck(d, 'second', 'Actual repair')

    def test_repo_lock_refused(self):
        d, stage = self.blocked()
        with (self.repo/'.codex/tasks/.worker.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError): recheck(d, 'first', 'Actual repair')

    def test_live_orphan_process_group_refused(self):
        d, stage = self.blocked()
        s = runner.read(stage/'state.json')
        s['active_process'] = dict(pid=os.getpgrp(), argv=['fixture'])
        runner.save(stage/'state.json', s)
        with self.assertRaises(ValueError): recheck(d, 'first', 'Actual repair')

    def test_wrong_parent_refused_without_check_execution(self):
        d, stage = self.blocked()
        p = stage/'parent.json'; parent = runner.read(p)
        parent['workflow_dir'] = str(self.repo/'other-workflow'); runner.save(p, parent)
        with self.assertRaises(ValueError): recheck(d, 'first', 'Actual repair')

    def test_child_handoff_cannot_expand_parent_authorization(self):
        d, stage = self.blocked()
        p = stage/'plan.json'; plan = runner.read(p)
        plan['host_handoff']['source'] = 'Different authorization'; runner.save(p, plan)
        # A newly frozen but differently authorized child is still rejected.
        s = runner.read(stage/'state.json'); s['plan_hash'] = runner.digest(plan); runner.save(stage/'state.json', s)
        with self.assertRaises(ValueError): recheck(d, 'first', 'Actual repair')

    def test_stop_during_check_keeps_original_failure_and_stop(self):
        d, stage = self.blocked()
        def stop_check(child, task, label):
            (d/'STOP').touch()
            return True, []
        with patch.object(runner.Runner, 'checks', stop_check):
            result = recheck(d, 'first', 'Actual repair')
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue((d/'STOP').exists())
        self.assertEqual(runner.read(stage/'state.json')['tasks']['first']['status'], 'failed')

    def test_cli_uses_frozen_checks_and_reports_result(self):
        d, stage = self.blocked()
        r = subprocess.run([sys.executable, str(LEGACY_CLI), 'host-recheck', str(d),
                            '--task', 'first', '--source', 'Actual fixture repair'],
                           env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)['passed'])
        self.assertEqual(runner.read(stage/'state.json')['tasks']['first']['attempts'], 2)


if __name__ == '__main__':
    unittest.main()
