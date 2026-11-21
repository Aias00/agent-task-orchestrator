"""Directed acpx public-runtime/ATO worker qualification, synthetic peers only."""
import argparse
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
import agent_registry
import acpx_profile
import acpx_worker
import runner
import task_trace

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT/'tests/fixtures/acpx_fixture_cli.py'


class AcpxWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-acpx-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = {k: v for k, v in os.environ.items() if k not in
                    ('CODEX_THREAD_ID', 'ATO_ACTOR_FILE', 'ATO_SESSION_REF', 'NODE_OPTIONS', 'CODEX_CONFIG')}
        self.env.update(ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'native'),
                        CTASK_CODEX_BIN='/nonexistent-forbidden-native-model')

    def cli(self, *args, check=True):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env,
            capture_output=True, text=True, timeout=30, check=check)

    def create(self, scenario='success', *, timeout=8, checks_pass=True, attempts=1, kind='offline_acpx_fixture'):
        self.value = 'value_'+os.urandom(5).hex()
        expected = self.value if checks_pass else 'independent-failure'
        plan = dict(objective='synthetic acpx worker qualification', memory=False,
            agent=dict(kind=kind), sandbox='workspace-write',
            tasks=[dict(id='write', prompt=f'scenario:{scenario} fixture-value:{self.value}', depends_on=[],
                checks=[[sys.executable, '-c', "from pathlib import Path; assert Path('result.txt').read_text() == "+repr(expected)]])],
            limits=dict(runtime_seconds=30, attempt_seconds=timeout, check_seconds=3, max_attempts=attempts))
        source = self.root/'plan.json'
        source.write_text(json.dumps(plan))
        return Path(self.cli('create', '--repo', self.repo, '--plan', source).stdout.strip())

    def run_case(self, scenario='success', **options):
        directory = self.create(scenario, **options)
        self.cli('run', directory)
        return directory, runner.read(directory/'state.json')

    def test_registered_opt_in_worker_has_no_planner_or_native_fallback(self):
        adapter = agent_registry.get('acpx_codex', 'worker')
        self.assertIn('acp_process', adapter.capabilities)
        self.assertNotIn('planner', adapter.capabilities)
        with self.assertRaises(ValueError):
            agent_registry.get('acpx_codex', 'planner')
        for name in agent_registry.SESSION_CAPABILITY_NAMES:
            self.assertEqual(agent_registry.session_capabilities('acpx_codex').state(name).value, 'unsupported')
        request = agent_registry.ExecutionRequest({'agent': {'kind': 'acpx_codex'}}, self.root,
            'write-attempt-1-abcdef', 'schema', 'out', 'workspace-write', False)
        self.assertEqual(Path(adapter.command(request)[1]), ROOT/'acpx_worker.py')
        with self.assertRaises(ValueError):
            agents.validate({'kind': 'acpx_codex', 'command': '/tmp/evil'})

    def test_unsupported_modes_and_grants_fail_before_process(self):
        for mode, planner, grant in [('danger-full-access', False, {}), ('read-only', True, {}),
                                     ('workspace-write', False, {'source': 'model', 'network_access': True})]:
            request = agent_registry.ExecutionRequest({'permissions': grant}, self.root, 'label',
                                                       'schema', 'out', mode, planner)
            with self.assertRaises(ValueError), patch('subprocess.Popen') as launch:
                agent_registry.get('acpx_codex').command(request)
            launch.assert_not_called()

    def test_qualified_package_integrity_and_no_install_on_missing_pin(self):
        pins = acpx_profile.dependencies()
        self.assertTrue(pins['runtime_url'].endswith('/acpx/dist/runtime.js'))
        with patch.object(acpx_profile, 'ARCHIVE', self.root/'missing'), patch('subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                acpx_profile.dependencies()
        launch.assert_not_called()

    def test_clean_home_and_inherited_config_prerequisites(self):
        home = self.root/'clean-native'
        home.mkdir(mode=0o700)
        acpx_profile.check_clean_home(home)
        (home/'config.toml').write_text('[mcp_servers.bad]')
        with self.assertRaises(ValueError):
            acpx_profile.check_clean_home(home)
        (self.repo/'.codex').mkdir()
        (self.repo/'.codex/config.toml').write_text('native config never opened')
        with self.assertRaises(ValueError):
            acpx_profile.reject_project_config(self.repo)

    def test_outer_sandbox_protects_authority_and_git(self):
        # Actual host Seatbelt qualification; the worker's known nested-sandbox
        # restriction is a host check, never a skip or an allow-all fallback.
        scratch, native, authority = [self.root/p for p in ('scratch', 'native', 'ato')]
        for folder in (scratch, native, authority, self.repo/'.codex'):
            folder.mkdir(mode=0o700)
        (self.repo/'alias').symlink_to(authority, target_is_directory=True)
        for mode in ('workspace-write', 'read-only'):
            policy = self.root/(mode+'.sb')
            policy.write_text(acpx_profile.sandbox_profile(self.repo, scratch, native,
                [self.repo/'.git', self.repo/'.codex', authority], mode))
            program = '''import pathlib,sys
repo,scratch,native,authority=map(pathlib.Path,sys.argv[1:5])
mode=sys.argv[5]
(scratch/'allowed').write_text('scratch')
def denied(path):
    try:path.write_text('must fail')
    except PermissionError:return
    raise AssertionError('unprotected write: '+str(path))
for path in [repo/'.git/forbidden',repo/'.codex/forbidden',authority/'forbidden',repo/'alias/forbidden',native/'auth.json',native/'config.toml']:
    denied(path)
if mode=='workspace-write':(repo/'allowed').write_text('repo')
else:denied(repo/'forbidden')
'''
            result = subprocess.run([str(acpx_profile.SANDBOX), '-f', str(policy), sys.executable,
                '-c', program, str(self.repo), str(scratch), str(native), str(authority), mode],
                env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_runtime_worker_writes_dynamic_file_and_independent_checks(self):
        directory, state = self.run_case()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual((self.repo/'result.txt').read_text(), self.value)
        receipts = list((directory/'checkpoints').glob('*.acpx-runtime/receipt.json'))
        self.assertEqual(len(receipts), 1)
        receipt = runner.read(receipts[0])
        self.assertEqual(receipt['delivery'], 'finished')
        self.assertEqual(receipt['business_validation'], 'unknown')
        self.assertEqual(receipt['backend_outcome'], 'unknown')
        self.assertEqual(receipt['capabilities']['load_session'], 'unsupported')
        self.assertEqual(receipt['capabilities']['qualification'], 'unknown')
        self.assertEqual(receipt['correlation']['attempt'], 1)
        events = task_trace.query(directory)['events']
        self.assertIn('check_passed', [e['kind'] for e in events])
        self.assertIn('native_unverified', [e['kind'] for e in events])

    def test_end_turn_cannot_replace_failed_independent_check(self):
        directory, state = self.run_case(checks_pass=False)
        self.assertNotEqual(state['status'], 'completed')
        self.assertIn('check_failed', [e['kind'] for e in task_trace.query(directory)['events']])

    def test_permission_requests_are_denied_and_cannot_publish_success(self):
        directory, state = self.run_case('permission')
        self.assertEqual(state['tasks']['write']['status'], 'blocked')
        decision = runner.read(self.repo/'fixture-permission.json')
        self.assertEqual(decision['outcome']['outcome'], 'cancelled')
        self.assertFalse(list((directory/'checkpoints').glob('write-attempt-*.json')))

    def test_disabled_client_filesystem_and_terminal_callbacks(self):
        self.run_case()
        capabilities = runner.read(self.repo/'fixture-client-capabilities.json')
        self.assertFalse(capabilities.get('terminal', False))
        self.assertFalse(capabilities.get('fs', {}).get('writeTextFile', False))
        self.assertFalse(capabilities.get('fs', {}).get('readTextFile', False))

    def test_missing_mode_fails_before_prompt_dispatch(self):
        (self.repo/'fixture-config.json').write_text(json.dumps({'no_mode': True}))
        _, state = self.run_case()
        self.assertNotEqual(state['status'], 'completed')
        self.assertFalse((self.repo/'fixture-admissions').exists())

    def test_unacknowledged_mode_fails_before_prompt_dispatch(self):
        (self.repo/'fixture-config.json').write_text(json.dumps({'wrong_mode': True}))
        _, state = self.run_case()
        self.assertNotEqual(state['status'], 'completed')
        self.assertFalse((self.repo/'fixture-admissions').exists())

    def test_eof_is_unknown_and_does_not_automatically_replay(self):
        directory, state = self.run_case('eof', attempts=3)
        self.assertEqual(state['tasks']['write']['status'], 'blocked')
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)
        self.assertFalse(list((directory/'checkpoints').glob('write-attempt-*.json')))

    def test_timeout_blocks_without_replay_or_success(self):
        directory, state = self.run_case('hang', timeout=2, attempts=3)
        self.assertNotEqual(state['status'], 'completed')
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)
        self.assertFalse(list((directory/'checkpoints').glob('write-attempt-*.json')))

    def test_production_policy_retry_cannot_replay_unknown_delivery_or_reset_attempts(self):
        directory, state = self.run_case('eof', attempts=3, kind='acpx_codex')
        self.assertTrue(state['tasks']['write']['acpx_delivery_unknown'])
        self.cli('run', directory, '--retry-failed')
        after = runner.read(directory/'state.json')['tasks']['write']
        self.assertEqual(after['status'], 'blocked')
        self.assertEqual(after['attempts'], 1)
        self.assertEqual(after['attempt_sequence'], 1)
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)

    def test_completed_transport_with_invalid_result_remains_a_distinct_failure(self):
        directory, state = self.run_case('malformed', kind='acpx_codex', attempts=2)
        row = state['tasks']['write']
        self.assertEqual(row['status'], 'blocked')
        self.assertFalse(row['acpx_delivery_unknown'])
        self.assertTrue(row['acpx_requires_reconciliation'])
        receipt = runner.read(next((directory/'checkpoints').glob('*.acpx-runtime/receipt.json')))
        self.assertEqual(receipt['delivery'], 'finished')
        self.assertFalse(receipt['result_schema_valid'])
        self.assertFalse(list((directory/'checkpoints').glob('write-attempt-*.json')))
        self.cli('run', directory, '--retry-failed')
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)

    def test_production_environment_is_clean_without_reading_fixture_login(self):
        home, scratch = self.root/'clean-native', self.root/'scratch'
        home.mkdir(mode=0o700); scratch.mkdir(mode=0o700)
        auth = home/'auth.json'
        auth.write_text('synthetic login sentinel; never opened by implementation')
        original = Path.read_text
        def no_credentials(path, *args, **kwargs):
            self.assertNotEqual(path, auth)
            return original(path, *args, **kwargs)
        config = self.root/'host-paths.json'
        config.write_text(json.dumps(dict(version=1, node=str(acpx_profile.NODE.resolve()),
            archive=str(acpx_profile.ARCHIVE.resolve()), deps=str(acpx_profile.DEPS.resolve()),
            native_home=str(home.resolve()))))
        config.chmod(0o600)
        with patch.object(acpx_profile, 'NATIVE_HOME', home), patch.object(Path, 'read_text', no_credentials), \
                patch.dict(os.environ, {'OPENAI_API_KEY': 'PRIVATE_SECRET', 'CODEX_CONFIG': 'untrusted',
                                         'ATO_ACPX_CONFIG': str(config.resolve()),
                                         'PATH': '/untrusted-toolchain',
                                         'NODE_OPTIONS': '--require=/untrusted'}):
            profile = acpx_profile.codex_profile(self.repo, scratch, [self.root/'ato'], 'workspace-write')
        env = profile['agent_env']
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('NODE_OPTIONS', env)
        self.assertNotIn('PRIVATE_SECRET', json.dumps(profile))
        self.assertEqual(env['CODEX_HOME'], str(scratch/'native-home'))
        isolated = Path(env['CODEX_HOME'])
        self.assertTrue((isolated/'auth.json').is_symlink())
        self.assertEqual((isolated/'auth.json').readlink(), home.resolve()/'auth.json')
        self.assertEqual(isolated.stat().st_mode & 0o777, 0o700)
        # Real native startup self-provisions these; it must not poison the seed
        # or cause a second distinct attempt to inherit config/skills.
        (isolated/'config.toml').write_text('synthetic auto-generated config')
        (isolated/'skills').mkdir()
        acpx_profile.check_clean_home(home)
        second = self.root/'scratch-second'; second.mkdir(mode=0o700)
        with patch.object(Path, 'read_text', no_credentials), patch.dict(os.environ, {'ATO_ACPX_CONFIG': str(config.resolve())}):
            again = acpx_profile.codex_profile(self.repo, second, [self.root/'ato'], 'workspace-write')
        self.assertNotEqual(again['agent_env']['CODEX_HOME'], env['CODEX_HOME'])
        self.assertFalse((Path(again['agent_env']['CODEX_HOME'])/'config.toml').exists())
        self.assertFalse((Path(again['agent_env']['CODEX_HOME'])/'skills').exists())
        self.assertEqual(json.loads(env['CODEX_CONFIG'])['mcp_servers'], {})
        tool_path = acpx_profile.trusted_tool_path(acpx_profile.NODE)
        self.assertEqual(env['PATH'], tool_path)
        self.assertEqual(json.loads(env['CODEX_CONFIG'])['shell_environment_policy'],
                         {'inherit': 'none', 'set': {'PATH': tool_path}})
        self.assertNotIn('/untrusted-toolchain', tool_path)
        result = subprocess.run(['node', '-e', 'process.stdout.write(String(1+1))'],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '2')
        self.assertEqual(profile['required_mode'], 'agent')

    def test_reviewed_loopback_proxy_is_model_only_and_rejects_remote_or_credentials(self):
        home = self.root/'proxy-seed'; home.mkdir(mode=0o700)
        (home/'auth.json').write_text('synthetic, implementation must not read')
        config = self.root/'proxy-paths.json'
        value = dict(version=1,node=str(acpx_profile.NODE.resolve()),
            archive=str(acpx_profile.ARCHIVE.resolve()),deps=str(acpx_profile.DEPS.resolve()),
            native_home=str(home.resolve()),model_proxy='http://127.0.0.1:45678')
        config.write_text(json.dumps(value));config.chmod(0o600)
        scratch=self.root/'proxy-scratch';scratch.mkdir()
        with patch.dict(os.environ, {'ATO_ACPX_CONFIG':str(config.resolve()),'HTTPS_PROXY':'http://evil.invalid:8080'}):
            profile=acpx_profile.codex_profile(self.repo,scratch,[self.root/'ato'],'workspace-write')
            self.assertEqual(profile['agent_env']['HTTPS_PROXY'],'http://127.0.0.1:45678')
            shell=json.loads(profile['agent_env']['CODEX_CONFIG'])['shell_environment_policy']
            self.assertEqual(shell['inherit'],'none');self.assertNotIn('HTTPS_PROXY',shell['set'])
            self.assertFalse(json.loads(profile['agent_env']['CODEX_CONFIG'])['permissions']['ato-worker']['network']['enabled'])
            for bad in ['http://external.invalid:8080','http://user:secret@127.0.0.1:8080','file:///tmp/x','http://127.0.0.1:8080/?token=secret']:
                value['model_proxy']=bad;config.write_text(json.dumps(value))
                with self.assertRaises(acpx_profile.ProfileUnavailable):acpx_profile.operator_paths()

    def test_failed_transport_has_body_free_diagnostics_without_acceptance_or_replay(self):
        directory, _ = self.run_case('eof', kind='acpx_codex')
        row=runner.read(directory/'state.json')['tasks']['write']
        self.assertTrue(row['acpx_delivery_unknown'])
        paths=list((directory/'checkpoints').glob('*.acpx-runtime/failure.json'))
        self.assertEqual(len(paths),1)
        value=runner.read(paths[0]);self.assertEqual(value['delivery'],'unknown')
        self.assertIn(value['diagnostic']['phase'],{'turn','final'})
        self.assertNotIn('prompt',json.dumps(value));self.assertNotIn('stderr',json.dumps(value))
        self.assertFalse(list((directory/'checkpoints').glob('*.acpx-runtime/receipt.json')))

    def test_production_policy_recheck_failure_keeps_consumed_attempt_ceiling(self):
        directory, state = self.run_case(kind='acpx_codex')
        self.assertEqual(state['status'], 'completed')
        (self.repo/'result.txt').write_text('changed after acceptance')
        self.cli('run', directory, '--retry-failed')
        after = runner.read(directory/'state.json')['tasks']['write']
        self.assertEqual(after['attempts'], 1)
        self.assertEqual(after['attempt_sequence'], 1)
        self.assertNotEqual(after['status'], 'completed')
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)

    def test_host_checks_cannot_supply_missing_transport_proof(self):
        directory = self.create(kind='acpx_codex')
        state = runner.read(directory/'state.json')
        state['tasks']['write'].update(status='blocked', attempt_sequence=1, attempts=1, acpx_delivery_unknown=True)
        runner.save(directory/'state.json', state)
        (self.repo/'result.txt').write_text(self.value)
        with patch.dict(os.environ, self.env, clear=True):
            runner.Runner(directory).execute(host_checks=True)
        after = runner.read(directory/'state.json')['tasks']['write']
        self.assertEqual(after['status'], 'needs_host')
        self.assertIn('lacks validated acpx', after['feedback'])
        self.assertFalse((self.repo/'fixture-admissions').exists())

    def test_stop_keeps_single_admission_and_cleans_process_group(self):
        directory = self.create('hang', timeout=15)
        process = subprocess.Popen([sys.executable, str(CLI), 'run', str(directory)], env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.terminate(), process.wait()) if process.poll() is None else None)
        deadline = time.monotonic()+8
        while not (self.repo/'fixture-process.json').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue((self.repo/'fixture-process.json').is_file())
        child = runner.read(self.repo/'fixture-process.json')['pid']
        self.cli('stop', directory)
        process.wait(timeout=10)
        self.assertTrue((directory/'STOP').exists())
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)
        self.assertIsNone(runner.read(directory/'state.json')['active_process'])
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_malformed_schema_prose_and_ambiguous_output_fail_closed(self):
        for scenario in ('malformed', 'duplicate', 'prose', 'schema', 'ambiguous'):
            with self.subTest(scenario=scenario):
                directory, state = self.run_case(scenario)
                self.assertEqual(state['tasks']['write']['status'], 'blocked')
                self.assertFalse(list((directory/'checkpoints').glob('write-attempt-*.json')))

    def test_truncated_cancelled_rpc_error_and_wrong_session_cannot_complete(self):
        for scenario in ('truncated', 'cancelled', 'rpc_error', 'wrong_session', 'thought_only', 'empty', 'oversize'):
            with self.subTest(scenario=scenario):
                directory, state = self.run_case(scenario)
                self.assertEqual(state['tasks']['write']['status'], 'blocked')
                self.assertFalse(list((directory/'checkpoints').glob('write-attempt-*.json')))

    def test_safe_observations_are_bounded_and_do_not_log_peer_secrets(self):
        directory, state = self.run_case('flood')
        self.assertEqual(state['status'], 'completed')
        receipt = runner.read(next((directory/'checkpoints').glob('*.acpx-runtime/receipt.json')))
        self.assertLessEqual(receipt['observations']['events'], 64)
        for path in [*list((directory/'evidence').glob('*')), *list((directory/'checkpoints').glob('*.acpx-runtime/*'))]:
            if path.is_file():
                self.assertNotIn('PRIVATE_SECRET', path.read_text(errors='replace'))

    def test_different_synthetic_capability_profile_uses_same_lifecycle(self):
        (self.repo/'fixture-config.json').write_text(json.dumps({'resumable': True}))
        directory, state = self.run_case()
        self.assertEqual(state['status'], 'completed')
        receipt = runner.read(next((directory/'checkpoints').glob('*.acpx-runtime/receipt.json')))
        self.assertEqual(receipt['capabilities']['load_session'], 'advertised')
        self.assertEqual(receipt['capabilities']['qualification'], 'unknown')

    def test_decode_rejects_stale_correlation_and_nonresponse(self):
        correlation = {'turn': 'current'}
        payload = dict(version=1, correlation=correlation, protocol='completed', stop_reason='end_turn',
            delivery='finished', transport='response', backend_outcome='unknown', business_validation='unknown',
            text=json.dumps(dict(status='ready_for_checks', summary='done', blocker='', next_step='', retry_after_seconds=1)))
        self.assertEqual(acpx_worker.decode(payload, correlation, runner.SCHEMA)['status'], 'ready_for_checks')
        for key, value in [('correlation', {'turn': 'stale'}), ('delivery', 'unknown'), ('transport', 'no_response')]:
            with self.assertRaises(ValueError):
                acpx_worker.decode({**payload, key: value}, correlation, runner.SCHEMA)


if __name__ == '__main__':
    unittest.main()
