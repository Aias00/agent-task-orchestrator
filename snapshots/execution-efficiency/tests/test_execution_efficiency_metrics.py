"""Timing partition, real Runner checks/CLI and offline three-arm receipts.

Only bounded foreground temporary Git/file fixtures. No actual native CLI,
provider, scheduler, detached process or production state access.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import execution_metrics as metrics
import runner
import task_trace
import workflow
from scripts import benchmark_execution_efficiency_fixture as fixture
from scripts import verify_execution_efficiency_local as actual


def row(key, phase, start, duration=None, parent=None):
    value = dict(version=1, id=key, phase=phase, parent=parent, started_at=start)
    if duration is not None:
        value.update(duration_seconds=duration, finished_at=start+duration)
    return value


class TimingPartitionTests(unittest.TestCase):
    def test_nested_spans_partition_without_counting_parent_twice(self):
        result = metrics.project([row('p', 'worker_unknown', 0, 10),
            row('m', 'model_request', 2, 5, 'p'), row('t', 'tool', 4, 1, 'm')])
        self.assertEqual(result['phases_seconds']['worker_unknown'], 5)
        self.assertEqual(result['phases_seconds']['model_request'], 4)
        self.assertEqual(result['phases_seconds']['tool'], 1)
        self.assertEqual(result['covered_seconds'], 10)
        self.assertEqual(result['observed_wall_seconds'], 10)
        self.assertTrue(result['complete'])

    def test_concurrent_sibling_overlap_is_one_unknown_wall_interval(self):
        result = metrics.project([row('a', 'tool', 0, 5), row('b', 'independent_check', 3, 5)])
        self.assertEqual(result['covered_seconds'], 8)
        self.assertEqual(result['phases_seconds']['concurrent_unknown'], 2)
        self.assertEqual(result['phases_seconds']['tool'], 3)
        self.assertEqual(result['phases_seconds']['independent_check'], 3)

    def test_same_phase_concurrency_is_union_not_command_sum(self):
        result = metrics.project([row('a', 'worker_unknown', 0, 5), row('b', 'worker_unknown', 3, 5)])
        self.assertEqual(result['phases_seconds']['worker_unknown'], 8)
        self.assertIsNone(result['phases_seconds']['model_request'])
        self.assertEqual(result['model_latency'], 'unknown')

    def test_open_crashed_span_does_not_turn_age_into_model_time(self):
        result = metrics.project([row('old', 'worker_unknown', 1)])
        self.assertEqual(result['covered_seconds'], 0)
        self.assertIsNone(result['observed_wall_seconds'])
        self.assertEqual(result['open_span_count'], 1)
        self.assertIn('open_span', result['issues'])
        self.assertFalse(result['complete'])

    def test_closed_child_inside_open_parent_remains_measured_with_unknown_gap(self):
        result = metrics.project([row('old', 'worker_unknown', 1), row('check', 'checkpoint', 3, 2, 'old')])
        self.assertEqual(result['covered_seconds'], 2)
        self.assertEqual(result['phases_seconds']['checkpoint'], 2)
        self.assertFalse(result['complete'])

    def test_cycles_orphan_and_child_outside_parent_never_supply_duration(self):
        for rows in ([row('a', 'tool', 0, 2, 'a')],
            [row('a', 'tool', 0, 2, 'missing')],
            [row('a', 'tool', 0, 2, 'b'), row('b', 'warmup', 0, 2, 'a')],
            [row('a', 'warmup', 2, 1), row('b', 'tool', 0, 4, 'a')]):
            with self.subTest(rows=rows):
                result = metrics.project(rows)
                self.assertFalse(result['complete'])
                self.assertIn('invalid_parent_or_interval', result['issues'])

    def test_negative_nan_bool_and_infinite_durations_are_not_latency(self):
        for duration in (-1, math.nan, math.inf, True):
            with self.subTest(duration=duration):
                result = metrics.project([row('bad', 'model_request', 1, duration)])
                self.assertEqual(result['covered_seconds'], 0)
                self.assertIsNone(result['phases_seconds']['model_request'])

    def test_uninstrumented_gaps_are_explicit_not_inference(self):
        result = metrics.project([row('first', 'warmup', 0, 1), row('second', 'independent_check', 5, 2)])
        self.assertEqual(result['observed_wall_seconds'], 7)
        self.assertEqual(result['covered_seconds'], 3)
        self.assertEqual(result['uninstrumented_gap_seconds'], 4)
        self.assertIsNone(result['phases_seconds']['external_wait'])


class TimingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-metrics-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base/'repo'; self.repo.mkdir()
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.repo)], check=True)
        env = dict(os.environ, ATO_HOME=str(self.base/'ato'), CODEX_HOME=str(self.base/'native'),
                   CTASK_CODEX_BIN='/nonexistent/no-native', PYTHONDONTWRITEBYTECODE='1')
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            env.pop(key, None)
        context = patch.dict(os.environ, env, clear=True); context.start(); self.addCleanup(context.stop)
        owner = patch.object(runner, 'process_identity', side_effect=lambda pid:'fixture-kernel-'+str(pid))
        owner.start(); self.addCleanup(owner.stop)
        self.task = dict(id='artifact', prompt='bounded fixture', checks=[[sys.executable, 'check.py']])
        self.plan = dict(objective='Full metric fixture objective', memory=False, tasks=[self.task],
            execution_policy=copy.deepcopy(fixture.EXECUTION))
        (self.repo/'input.txt').write_text('yes')
        (self.repo/'check.py').write_text("from pathlib import Path\nassert Path('input.txt').read_text()=='yes'\n")

    def create(self, cached=False):
        if cached:
            self.plan['verification_policy'] = dict(version=1, checks=dict(artifact=dict(inputs=['input.txt'], env=[], cacheable=True)))
        runner.save(self.repo/'plan-input.json', self.plan)
        r = runner.Runner(runner.create(self.repo, self.repo/'plan-input.json'))
        r.state.update(owner_pid=os.getpid(), owner_identity=runner.process_identity(os.getpid()))
        r.persist()
        return r

    def test_monotonic_elapsed_survives_wall_clock_regression(self):
        with patch.object(metrics.time, 'time', side_effect=[100, -999]), patch.object(metrics.time, 'monotonic', side_effect=[10, 13]):
            with metrics.span(self.repo, 'worker_unknown'):
                pass
        result = metrics.query(self.repo)
        self.assertEqual(result['covered_seconds'], 3)
        self.assertEqual(result['spans'][0]['finished_at'], 103)
        self.assertEqual(result['model_latency'], 'unknown')

    def test_exception_span_is_observation_not_success_and_closes(self):
        with self.assertRaisesRegex(ValueError, 'intentional'):
            with metrics.span(self.repo, 'checkpoint'):
                raise ValueError('intentional')
        result = metrics.query(self.repo)
        self.assertEqual(result['spans'][0]['outcome'], 'raised')
        self.assertEqual(result['open_span_count'], 0)

    def test_terminal_span_cannot_be_replaced_or_double_counted(self):
        with metrics.span(self.repo, 'tool'):
            pass
        saved = metrics.query(self.repo)['spans'][0]
        self.assertTrue(metrics._write(self.repo, saved, finish=True))
        self.assertFalse(metrics._write(self.repo, dict(saved, duration_seconds=9), finish=True))
        self.assertEqual(len(metrics.query(self.repo)['spans']), 1)

    def test_overflow_is_bounded_and_never_claims_complete_telemetry(self):
        with patch.object(metrics, 'MAX_SPANS', 2):
            for _ in range(5):
                with metrics.span(self.repo, 'tool'):
                    pass
            result = metrics.query(self.repo)
        self.assertEqual(result['span_count'], 2)
        self.assertFalse(result['complete'])
        self.assertIn('span_bound_exceeded', result['issues'])

    def test_trace_io_failure_and_link_cannot_prevent_foreground_operation(self):
        ran = []
        with metrics.span(self.repo/'absent', 'tool'):
            ran.append(True)
        target = self.repo/'target'; target.write_text('preserved')
        (self.repo/'trace.sqlite3').symlink_to(target)
        with metrics.span(self.repo, 'tool'):
            ran.append(True)
        self.assertEqual(ran, [True, True])
        self.assertEqual(target.read_text(), 'preserved')

    def test_corrupt_trace_cannot_supply_success_or_break_task_filtered_query(self):
        with sqlite3.connect(self.repo/'trace.sqlite3') as db:
            db.execute('CREATE TABLE phase_spans(seq INTEGER PRIMARY KEY,id TEXT,data TEXT)')
            db.execute('INSERT INTO phase_spans(id,data) VALUES(?,?)', ('bad', '[]'))
        for task in (None, 'artifact'):
            result = metrics.query(self.repo, task=task)
            self.assertFalse(result['complete'])
            self.assertEqual(result['issues'], ['trace_unavailable'])
            self.assertIsNone(result['phases_seconds']['model_request'])

    def test_read_only_projection_has_no_filesystem_mutation(self):
        r = self.create()
        r.checks(self.task, 'first')
        before = {str(p): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes()) for p in r.dir.rglob('*') if p.is_file()}
        first = metrics.query(r.dir); second = metrics.query(r.dir)
        self.assertEqual(first, second)
        self.assertEqual(before, {str(p): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes()) for p in r.dir.rglob('*') if p.is_file()})

    def test_actual_runner_check_and_cache_reuse_have_different_counts(self):
        r = self.create(cached=True)
        for name in ('a', 'b', 'final-independent'):
            self.assertTrue(r.checks(self.task, name)[0])
        view = metrics.view(r.dir, r.state['execution_accounting'])
        self.assertEqual(view['check_executions'], 2)
        self.assertEqual(view['check_reuses'], 1)
        self.assertIsNone(view['phases_seconds']['model_request'])
        self.assertGreater(view['phases_seconds']['independent_check'], 0)
        self.assertGreater(view['v2_ledger']['active_seconds'], 0)
        self.assertIn('not additive', view['ledger_basis'])

    def test_actual_capacity_admission_wait_is_queue_and_separate_from_ledger(self):
        import capacity
        self.plan['capacity'] = dict(endpoint='fixture', priority='background')
        home = Path(os.environ['ATO_HOME']); home.mkdir(parents=True, exist_ok=True)
        runner.save(home/'capacity.json', dict(version=1, endpoints=dict(fixture=dict(limit=1, reserved_interactive=0))))
        r = self.create()
        original = capacity.CapacityStore.claim
        calls = []
        def busy_once(store, request):
            calls.append(request)
            return None if len(calls) == 1 else original(store, request)
        value = r.state['tasks']['artifact']
        with patch.object(capacity.CapacityStore, 'claim', new=busy_once):
            admission = r.capacity_admit(value, 'artifact', 1)
        self.assertEqual(len(calls), 2)
        r.capacity_finish(value, admission)
        timing = metrics.view(r.dir, r.state['execution_accounting'])
        self.assertGreaterEqual(timing['phases_seconds']['queue'], .2)
        self.assertEqual(timing['worker_process_startups'], 0)
        self.assertGreater(timing['v2_ledger']['wait_seconds'], 0)
        self.assertLessEqual(timing['covered_seconds'], timing['observed_wall_seconds']+.00001)
        self.assertEqual(value['capacity_request']['state'], 'released')

    def test_failed_check_has_observed_time_and_never_a_reused_pass(self):
        r = self.create(cached=True)
        (self.repo/'input.txt').write_text('wrong')
        for name in ('a', 'b'):
            self.assertFalse(r.checks(self.task, name)[0])
        view = metrics.query(r.dir)
        self.assertEqual(view['check_executions'], 2)
        self.assertEqual(view['check_reuses'], 0)

    def test_stopped_check_is_an_attempt_without_a_process_execution(self):
        r = self.create()
        (r.dir/'STOP').touch()
        with self.assertRaises(runner.Halt):
            r.checks(self.task, 'stopped')
        timing = metrics.query(r.dir)
        self.assertEqual(timing['check_attempts'], 1)
        self.assertEqual(timing['check_executions'], 0)
        self.assertFalse(timing['spans'][0]['process_started'])

    def test_missing_executable_is_not_a_launched_check(self):
        r = self.create()
        ok, _ = r.checks(dict(self.task, checks=[['/nonexistent/fixture-check']]), 'missing')
        self.assertFalse(ok)
        timing = metrics.query(r.dir)
        self.assertEqual(timing['check_attempts'], 1)
        self.assertEqual(timing['check_executions'], 0)
        self.assertEqual(timing['check_launches_unknown'], 0)

    def test_actual_command_wall_is_worker_unknown_not_model_latency(self):
        r = self.create()
        self.assertEqual(r.command([sys.executable, '-c', 'pass'], 'synthetic-worker', 3, agent_kind='synthetic')[0], 0)
        timing = metrics.query(r.dir)
        self.assertGreater(timing['phases_seconds']['worker_unknown'], 0)
        self.assertIsNone(timing['phases_seconds']['model_request'])
        self.assertGreater(timing['phases_seconds']['restart'], 0)
        self.assertLessEqual(timing['covered_seconds'], timing['observed_wall_seconds']+.00001)

    def test_trace_status_and_task_summary_cli_project_spans_without_writes(self):
        r = self.create(); r.checks(self.task, 'first')
        for command in ('trace', 'status', 'task-summary'):
            argv = [sys.executable, str(ROOT/'runner.py'), command, str(r.dir)]
            if command == 'trace':
                argv.append('--json')
            if command == 'task-summary':
                argv.extend(['--format', 'json'])
            result = subprocess.run(argv, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data['phase_timing']['check_executions'], 1)
            self.assertEqual(data['phase_timing']['model_latency'], 'unknown')
            if command == 'status':
                self.assertEqual(data['task_view']['phase_timing']['check_executions'], 1)
        self.assertEqual(task_trace.query(r.dir)['phase_timing']['check_executions'], 1)

    def test_task_filtered_trace_does_not_attribute_other_task_time(self):
        r = self.create()
        r.checks(self.task, 'first')
        r.checks(dict(self.task, id='other'), 'second')
        self.assertEqual(task_trace.query(r.dir, 'artifact')['phase_timing']['check_executions'], 1)
        self.assertEqual(task_trace.query(r.dir, 'other')['phase_timing']['check_executions'], 1)
        self.assertEqual(task_trace.query(r.dir)['phase_timing']['check_executions'], 2)
        self.assertFalse(task_trace.query(r.dir, 'absent')['phase_timing']['available'])

    def test_workflow_overall_checks_use_same_phase_instrumentation(self):
        contract = dict(objective='Full workflow objective', scope='temporary files', authorization='fixture', memory=False,
            execution_policy=copy.deepcopy(fixture.EXECUTION), acceptance=[dict(id='artifact', description='yes', checks=self.task['checks'])])
        runner.save(self.repo/'contract-input.json', contract)
        w = workflow.Workflow(workflow.create(self.repo, self.repo/'contract-input.json'))
        w.check_total(fresh=True)
        self.assertEqual(w.state['criteria']['artifact']['status'], 'passed')
        self.assertEqual(runner.status(w.dir)['task_view']['phase_timing']['check_executions'], 1)

    def test_workflow_joins_only_bound_child_trace_and_never_adds_stage_totals_twice(self):
        contract = dict(objective='Full workflow', scope='temporary files', authorization='fixture', memory=False,
            execution_policy=copy.deepcopy(fixture.EXECUTION), acceptance=[dict(id='artifact', description='yes', checks=self.task['checks'])])
        runner.save(self.repo/'contract-input.json', contract)
        w = workflow.Workflow(workflow.create(self.repo, self.repo/'contract-input.json'))
        w.stage(copy.deepcopy(self.plan), ['artifact'])
        child = runner.Runner(w.state['active_run'])
        child.checks(self.task, 'child')
        w.check_total(fresh=True)
        view = runner.status(w.dir)
        self.assertEqual(view['phase_timing']['source_count'], 2)
        self.assertEqual(view['phase_timing']['check_executions'], 2)
        self.assertEqual(view['task_view']['stages'][0]['phase_timing']['check_executions'], 1)
        self.assertLessEqual(view['phase_timing']['covered_seconds'], view['phase_timing']['observed_wall_seconds']+.00001)
        # The original parent binding remains authoritative for joins.
        binding = runner.read(child.dir/'parent.json'); binding['workflow_id'] = 'unrelated'
        runner.save(child.dir/'parent.json', binding)
        view = runner.status(w.dir)['task_view']
        self.assertEqual(view['phase_timing']['check_executions'], 1)
        self.assertTrue(view['observation_errors'])

    def test_legacy_without_spans_is_unknown_and_ledger_unchanged(self):
        r = self.create()
        before = (r.dir/'state.json').read_bytes()
        result = metrics.view(r.dir, {'active_seconds': 15, 'wait_seconds': 20, 'yield_seconds': 1})
        self.assertFalse(result['available'])
        self.assertEqual(result['v2_ledger']['active_seconds'], 15)
        self.assertEqual(result['v2_ledger']['wait_seconds'], 20)
        self.assertIsNone(result['phases_seconds']['model_request'])
        self.assertEqual(before, (r.dir/'state.json').read_bytes())


class ControlledBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = fixture.benchmark()

    def test_three_arms_same_initial_files_contract_permissions_and_artifacts(self):
        arms = self.result['arms']
        for field in ('initial_files', 'task_sha256', 'checks', 'profile', 'artifacts'):
            self.assertEqual(arms['foreground'][field], arms['legacy_ato'][field], field)
            self.assertEqual(arms['foreground'][field], arms['warm_ato'][field], field)
        self.assertTrue(self.result['artifacts_equal'])

    def test_two_verified_exact_turns_per_arm_with_actual_file_results(self):
        for name, arm in self.result['arms'].items():
            self.assertEqual(arm['counts']['native_turns'], 2)
            self.assertEqual(arm['counts']['model_requests'], 2)
            self.assertEqual(len({t['turn_id'] for t in arm['exact_protocol_turns']}), 2)
            sessions = {t['native_id'] for t in arm['exact_protocol_turns']}
            self.assertEqual(len(sessions), 2 if name == 'legacy_ato' else 1)
            self.assertTrue(arm['independent_checks_passed'])
            self.assertTrue(arm['final_check_fresh'])
            self.assertEqual(arm['artifacts']['artifact.txt'], hashlib.sha256(b'first\nsecond\n').hexdigest())

    def test_measured_initialization_read_and_repeated_check_counts_reduced(self):
        cold, warm = [self.result['arms'][name]['counts'] for name in ('legacy_ato', 'warm_ato')]
        self.assertEqual((cold['cold_initializations'], warm['cold_initializations']), (2, 1))
        self.assertEqual((cold['cli_process_startups'], warm['cli_process_startups']), (2, 2))
        self.assertLess(warm['tool_read_count'], cold['tool_read_count'])
        self.assertLess(warm['check_executions'], cold['check_executions'])
        self.assertEqual(warm['check_reuses'], 2)
        for receipt in self.result['arms']['warm_ato']['reuse_provenance']:
            self.assertEqual(receipt['returncode'], 0)
            self.assertEqual(len(receipt['source_sha256']), 64)
            self.assertLessEqual(receipt['started_at'], receipt['finished_at'])

    def test_fixture_never_promoted_to_actual_or_provider_speedup(self):
        self.assertTrue(self.result['fixture_only'])
        self.assertFalse(self.result['actual_native_cli'])
        self.assertEqual(self.result['provider_performance'], 'unknown')
        for arm in self.result['arms'].values():
            self.assertEqual(arm['provider_speedup'], 'unknown')
            self.assertIsNone(arm['timing']['phases_seconds']['model_request'])
            self.assertGreater(arm['wall_seconds'], 0)
        with self.assertRaises(ValueError):
            actual.validate_proof(self.result)

    def test_origin_schema_requires_exact_bounded_three_arm_evidence(self):
        schema = actual.PROOF_SCHEMA
        self.assertEqual(schema['max_native_turns'], 12)
        self.assertEqual(schema['max_wall_seconds'], 1200)
        self.assertEqual(schema['samples_per_arm'], 2)
        self.assertEqual(schema['turns_per_sample'], 2)
        self.assertTrue(schema['origin_only'])
        self.assertFalse(schema['fixture_allowed'])
        for data in ({}, dict(kind=schema['kind'], fixture_only=False), dict(self.result, kind=schema['kind'])):
            with self.assertRaises(ValueError):
                actual.validate_proof(data)

if __name__ == '__main__':
    unittest.main()
