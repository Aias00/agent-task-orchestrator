"""End-to-end ACP fixture through the registered runner and host checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import runner
from ato_workflow import task_trace

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT/'tests/fixtures/acp_fixture_cli.py'


class ACPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = dict(os.environ, CODEX_HOME=str(self.root/'config'))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env,
                              capture_output=True, text=True, timeout=30, check=True).stdout.strip()

    def create(self, marker='', timeout=5, attempts=1, kind='offline_acp_fixture'):
        plan = dict(objective='offline fixture', memory=False, agent=dict(kind=kind),
                    tasks=[dict(id='one', prompt='write result '+marker, depends_on=[],
                                checks=[[sys.executable, '-c', "from pathlib import Path; assert Path('result').read_text() == 'fixture output'"]])],
                    limits=dict(runtime_seconds=20, attempt_seconds=timeout, check_seconds=3, max_attempts=attempts))
        source = self.root/'plan.json'
        source.write_text(json.dumps(plan))
        return Path(self.cli('create', '--repo', self.repo, '--plan', source))

    def test_registered_acp_worker_and_checks(self):
        directory = self.create()
        self.cli('run', directory)
        self.assertEqual(runner.read(directory/'state.json')['status'], 'completed')
        data = json.loads(self.cli('trace', directory, '--json'))
        self.assertEqual(data['summary']['tool_call_count'], 1)
        self.assertIn('check_passed', [e['kind'] for e in data['events']])

    def test_unwritable_trace_does_not_abort_worker_or_checks(self):
        directory = self.create()
        (directory/'trace.sqlite3').mkdir()
        self.cli('run', directory)
        self.assertEqual(runner.read(directory/'state.json')['status'], 'completed')
        self.assertEqual((self.repo/'result').read_text(), 'fixture output')
        self.assertFalse(task_trace.query(directory)['summary']['available'])

    def test_production_profile_is_registered_without_plan_commands(self):
        from ato_adapters import agents
        self.assertIn('acp_pi', agents.registry.kinds())
        with self.assertRaises(ValueError):
            agents.validate({'kind': 'acp_pi', 'command': '/tmp/attacker'})
        request = agents.registry.ExecutionRequest({'agent': {'kind': 'acp_pi'}}, self.root,
            'task-attempt-1-abcdef', self.root/'schema', self.root/'output', 'workspace-write', False)
        argv = agents.registry.get('acp_pi', 'worker').command(request)
        self.assertEqual(Path(argv[1]), ROOT/'ato_pi/pi_acp_worker.py')

    def test_live_status_rejects_reused_process_identity(self):
        from ato_workflow import control
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
        self.addCleanup(lambda: (process.terminate(), process.wait()) if process.poll() is None else None)
        state = {'status': 'running', 'owner_pid': os.getpid(),
                 'owner_identity': runner.process_identity(os.getpid()),
                 'active_process': {'pid': process.pid, 'identity': runner.process_identity(process.pid),
                                    'argv': ['fixture'], 'phase': 'agent'}, 'tasks': {}}
        if state['owner_identity'] and state['active_process']['identity']:
            self.assertIsNotNone(control.live_execution(self.root, state))
        else:
            # Some worker sandboxes deny ps; a real but unverifiable PID is not
            # claimed to be live. Host runs also exercise the verified branch.
            self.assertIsNone(control.live_execution(self.root, state))
        state['active_process']['identity'] = 'stale-identity'
        self.assertIsNone(control.live_execution(self.root, state))

    def test_error_cannot_write_success_checkpoint(self):
        directory = self.create('fixture-native-error')
        self.cli('run', directory)
        self.assertNotEqual(runner.read(directory/'state.json')['status'], 'completed')
        self.assertFalse(list((directory/'checkpoints').glob('one-attempt-*.json')))
        self.assertIn('native_error', [e['kind'] for e in task_trace.query(directory)['events']])

    def test_cancel_timeout_no_replay(self):
        # Leave time for the bridge and agent to start. The agent sleeps for
        # 60 seconds after admitting the prompt, beyond this attempt deadline.
        directory = self.create('fixture-timeout', timeout=5, attempts=2)
        self.cli('run', directory)
        self.assertNotEqual(runner.read(directory/'state.json')['status'], 'completed')
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)

    def test_stop_terminates_the_fixture_without_replay(self):
        directory = self.create('fixture-timeout', timeout=10)
        proc = subprocess.Popen([sys.executable, str(CLI), 'run', str(directory)], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.terminate(), proc.wait()) if proc.poll() is None else None)
        deadline = time.monotonic() + 6
        while not (self.repo/'fixture-admissions').exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue((self.repo/'fixture-admissions').exists())
        self.cli('stop', directory)
        proc.wait(timeout=10)
        self.assertNotEqual(runner.read(directory/'state.json')['status'], 'completed')
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)

    def test_resume_keeps_native_identity(self):
        directory = self.create('fixture-native-error', attempts=1)
        self.cli('run', directory)
        first = task_trace.query(directory)['events']
        self.cli('run', directory, '--retry-failed')
        second = task_trace.query(directory)['events']
        errors = [e for e in second if e['kind'] == 'native_error']
        self.assertEqual(len(errors), 2)
        self.assertEqual([e['attempt'] for e in errors], [1, 2])
        self.assertNotEqual(errors[0]['native_session'], errors[1]['native_session'])
        self.assertEqual(first, second[:len(first)])

    def test_missing_native_proof_is_unverified(self):
        directory = self.create('fixture-missing-proof')
        self.cli('run', directory)
        self.assertNotEqual(runner.read(directory/'state.json')['status'], 'completed')
        self.assertIn('native_unverified', [e['kind'] for e in task_trace.query(directory)['events']])

    def test_wrong_turn_and_late_error_cannot_succeed(self):
        for marker in ('fixture-wrong-turn', 'fixture-wrong-session', 'fixture-late-error'):
            with self.subTest(marker=marker):
                directory = self.create(marker)
                self.cli('run', directory)
                self.assertNotEqual(runner.read(directory/'state.json')['status'], 'completed')
                self.assertFalse(list((directory/'checkpoints').glob('one-attempt-*.json')))

    def test_prior_success_cannot_prove_a_new_attempt(self):
        directory = self.create('fixture-success-then-error', attempts=1)
        self.cli('run', directory)
        self.assertEqual(runner.read(directory/'state.json')['status'], 'completed')
        (self.repo/'result').unlink()
        self.cli('run', directory, '--retry-failed')
        state = runner.read(directory/'state.json')
        self.assertNotEqual(state['status'], 'completed')
        self.assertEqual(state['tasks']['one']['attempt_sequence'], 2)
        trace = task_trace.query(directory)['events']
        self.assertIn('native_error', [e['kind'] for e in trace if e.get('attempt') == 2])
        self.assertNotIn('check_passed', [e['kind'] for e in trace if e.get('attempt') == 2])

    def test_derived_pi_rpc_error_overrides_end_turn(self):
        derived = self.root/'derived'
        subprocess.run([sys.executable, str(ROOT/'scripts/derive_pi_acp.py'), str(derived)], check=True,
                       capture_output=True, text=True)
        fixture = ROOT/'tests/fixtures/pi_rpc_fixture.py'
        self.env.update(CTASK_FIXTURE_DERIVED_PI_ACP=str(derived/'pi-acp.mjs'),
                        CTASK_PI_ACP_STATE_DIR=str(self.root/'pi-state'),
                        PI_ACP_PI_COMMAND=str(fixture))
        directory = self.create(kind='offline_pi_acp_fixture', timeout=10)
        self.cli('run', directory)
        self.assertNotEqual(runner.read(directory/'state.json')['status'], 'completed')
        self.assertFalse(list((directory/'checkpoints').glob('one-attempt-*.json')))
        kinds = [e['kind'] for e in task_trace.query(directory)['events']]
        self.assertIn('native_error', kinds)
        self.assertIn('transport_completed', kinds)

    def test_derived_pi_rpc_success_and_independent_checks(self):
        derived = self.root/'derived'
        subprocess.run([sys.executable, str(ROOT/'scripts/derive_pi_acp.py'), str(derived)], check=True,
                       capture_output=True, text=True)
        self.env.update(CTASK_FIXTURE_DERIVED_PI_ACP=str(derived/'pi-acp.mjs'),
                        CTASK_PI_ACP_STATE_DIR=str(self.root/'pi-state'),
                        PI_ACP_PI_COMMAND=str(ROOT/'tests/fixtures/pi_rpc_fixture.py'))
        directory = self.create('fixture-pi-success', kind='offline_pi_acp_fixture', timeout=10)
        self.cli('run', directory)
        self.assertEqual(runner.read(directory/'state.json')['status'], 'completed')
        data = task_trace.query(directory)
        self.assertEqual(data['summary']['tool_call_count'], 1)
        self.assertIn('check_passed', [e['kind'] for e in data['events']])


if __name__ == '__main__':
    unittest.main()
