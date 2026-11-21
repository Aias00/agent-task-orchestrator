"""Task-summary CLI and query purity with private synthetic state only."""
import copy
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ato_config
import execution_accounting as accounting
import runner
import task_presentation
import task_trace
import workflow


ROOT = Path(runner.__file__).resolve().parent
POLICY = dict(version=2, max_continuations=6, max_waits=4, max_yields=4, max_no_progress=3)


class TaskSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-task-view-'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = dict(os.environ, ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'codex'),
                        CTASK_CODEX_BIN='/nonexistent/no-model')
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            self.env.pop(key, None)
        self.env_patch = patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)

    def plan(self, v2=False):
        plan = dict(objective='Synthetic full stage objective', memory=False,
                    tasks=[dict(id='work', prompt='Synthetic stage work',
                                checks=[[sys.executable, '-c', 'pass']])])
        if v2:
            plan['execution_policy'] = copy.deepcopy(POLICY)
        return plan

    def create(self, v2=False):
        source = self.repo/'plan-input.json'; runner.save(source, self.plan(v2))
        return runner.create(self.repo, source)

    def create_workflow(self, manual=False, v2=False):
        contract = dict(objective='The complete authorized objective', scope='Synthetic repository only',
                        authorization='Test fixture only', memory=False,
                        acceptance=[dict(id='integration', description='Total integration',
                                         checks=[[sys.executable, '-c', 'pass']])])
        if manual:
            contract['acceptance'].append(dict(id='human', kind='manual', description='Actual human acceptance'))
        if v2:
            contract['execution_policy'] = copy.deepcopy(POLICY)
        path = self.repo/'contract-input.json'; runner.save(path, contract)
        # Create a local workflow with no runtime/hub binding. Registration has
        # no real authority and must not fall back to the caller's actual home.
        return workflow.create(self.repo, path)

    def change(self, directory, **updates):
        state = runner.read(directory/'state.json'); state.update(updates)
        runner.save(directory/'state.json', state)
        return state

    def cli(self, directory, fmt='json', command='task-summary'):
        argv = [sys.executable, str(ROOT/'runner.py'), command, str(directory)]
        if command == 'task-summary':
            argv += ['--format', fmt]
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if fmt == 'json' else result.stdout

    def snapshot(self):
        # Include directory creation and file metadata, not just state.json.
        return {str(p.relative_to(self.root)): ('directory' if p.is_dir() else
                    (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode))
                for p in self.root.rglob('*') if not p.is_symlink()}

    def stage(self, directory, v2=False):
        host = workflow.Workflow(directory)
        host.stage(self.plan(v2), ['integration'])
        return Path(host.state['active_run'])

    def test_created_json_snapshot_preserves_legacy_unknown(self):
        d = self.create()
        view = self.cli(d)
        self.assertEqual({k:view[k] for k in ('objective', 'completion_scope', 'raw_status',
                         'effective_status', 'process_status', 'needs_user', 'read_only')},
                         dict(objective='Synthetic full stage objective', completion_scope='stage_only',
                              raw_status='created', effective_status='created',
                              process_status='no_verified_process', needs_user=False, read_only=True))
        counts = view['execution_accounting']
        self.assertEqual(counts['invocations'], 0)
        for key in ('failures', 'continuations', 'waits', 'yields', 'active_seconds', 'wait_seconds'):
            self.assertIsNone(counts[key], key)
        self.assertEqual([r['id'] for r in view['remaining']], ['work'])

    def test_actual_text_cli_snapshot(self):
        d = self.create()
        text = self.cli(d, 'text')
        self.assertIn('Task: '+d.name+' (stage; stage_only)\nObjective: Synthetic full stage objective', text)
        self.assertIn('Status: raw=created; effective=created; process=no_verified_process', text)
        self.assertIn('failures=unknown', text)
        self.assertIn('Owner: pid=unknown; absent; identity_verified=False', text)
        self.assertIn('Remaining acceptance: work=pending', text)
        self.assertIn('STOP: False; needs_user=False', text)

    def test_all_cli_queries_have_no_state_or_home_side_effects(self):
        d = self.create()
        before = self.snapshot()
        for command, fmt in [('status', 'json'), ('task-summary', 'json'), ('task-summary', 'text')]:
            self.cli(d, fmt, command)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(Path(self.env['ATO_HOME']).exists())
        self.assertFalse(Path(self.env['CODEX_HOME']).exists())

    def test_status_preserves_existing_fields_and_adds_same_task_view(self):
        d = self.create(True)
        state = runner.read(d/'state.json'); status = self.cli(d, command='status')
        for key, value in state.items():
            self.assertEqual(status[key], value)
        self.assertEqual(status['task_view'], self.cli(d))

    def test_stale_running_without_pid_is_not_live(self):
        d = self.create(); self.change(d, status='running')
        view = self.cli(d)
        self.assertEqual((view['raw_status'], view['effective_status']), ('running', 'interrupted'))
        self.assertFalse(view['needs_user'])
        self.assertIn('no verified executor', view['next_step'])
        self.assertEqual(runner.read(d/'state.json')['status'], 'running')

    def test_dead_running_pid_and_child_are_retained_as_history(self):
        d = self.create()
        self.change(d, status='running', owner_pid=99999999, owner_identity='old-start',
                    active_process=dict(pid=99999998, identity='old-child', phase='agent', evidence='old.log'))
        view = self.cli(d)
        self.assertEqual(view['effective_status'], 'interrupted')
        self.assertEqual(view['processes']['owner']['status'], 'dead')
        self.assertEqual(view['processes']['worker']['evidence'], 'old.log')
        self.assertEqual(view['processes']['worker']['group_alive'], False)

    def test_live_process_requires_matching_start_identity(self):
        d = self.create(); identity = runner.process_identity(os.getpid())
        self.change(d, status='running', owner_pid=os.getpid(), owner_identity=identity or 'unavailable-in-worker')
        view = self.cli(d)
        self.assertEqual(view['process_status'], 'verified_active' if identity else 'unknown')
        self.assertEqual(view['effective_status'], 'running' if identity else 'process_unverified')
        # Some worker sandboxes deny ps. Keep that unknown in the actual CLI;
        # also exercise matching identity behavior using a synthetic observer.
        self.change(d, status='running', owner_pid=os.getpid(), owner_identity='synthetic-start')
        with patch('runner.process_identity', return_value='synthetic-start'):
            self.assertEqual(runner.status(d)['task_view']['effective_status'], 'running')
        self.change(d, status='running', owner_pid=os.getpid(), owner_identity='reused-pid')
        view = self.cli(d)
        self.assertEqual(view['effective_status'], 'process_unverified')
        self.assertTrue(view['processes']['owner']['alive'])
        self.assertFalse(view['processes']['owner']['identity_verified'])

    def test_legacy_live_pid_without_identity_is_unknown(self):
        d = self.create(); self.change(d, status='running', owner_pid=os.getpid())
        view = self.cli(d)
        self.assertEqual(view['process_status'], 'unknown')
        self.assertEqual(view['capabilities']['resumable'], 'unknown')

    def test_stop_remains_and_query_never_replays_unknown_delivery(self):
        d = self.create(True)
        state = runner.read(d/'state.json'); state['status'] = 'blocked'
        state['tasks']['work'].update(status='blocked', delivery_unknown=True, feedback='Unverified native delivery')
        runner.save(d/'state.json', state)
        before = self.snapshot(); view = self.cli(d)
        self.assertEqual(view['effective_status'], 'delivery_unknown')
        self.assertIn('do not replay', view['next_step'])
        self.assertEqual(before, self.snapshot())
        (d/'STOP').touch(); view = self.cli(d)
        self.assertEqual(view['effective_status'], 'stopped')
        self.assertTrue(view['stop_requested']); self.assertTrue((d/'STOP').exists())

    def test_v2_counts_are_distinct_from_old_attempts(self):
        d = self.create(True)
        state = runner.read(d/'state.json'); row = state['tasks']['work']
        row.update(attempts=9, attempt_sequence=9, status='failed')
        row['execution_accounting'].update(invocations=9, continuations=3, waits=2, yields=1, failures=2,
                                           active_seconds=4.5, wait_seconds=15, yield_seconds=2)
        state.update(status='blocked', execution_accounting=accounting.total([row]))
        runner.save(d/'state.json', state)
        before = self.snapshot(); view = self.cli(d)
        counts = view['execution_accounting']
        self.assertEqual([counts[k] for k in ('invocations', 'continuations', 'waits', 'yields', 'failures')], [9,3,2,1,2])
        self.assertEqual((counts['active_seconds'], counts['wait_seconds'], counts['yield_seconds']), (4.5,15,2))
        self.assertEqual(view['legacy_attempts'], dict(work=9))
        self.assertEqual(before, self.snapshot())

    def test_unavailable_legacy_or_invalid_v2_ledger_is_unknown_without_repair(self):
        legacy = self.create(); state = runner.read(legacy/'state.json')
        state['tasks']['work'].pop('attempts')
        runner.save(legacy/'state.json', state)
        before = self.snapshot(); view = self.cli(legacy)
        self.assertIsNone(view['execution_accounting']['invocations'])
        self.assertIsNone(view['execution_accounting']['failures'])
        self.assertEqual(before, self.snapshot())
        v2 = self.create(True); state = runner.read(v2/'state.json')
        state['tasks']['work']['execution_accounting'] = None
        runner.save(v2/'state.json', state)
        before = self.snapshot(); view = self.cli(v2)
        self.assertEqual(view['execution_accounting']['knowledge'], 'unknown')
        self.assertIsNone(view['execution_accounting']['failures'])
        self.assertTrue(view['observation_errors'])
        self.assertEqual(before, self.snapshot())

    def test_wait_is_finite_and_unsettled_time_is_projection_only(self):
        d = self.create(True); state = runner.read(d/'state.json')
        state['tasks']['work'].update(status='waiting', wait_reason='Synthetic dependency not ready',
                                    next_check_at=runner.now()+60, wait_accounted_at=runner.now()-10)
        state['tasks']['work']['execution_accounting'].update(waits=1, wait_seconds=2, active_seconds=3)
        state['status'] = 'running'; runner.save(d/'state.json', state)
        before = self.snapshot(); view = self.cli(d)
        self.assertEqual(view['effective_status'], 'waiting')
        self.assertEqual(view['execution_accounting']['active_seconds'], 3)
        self.assertEqual(view['execution_accounting']['wait_seconds'], 2)
        self.assertGreaterEqual(view['execution_accounting']['unsettled_wait_seconds'], 10)
        self.assertTrue(view['waits'][0]['valid']); self.assertFalse(view['needs_user'])
        self.assertEqual(before, self.snapshot())

    def test_expired_or_incomplete_wait_does_not_claim_valid_external_wait(self):
        d = self.create(True); state = runner.read(d/'state.json')
        state['status'] = 'waiting'
        state['tasks']['work'].update(status='waiting', next_check_at=runner.now()-1)
        runner.save(d/'state.json', state)
        view = self.cli(d)
        self.assertEqual(view['effective_status'], 'wait_unverified')
        self.assertFalse(view['waits'][0]['valid'])

    def test_workflow_planner_wait_uses_actual_workflow_deadline_and_legacy_unknowns(self):
        for v2 in (False, True):
            with self.subTest(v2=v2):
                d = self.create_workflow(v2=v2)
                deadline = runner.now()+60
                state = self.change(d, status='waiting', next_check_at=deadline,
                                    error='Synthetic external planner dependency')
                if v2:
                    state['planning_execution'].update(status='waiting',
                        wait_reason=state['error'], wait_accounted_at=runner.now()-5)
                    state['planning_execution']['execution_accounting']['waits'] = 1
                    runner.save(d/'state.json', state)
                before = self.snapshot(); view = self.cli(d)
                self.assertEqual(view['effective_status'], 'waiting')
                self.assertEqual(view['waits'], [dict(id='planner', status='waiting',
                    reason='Synthetic external planner dependency', next_check_at=deadline, valid=True)])
                self.assertFalse(view['needs_user'])
                self.assertEqual(view['execution_accounting']['waits'], 1 if v2 else None)
                self.assertEqual(before, self.snapshot())
                self.change(d, next_check_at=runner.now()-1)
                expired = self.cli(d)
                self.assertEqual(expired['effective_status'], 'wait_unverified')
                self.assertFalse(expired['waits'][0]['valid'])

    def test_workflow_stage_success_does_not_accept_full_objective(self):
        d = self.create_workflow(True, True); child = self.stage(d, True)
        state = runner.read(child/'state.json'); state['status'] = 'completed'
        state['tasks']['work']['status'] = 'completed'; runner.save(child/'state.json', state)
        self.change(d, status='needs_input', criteria=dict(integration=dict(status='passed', evidence=[]),
                                                        human=dict(status='needs_input', evidence=[])))
        view = self.cli(d)
        self.assertEqual(view['completion_scope'], 'overall')
        self.assertEqual(view['effective_status'], 'needs_input')
        self.assertEqual(view['current_stage']['raw_status'], 'completed')
        self.assertEqual([p['id'] for p in view['remaining']], ['human'])
        self.assertTrue(view['needs_user']); self.assertIn('canonical ask', view['next_step'])
        stage = self.cli(child)
        self.assertEqual(stage['objective'], 'The complete authorized objective')
        self.assertEqual(stage['stage_objective'], 'The complete authorized objective\nStage: Synthetic full stage objective')
        self.assertEqual(stage['completion_scope'], 'stage_only')
        self.assertEqual(stage['overall']['raw_status'], 'needs_input')
        self.assertTrue(stage['needs_user'])

    def test_workflow_aggregates_all_stage_and_planning_ledgers_once(self):
        d = self.create_workflow(v2=True); first = self.stage(d, True); second = self.stage(d, True)
        for child, n in [(first, 2), (second, 3)]:
            state = runner.read(child/'state.json')
            state['tasks']['work']['execution_accounting'].update(invocations=n, failures=1, active_seconds=n)
            runner.save(child/'state.json', state)
        state = runner.read(d/'state.json')
        state['planning_execution']['execution_accounting'].update(invocations=1, waits=1, wait_seconds=10)
        state['host_execution']['execution_accounting']['active_seconds'] = 4
        # Deliberately stale cached workflow total must not hide child history.
        runner.save(d/'state.json', state)
        view = self.cli(d); counts = view['execution_accounting']
        self.assertEqual((counts['invocations'], counts['failures'], counts['waits']), (6,2,1))
        self.assertEqual(counts['active_seconds'], 9)
        self.assertEqual(len(view['stages']), 2)
        self.assertEqual(view['current_stage']['id'], second.name)

    def test_missing_stage_and_invalid_parent_are_reported_without_fake_zero_totals(self):
        d = self.create_workflow(v2=True); child = self.stage(d, True)
        (child/'state.json').unlink()
        view = self.cli(d)
        self.assertTrue(view['observation_errors']); self.assertIsNone(view['execution_accounting']['failures'])
        self.assertIsNone(view['current_stage'])
        standalone = self.create(); runner.save(standalone/'parent.json', dict(workflow_dir=str(d), workflow_id='wrong'))
        view = self.cli(standalone)
        self.assertEqual(view['parent_binding'], 'invalid'); self.assertIsNone(view['overall'])

    def test_host_handoff_is_quiet_and_not_a_user_approval_request(self):
        d = self.create(); state = runner.read(d/'state.json')
        state.update(status='needs_host', error='Frozen checks require original host')
        state['tasks']['work'].update(status='needs_host', feedback='Run original argv only')
        runner.save(d/'state.json', state)
        view = self.cli(d)
        self.assertEqual(view['effective_status'], 'needs_host'); self.assertFalse(view['needs_user'])
        self.assertIn('frozen authorized handoff', view['next_step'])

    def test_recovery_unknown_delivery_query_does_not_initialize_mailbox_or_native_auth(self):
        d = self.create_workflow(); self.change(d, status='blocked')
        runner.save(d/'recovery.json', dict(enabled=True, status='requested', requests=[
                    dict(id='fixture-request', state='delivery_unknown', notification=dict(id='question', state='pending'))],
                    max_requests=3, deadline=runner.now()+60, thread_id='synthetic-never-probed'))
        before = self.snapshot()
        with patch('transport.managed_bus', side_effect=AssertionError('query opened mailbox')), \
             patch('session_router.activity', side_effect=AssertionError('query probed native activity')), \
             patch('runner.save', side_effect=AssertionError('query wrote state')), \
             patch('interaction.ask', side_effect=AssertionError('query sent question')), \
             patch('control.record', side_effect=AssertionError('query marked presented')):
            status = runner.status(d)
        view = self.cli(d)
        self.assertEqual(view['recovery']['latest']['state'], 'delivery_unknown')
        self.assertEqual(status['task_view']['effective_status'], 'delivery_unknown')
        self.assertEqual(before, self.snapshot())

    def test_trace_cli_is_read_only_and_native_success_is_not_full_acceptance(self):
        d = self.create()
        task_trace.append(d, 'attempt_started', event_id='fixture-start', task='work', attempt=1)
        task_trace.append(d, 'native_success', event_id='fixture-success', task='work', attempt=1)
        before = self.snapshot()
        status = self.cli(d, command='status')
        self.assertEqual(status['trace_summary']['native'], 'native_success')
        self.assertEqual(status['task_view']['effective_status'], 'created')
        self.assertEqual(before, self.snapshot())

    def test_completed_metadata_with_remaining_checks_is_not_accepted(self):
        d = self.create(); self.change(d, status='completed')
        view = self.cli(d)
        self.assertEqual(view['effective_status'], 'acceptance_incomplete')
        self.assertEqual(view['raw_status'], 'completed')

    def test_live_wal_telemetry_query_keeps_original_database_and_sidecars_untouched(self):
        d = self.create()
        task_trace.append(d, 'attempt_started', event_id='start', task='work', attempt=1)
        with sqlite3.connect(d/'trace.sqlite3') as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('SELECT count(*) FROM events').fetchone()
            task_trace.append(d, 'native_error', event_id='live-wal-error', task='work', attempt=1)
            self.assertTrue(Path(str(d/'trace.sqlite3')+'-wal').exists())
            before = self.snapshot()
            status = self.cli(d, command='status')
            self.assertEqual(status['trace_summary']['native'], 'native_error')
            self.assertEqual(status['task_view']['effective_status'], 'created')
            self.assertEqual(before, self.snapshot())

    def test_independent_running_branch_is_not_hidden_by_an_external_wait(self):
        plan = self.plan(True)
        plan['tasks'].append(dict(id='independent', prompt='Synthetic independent work', checks=[[sys.executable, '-c', 'pass']]))
        source = self.repo/'two-branches.json'; runner.save(source, plan); d = runner.create(self.repo, source)
        state = runner.read(d/'state.json')
        state.update(status='running', owner_pid=os.getpid(), owner_identity='synthetic-active')
        state['tasks']['work'].update(status='waiting', wait_reason='Synthetic wait', next_check_at=runner.now()+60)
        state['tasks']['independent']['status'] = 'running'; runner.save(d/'state.json', state)
        with patch('runner.process_identity', return_value='synthetic-active'):
            view = runner.status(d)['task_view']
        self.assertEqual(view['effective_status'], 'running')
        self.assertEqual(view['waits'][0]['status'], 'waiting')

    def test_actual_workflow_manual_gate_remains_after_successful_stage(self):
        from test_continuation_lifecycle import FAKE
        fake = self.repo/'synthetic-cli'; fake.write_text('#!'+sys.executable+'\n'+FAKE); fake.chmod(0o755)
        d = self.create_workflow(True, True); child = self.stage(d, True)
        result = subprocess.run([sys.executable, str(ROOT/'runner.py'), 'run', str(d)],
                                env=dict(self.env, CTASK_CODEX_BIN=str(fake)),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.snapshot(); view = self.cli(d)
        self.assertEqual(view['raw_status'], 'needs_input')
        self.assertEqual(view['effective_status'], 'needs_input')
        self.assertIsNone(view['current_stage'])
        self.assertEqual(view['stages'][-1]['effective_status'], 'completed')
        self.assertTrue(view['needs_user']); self.assertEqual([p['id'] for p in view['remaining']], ['human'])
        self.assertEqual(view['execution_accounting']['invocations'], 1)
        self.assertEqual(before, self.snapshot())

    def test_real_runner_lifecycle_accounting_is_visible_in_cli(self):
        from test_continuation_lifecycle import FAKE
        fake = self.repo/'synthetic-cli'; fake.write_text('#!'+sys.executable+'\n'+FAKE); fake.chmod(0o755)
        plan = self.plan(True)
        plan['tasks'][0].update(prompt='continue3', checks=[[sys.executable, '-c',
                                   "from pathlib import Path; assert Path('artifact').exists()"]])
        source = self.repo/'real-lifecycle.json'; runner.save(source, plan); d = runner.create(self.repo, source)
        env = dict(self.env, CTASK_CODEX_BIN=str(fake))
        result = subprocess.run([sys.executable, str(ROOT/'runner.py'), 'run', str(d)], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.snapshot(); view = self.cli(d)
        self.assertEqual(view['effective_status'], 'completed')
        self.assertEqual(view['execution_accounting']['invocations'], 4)
        self.assertEqual(view['execution_accounting']['continuations'], 3)
        self.assertEqual(view['execution_accounting']['failures'], 0)
        self.assertEqual(view['remaining'], [])
        self.assertEqual(view['completion_scope'], 'stage_only')
        self.assertEqual(before, self.snapshot())


class SuccessorSummaryTests(unittest.TestCase):
    """Use existing synthetic native identities; no real session or hub calls."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-successor-view-'); self.addCleanup(self.tmp.cleanup)
        self.env_patch = patch.dict(os.environ, ATO_HOME=self.tmp.name+'/ato')
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        from test_workflow_lineage import LineageTests
        self.fixture = LineageTests('runTest'); self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        # The old fixture creates only synthetic endpoints in this fresh ATO root.

    def test_verified_successor_keeps_raw_blocked_stop_and_all_history(self):
        f = self.fixture; (f.f.d/'STOP').touch(); f.link()
        before = (f.f.d/'state.json').read_bytes()
        result = subprocess.run([sys.executable, str(ROOT/'runner.py'), 'task-summary', str(f.f.d), '--format', 'json'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr); view = json.loads(result.stdout)
        self.assertEqual((view['raw_status'], view['effective_status']), ('blocked', 'completed'))
        self.assertEqual(view['delivery']['successor'], str(f.successor))
        self.assertEqual(view['delivery']['validation'], 'valid')
        self.assertTrue(view['stop_requested']); self.assertFalse(view['needs_user'])
        self.assertEqual([p['id'] for p in view['remaining']], ['file'])
        self.assertEqual(before, (f.f.d/'state.json').read_bytes()); self.assertTrue((f.f.d/'STOP').exists())
        self.assertIsNone(view['execution_accounting']['failures'])

    def test_changed_successor_evidence_revokes_effective_completion(self):
        f = self.fixture; f.link(); (f.f.repo/'check.log').write_text('changed fixture evidence')
        view = runner.status(f.f.d)['task_view']
        self.assertEqual(view['raw_status'], 'blocked')
        self.assertNotEqual(view['effective_status'], 'completed')
        self.assertEqual(view['delivery']['validation'], 'invalid')

    def test_canonical_question_is_visible_without_presentation_or_acceptance(self):
        import interaction
        f = self.fixture
        with patch('interaction.subprocess.run', side_effect=f.f.transport):
            question = f.f.ask()
        path = interaction.question_path(question['id']); before = path.read_bytes()
        view = runner.status(f.f.d)['task_view']
        self.assertTrue(view['needs_user'])
        self.assertEqual(view['interaction']['questions'][0]['state'], 'pending')
        self.assertEqual(before, path.read_bytes())
        state = runner.read(f.f.d/'state.json'); state['execution_revision'] = 1
        runner.save(f.f.d/'state.json', state)
        stale = runner.status(f.f.d)['task_view']
        self.assertFalse(stale['needs_user']); self.assertEqual(stale['interaction']['questions'], [])
        self.assertEqual(before, path.read_bytes())

    def test_quiet_policy_and_failure_dedup_are_unchanged_by_cli_queries(self):
        import control
        import notifications
        import failure_attention
        import test_interaction
        f = self.fixture
        with patch.dict(os.environ, CODEX_THREAD_ID=test_interaction.HUB):
            control.enable('Synthetic fixture controller', 60, 0)
            notifications.configure('user_input_only', 'Fixture quiet policy')
            failure_attention.configure('Synthetic once-only failure notice')
        control.register(f.f.d)
        with control.database() as con:
            row = json.loads(con.execute('select body from workflows where path=?', (str(f.f.d),)).fetchone()[0])
        with patch('failure_attention.origin_busy', return_value=False), \
             patch('recovery.handler_status', return_value='unhandled'):
            failure_attention.observe(row, f.f.d, runner.read(f.f.d/'state.json'), runner.read(f.f.d/'contract.json'))
        attention = [a for a in control.rows(True) if a['kind'] == 'failure_attention']
        self.assertEqual(len(attention), 1)
        with patch.dict(os.environ, CODEX_THREAD_ID=test_interaction.HUB):
            control.record(attention[0]['id'], state='presented', note='Synthetic fixture presentation')
        before = {str(p):p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()}
        for _ in range(2):
            result = subprocess.run([sys.executable, str(ROOT/'runner.py'), 'task-summary', str(f.f.d), '--format', 'json'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {str(p):p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()})
        with patch('failure_attention.origin_busy', return_value=False), \
             patch('recovery.handler_status', return_value='unhandled'):
            failure_attention.observe(row, f.f.d, runner.read(f.f.d/'state.json'), runner.read(f.f.d/'contract.json'))
        attention = [a for a in control.rows(True) if a['kind'] == 'failure_attention']
        self.assertEqual(len(attention), 1); self.assertFalse(notifications.disposition(attention[0])['notify'])
        for kind in ('workflow', 'workflow_result', 'host_handoff', 'operational'):
            self.assertFalse(notifications.disposition(dict(kind=kind, workflow=str(f.f.d)))['notify'])
