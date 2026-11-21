"""Offline v2 lifecycle through real Runner/Workflow processes and frozen checks.

Every case has a fresh repository, private empty ATO/Codex roots and a scripted
local CLI. No installed launcher, provider, native account or live task is used.
"""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import agents
import execution_accounting as accounting
import runner
import workflow


POLICY = dict(version=2, max_continuations=6, max_waits=4, max_yields=4, max_no_progress=3)
FAKE = '''
import json, sys, time
from pathlib import Path
prompt=sys.stdin.read()
out=Path(sys.argv[sys.argv.index('-o')+1])
if 'You are the planner for an already authorized total task' in prompt:
 mode='planner-wait' if 'planner-wait' in prompt else 'planner-stage'
 out.write_text(json.dumps(dict(decision='waiting' if mode=='planner-wait' else 'stage',
  reason='synthetic external dependency',tasks=[] if mode=='planner-wait' else [dict(id='work',prompt='continue3',depends_on=[],covers=['full'])],retry_after_seconds=1)))
 sys.exit(0)
mode=prompt.split('Task: ',1)[1].split('\\n',1)[0]
counter=Path('.fixture-count-'+mode)
n=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(n))
status='complete'
if mode=='timeout': time.sleep(5)
if mode=='stop': Path('entered').touch(); time.sleep(20)
if mode=='nonzero': sys.exit(7)
if mode=='malformed': out.write_text('{'); sys.exit(0)
if mode=='continue3':
 Path('feature.py').write_text('step = '+str(n))
 if n<4: status='continue'
 else: Path('artifact').touch()
elif mode=='no-progress': status='continue'
elif mode=='always-progress':
 Path('feature.py').write_text('step = '+str(n)); status='continue'
elif mode=='waiting':
 if n<3: status='waiting'
 else: Path('artifact').touch()
elif mode=='waiting-forever': status='waiting'
elif mode=='yield':
 if n<2: status='yielded'
 else: Path('artifact').touch()
elif mode=='yield-forever': status='yielded'; Path('feature.py').write_text('step = '+str(n))
elif mode=='host': status='needs_host'; Path('artifact').touch()
elif mode=='blocked': status='blocked'
elif mode=='false-complete': pass
elif mode=='independent': Path('independent').touch()
elif mode=='bad-wait': status='waiting'
else: Path('artifact').touch()
out.write_text(json.dumps(dict(status=status,summary='synthetic condition '+str(n),next_step='continue original scope',
 blocker='fixture external dependency' if status in ('waiting','blocked') else '',retry_after_seconds=0 if mode=='bad-wait' else 1)))
'''


class ContinuationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-v2-lifecycle-')
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)/'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.fake = self.repo/'fake-cli'
        self.fake.write_text('#!'+sys.executable+'\n'+FAKE); self.fake.chmod(0o755)
        clean = dict(ATO_HOME=str(Path(self.tmp.name)/'ato'), CODEX_HOME=str(Path(self.tmp.name)/'codex'),
                     CTASK_CODEX_BIN=str(self.fake))
        self.env_patch = patch.dict(os.environ, clean)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            if key in os.environ:
                old = os.environ.pop(key)
                self.addCleanup(os.environ.__setitem__, key, old)
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(sig, value) for sig, value in handlers.items()])

    def task(self, mode='continue3', key='work', deps=None, expression=None):
        return dict(id=key, prompt=mode, depends_on=deps or [], checks=[[sys.executable, '-c', expression or
                    "from pathlib import Path; assert Path('artifact').exists()"]])

    def plan(self, mode='continue3', **changes):
        plan = dict(objective='Whole synthetic objective', memory=False, execution_policy=copy.deepcopy(POLICY),
                    limits=dict(runtime_seconds=45, attempt_seconds=3, check_seconds=2, max_attempts=2),
                    tasks=[self.task(mode)])
        plan.update(changes)
        return plan

    def create(self, plan):
        source = self.repo/'input.json'; runner.save(source, plan)
        return runner.create(self.repo, source)

    def run_cli(self, directory, *args):
        result = subprocess.run([sys.executable, runner.__file__, 'run', str(directory), *args],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        return runner.read(directory/'state.json')

    def account(self, state, key='work'):
        return state['tasks'][key]['execution_accounting']

    def test_three_continuations_then_completion_preserve_all_invocations(self):
        d = self.create(self.plan()); state = self.run_cli(d)
        self.assertEqual(state['status'], 'completed', state)
        row = state['tasks']['work']; value = self.account(state)
        self.assertEqual((row['attempts'], row['attempt_sequence'], value['invocations']), (4, 4, 4))
        self.assertEqual((value['continuations'], value['failures']), (3, 0))
        self.assertEqual([h['sequence'] for h in value['history']], [1, 2, 3, 4])
        checkpoint = runner.read(Path(value['history'][0]['checkpoint']))
        self.assertTrue(checkpoint['progress']['source_changed'])
        self.assertFalse(all(checkpoint['checks_after']))
        self.assertTrue(checkpoint['cleanup_confirmed'])

    def test_continue_with_failing_checks_does_not_spend_failures(self):
        d = self.create(self.plan('always-progress', execution_policy=POLICY | dict(max_continuations=2)))
        state = self.run_cli(d); value = self.account(state)
        self.assertEqual((value['invocations'], value['continuations'], value['failures']), (2, 2, 0))
        self.assertEqual(state['tasks']['work']['accounting_stop'], 'max_continuations')

    def test_no_progress_summary_changes_are_bounded_and_retry_does_not_reset(self):
        d = self.create(self.plan('no-progress')); state = self.run_cli(d)
        row = state['tasks']['work']; self.assertEqual(row['accounting_stop'], 'max_no_progress')
        self.assertEqual(self.account(state)['invocations'], 3)
        checkpoint = runner.read(Path(row['progress_checkpoint']))
        self.assertFalse(checkpoint['progress']['verified'])
        again = self.run_cli(d, '--retry-failed')
        self.assertEqual(self.account(again), self.account(state))
        self.assertEqual(again['tasks']['work']['attempts'], 3)

    def test_waits_record_real_time_excluded_from_active_and_run_independent_branch(self):
        tasks = [self.task('waiting'), self.task('independent', 'other', expression="from pathlib import Path; assert Path('independent').exists()")]
        d = self.create(self.plan(tasks=tasks)); state = self.run_cli(d)
        self.assertEqual(state['status'], 'completed', state)
        value = self.account(state)
        self.assertEqual((value['waits'], value['failures'], value['invocations']), (2, 0, 3))
        self.assertGreaterEqual(value['wait_seconds'], 1.8)
        # Row active time is actual commands, not its two-second external wait.
        events = [json.loads(line) for line in (d/'events.jsonl').read_text().splitlines()]
        results = [e['task'] for e in events if e['event'] == 'task_result']
        self.assertEqual(results[:3], ['work', 'other', 'work'])
        self.assertLess(value['active_seconds'], state['elapsed_seconds'] + .5)

    def test_cooperative_yield_cleans_process_and_preserves_failure_allowance(self):
        d = self.create(self.plan('yield')); state = self.run_cli(d)
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual((self.account(state)['yields'], self.account(state)['failures']), (1, 0))
        self.assertGreaterEqual(self.account(state)['yield_seconds'], .9)
        self.assertIsNone(state['active_process'])
        self.assertIn('yielded', runner.read(d/'result-schema.json')['properties']['status']['enum'])

    def test_wait_and_yield_counts_enforce_their_own_total_bounds(self):
        for mode, field, count in (('waiting-forever', 'max_waits', 'waits'), ('yield-forever', 'max_yields', 'yields')):
            with self.subTest(mode=mode):
                policy = POLICY | {field: 2}
                d = self.create(self.plan(mode, execution_policy=policy)); state = self.run_cli(d)
                self.assertEqual(state['tasks']['work']['accounting_stop'], field)
                self.assertEqual((self.account(state)[count], self.account(state)['failures']), (2, 0))
                again = self.run_cli(d, '--retry-failed')
                self.assertEqual(self.account(again)['invocations'], 2)

    def test_resume_retry_cannot_skip_durable_wait_deadline(self):
        d = self.create(self.plan('waiting')); owner = runner.Runner(d)
        owner.attempt(owner.plan['tasks'][0], '')
        before = runner.read(d/'state.json')
        self.assertEqual(before['tasks']['work']['status'], 'waiting')
        deadline = before['tasks']['work']['next_check_at']
        after = self.run_cli(d, '--retry-failed')
        history = self.account(after)['history']
        self.assertGreaterEqual(history[1]['started_at'], deadline)
        self.assertGreaterEqual(self.account(after)['wait_seconds'], 1.8)

    def test_false_complete_spends_exact_failure_allowance(self):
        d = self.create(self.plan('false-complete')); state = self.run_cli(d)
        self.assertEqual(state['tasks']['work']['status'], 'failed')
        self.assertEqual((self.account(state)['failures'], self.account(state)['invocations']), (2, 2))
        again = self.run_cli(d, '--retry-failed')
        self.assertEqual(self.account(again)['failures'], 2)
        self.assertEqual(again['tasks']['work']['attempts'], 2)

    def test_nonzero_and_malformed_structured_result_are_genuine_failures(self):
        for mode in ('nonzero', 'malformed', 'bad-wait'):
            with self.subTest(mode=mode):
                d = self.create(self.plan(mode)); state = self.run_cli(d)
                self.assertEqual((self.account(state)['failures'], self.account(state)['invocations']), (2, 2))

    def test_attempt_timeout_records_failure_and_reaps_actual_child(self):
        plan = self.plan('timeout'); plan['limits'].update(attempt_seconds=1, max_attempts=1)
        d = self.create(plan); state = self.run_cli(d)
        self.assertEqual(self.account(state)['failures'], 1)
        self.assertEqual(self.account(state)['history'][0]['outcome'], 'timeout')
        self.assertIsNone(state['active_process'])

    def test_stop_is_never_failure_or_permission_to_replay(self):
        d = self.create(self.plan('stop'))
        process = subprocess.Popen([sys.executable, runner.__file__, 'run', str(d)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 6
            while not (self.repo/'entered').exists() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertTrue((self.repo/'entered').exists())
            pid = runner.read(d/'state.json')['active_process']['pid']
            (d/'STOP').touch(); process.wait(timeout=6)
            state = runner.read(d/'state.json')
            self.assertEqual(state['status'], 'stopped')
            self.assertEqual(self.account(state)['failures'], 0)
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
            before = (d/'state.json').read_bytes()
            result = subprocess.run([sys.executable, runner.__file__, 'run', str(d)], capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((d/'STOP').exists()); self.assertEqual(before, (d/'state.json').read_bytes())
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=6)

    def test_unknown_acp_delivery_cannot_automatically_replay_even_explicit_retry_flag(self):
        plan = self.plan('malformed', agent=dict(kind='acp_pi'))
        d = self.create(plan)
        def command(plan, directory, label, schema, output, **kwargs):
            return [sys.executable, str(self.fake), '-o', str(output)]
        with patch.object(agents, 'command', side_effect=command):
            runner.Runner(d).execute()
            before = runner.read(d/'state.json')
            runner.Runner(d).execute(retry=True)
        state = runner.read(d/'state.json')
        self.assertTrue(state['tasks']['work']['delivery_unknown'])
        self.assertEqual(self.account(state), self.account(before))
        self.assertEqual(self.account(state)['invocations'], 1)

    def test_unfinished_receipt_is_quarantined_even_if_status_was_pending(self):
        d = self.create(self.plan()); state = runner.read(d/'state.json'); row = state['tasks']['work']
        row.update(attempts=1, attempt_sequence=1)
        accounting.begin(row, 1, time.time())
        runner.save(d/'state.json', state)
        after = self.run_cli(d, '--retry-failed')
        self.assertTrue(after['tasks']['work']['delivery_unknown'])
        self.assertEqual(self.account(after)['invocations'], 1)
        self.assertFalse((self.repo/'artifact').exists())

    def test_yield_is_not_accepted_when_process_cleanup_is_unconfirmed(self):
        d = self.create(self.plan('yield'))
        owner = runner.Runner(d)
        def cleanup_failed():
            raise PermissionError('synthetic cleanup cannot be confirmed')
        # The baseline acceptance probe has not launched any worker yet.
        owner.state['tasks']['work']['verified_checks'] = [False]
        owner.persist()
        with patch.object(owner, 'kill', side_effect=cleanup_failed):
            owner.execute()
        state = runner.read(d/'state.json')
        self.assertEqual(state['status'], 'cleanup_blocked')
        self.assertEqual(self.account(state)['yields'], 0)
        self.assertTrue(state['tasks']['work']['delivery_unknown'])
        self.assertIsNotNone(state['active_process'])

    def test_changed_acceptance_can_verify_progress_without_source_changes(self):
        d = self.create(self.plan('no-progress'))
        owner = runner.Runner(d); row = owner.state['tasks']['work']
        owner.verified_checks(owner.plan['tasks'][0], 'test-progress-baseline')
        self.assertEqual(row['verified_checks'], [False])
        (self.repo/'artifact').touch()
        # The unchanged-source result still gets actual host check observations.
        owner.attempt(owner.plan['tasks'][0], '')
        observed = runner.read(Path(row['progress_checkpoint']))['progress']
        self.assertTrue(observed['checks_improved'])
        self.assertFalse(observed['source_changed'])
        self.assertEqual(self.account(owner.state)['failures'], 0)

    def test_host_checks_repeat_without_worker_invocations_or_failure_charges(self):
        plan = self.plan('host', host_handoff=dict(source='synthetic local test', instructions='Frozen checks only'))
        d = self.create(plan); state = self.run_cli(d)
        self.assertEqual(state['status'], 'needs_host')
        with patch.object(runner.Runner, 'attempt', side_effect=AssertionError('host checks must not run workers')):
            runner.Runner(d).execute(host_checks=True)
            runner.Runner(d).execute(host_checks=True)
        after = runner.read(d/'state.json')
        self.assertEqual(after['status'], 'completed')
        for key in ('invocations', 'failures', 'continuations', 'waits', 'yields', 'history'):
            self.assertEqual(self.account(after)[key], self.account(state)[key])
        self.assertEqual(after['tasks']['work']['attempts'], 1)

    def test_failed_host_checks_and_selected_rechecks_do_not_recharge_worker(self):
        from host_recheck import recheck
        contract = self.contract('false-complete')
        contract['host_handoff'] = dict(source='synthetic authorization', instructions='Frozen isolated checks only')
        d = self.create_workflow(contract); state = self.run_cli(d)
        child = Path(state['active_run']); prior = runner.read(child/'state.json')
        self.assertEqual(self.account(prior)['failures'], 2)
        result = recheck(d, 'work', 'Synthetic repair still fails')
        self.assertFalse(result['passed'])
        (self.repo/'artifact').touch()
        result = recheck(d, 'work', 'Synthetic actual artifact repair')
        self.assertTrue(result['passed'])
        after = runner.read(child/'state.json')
        self.assertEqual((self.account(after)['failures'], self.account(after)['invocations']), (2, 2))
        self.assertEqual(self.account(prior)['history'], self.account(after)['history'])
        final = self.run_cli(d)
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['execution_accounting']['failures'], 2)

    def test_legacy_plan_retains_old_attempt_limit_and_schema(self):
        plan = self.plan(); del plan['execution_policy']
        d = self.create(plan); state = self.run_cli(d)
        self.assertEqual(state['tasks']['work']['status'], 'failed')
        self.assertEqual(state['tasks']['work']['attempts'], 2)
        self.assertNotIn('execution_accounting', state['tasks']['work'])
        self.assertNotIn('yielded', runner.read(d/'result-schema.json')['properties']['status']['enum'])

    def test_policy_is_strict_and_does_not_mutate_existing_plans(self):
        policies = [None, {}, POLICY | dict(version=True), POLICY | dict(version=1), POLICY | dict(version='2'),
                    POLICY | dict(max_waits=0), POLICY | dict(max_yields=True), POLICY | dict(max_no_progress=1.5),
                    POLICY | dict(extra=1)]
        for policy in policies:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                runner.validate(self.plan(execution_policy=policy))

    def contract(self, mode='continue3', manual=False):
        checks = self.task()['checks']
        return dict(objective='Full workflow objective', scope='Temporary local fixture only', authorization='Synthetic offline test',
                    memory=False, execution_policy=copy.deepcopy(POLICY),
                    limits=dict(runtime_seconds=45, attempt_seconds=3, check_seconds=2, max_attempts=2,
                                max_stages=4, max_planning_attempts=5, max_wait_seconds=1),
                    acceptance=[dict(id='full', description='Actual artifact exists', checks=checks)] +
                        ([dict(id='human', description='Actual user acceptance', kind='manual')] if manual else []),
                    stages=[dict(id='initial', covers=['full'], plan=dict(objective='Initial phase', memory=False, tasks=[self.task(mode)]))])

    def create_workflow(self, contract):
        source = self.repo/'contract-input.json'; runner.save(source, contract)
        return workflow.create(self.repo, source)

    def test_workflow_inherits_exact_policy_runs_continuations_and_retains_manual_gate(self):
        contract = self.contract(manual=True); d = self.create_workflow(contract)
        state = self.run_cli(d)
        self.assertEqual(state['status'], 'needs_input', state)
        child = Path(state['stages'][0]['run_dir'])
        self.assertEqual(runner.read(child/'plan.json')['execution_policy'], POLICY)
        self.assertEqual(state['execution_accounting']['invocations'], 4)
        self.assertEqual(state['execution_accounting']['failures'], 0)
        self.assertEqual(state['criteria']['human']['status'], 'needs_input')
        self.assertEqual(state['stages'][0]['status'], 'completed')

    def test_workflow_stage_cannot_override_or_independently_opt_into_policy(self):
        contract = self.contract(); contract['stages'][0]['plan']['execution_policy'] = POLICY | dict(max_waits=100)
        with self.assertRaises(ValueError): workflow.validate(contract)
        contract = self.contract(); del contract['execution_policy']
        contract['stages'][0]['plan']['execution_policy'] = POLICY
        with self.assertRaises(ValueError): workflow.validate(contract)

    def test_workflow_generated_stage_also_inherits_policy(self):
        contract = self.contract(); contract['stages'] = []
        d = self.create_workflow(contract); state = self.run_cli(d)
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(state['execution_accounting']['invocations'], 5)  # planner + four workers
        self.assertEqual(state['planning_attempts'], 1)
        self.assertEqual(runner.read(Path(state['stages'][0]['run_dir'])/'plan.json')['execution_policy'], POLICY)

    def test_workflow_planning_waits_have_separate_bounded_accounting(self):
        contract = self.contract(); contract.update(stages=[], objective='planner-wait full objective')
        d = self.create_workflow(contract); state = self.run_cli(d)
        self.assertEqual(state['status'], 'blocked', state)
        value = state['planning_execution']['execution_accounting']
        self.assertEqual((value['waits'], value['failures']), (3, 0))
        self.assertGreaterEqual(value['wait_seconds'], 1.8)
        self.assertEqual(state['planning_execution']['accounting_stop'], 'max_no_progress')
        again = self.run_cli(d, '--retry-failed')
        self.assertEqual(again['planning_attempts'], 3)

    def test_source_progress_ignores_hidden_private_dependencies_and_symlink_targets(self):
        outside = Path(self.tmp.name)/'private.py'; outside.write_text('outside = 1')
        for folder in ('private', 'node_modules', '.codex'):
            (self.repo/folder).mkdir(exist_ok=True); (self.repo/folder/'ignored.py').write_text('ignore = 1')
        (self.repo/'credential.py').write_text('not read')
        (self.repo/'linked.py').symlink_to(outside)
        (self.repo/'source.py').write_text('source = 1')
        one = accounting.source_snapshot(self.repo)
        self.assertEqual(set(one['files']), {'source.py'})
        outside.write_text('outside = 2')
        self.assertEqual(one['fingerprint'], accounting.source_snapshot(self.repo)['fingerprint'])


if __name__ == '__main__':
    unittest.main()
