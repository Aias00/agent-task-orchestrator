"""Actual bounded Runner/Workflow checks in temporary Git repositories only.

No model, native CLI, live task, install, detached process or external mutation.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runner
import verification_cache as cache
import workflow

SCRIPT = """from pathlib import Path
p=Path('count')
p.write_text(str(int(p.read_text())+1 if p.exists() else 1))
assert Path('input.txt').read_text() in ('one','two')
print('original independent evidence')
"""


class IncrementalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-verification-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base/'repo'; self.repo.mkdir()
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.repo)], check=True)
        self.env = dict(os.environ, ATO_HOME=str(self.base/'private'), CODEX_HOME=str(self.base/'native'),
                        PYTHONDONTWRITEBYTECODE='1')
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            self.env.pop(key, None)
        env = patch.dict(os.environ, self.env, clear=True); env.start(); self.addCleanup(env.stop)
        (self.repo/'input.txt').write_text('one')
        (self.repo/'check.py').write_text(SCRIPT)
        self.task = dict(id='artifact', prompt='Fixture only', depends_on=[], checks=[[sys.executable, 'check.py']])
        self.policy = dict(version=1, checks=dict(artifact=dict(inputs=['input.txt'], env=['FIXTURE_MODE'], cacheable=True)))
        self.plan = dict(objective='Complete temporary artifact', memory=False, tasks=[self.task],
                         verification_policy=self.policy, limits=dict(check_seconds=3, attempt_seconds=5))

    def create(self):
        runner.save(self.repo/'plan-input.json', self.plan)
        return runner.Runner(runner.create(self.repo, self.repo/'plan-input.json'))

    def total(self, manual=False, stage=False):
        contract = dict(objective=self.plan['objective'], scope='temporary fixture files only',
            authorization='bounded synthetic checks only', memory=False, verification_policy=copy.deepcopy(self.policy),
            acceptance=[dict(id='artifact', description='required output', checks=self.task['checks'])])
        if manual:
            contract['acceptance'].append(dict(id='human', description='actual user decision', kind='manual'))
            contract['verification_policy']['checks']['human'] = dict(inputs=['input.txt'], env=[], cacheable=True)
        if stage:
            contract['stages'] = [dict(id='s', covers=['artifact'], plan=dict(objective='stage', tasks=[copy.deepcopy(self.task)]))]
        runner.save(self.repo/'contract-input.json', contract)
        return workflow.Workflow(workflow.create(self.repo, self.repo/'contract-input.json'))

    def check(self, r, label='probe', ok=True, **kwargs):
        result, logs = r.checks(self.task, label, **kwargs)
        self.assertEqual(result, ok); self.assertTrue(logs)
        return logs

    def count(self):
        return int((self.repo/'count').read_text())

    def ledger(self, r):
        return runner.read(cache._root(r)/'receipts.json')

    def events(self, r):
        return [json.loads(x) for x in (r.dir/'events.jsonl').read_text().splitlines()]

    def cli(self, directory):
        # This sandbox denies ps. Supply an explicit synthetic kernel identity
        # only in the fixture harness, as earlier worker-session stage tests do.
        # There is no production fallback for an unknown execution owner.
        bootstrap = "import sys;sys.path.insert(0,sys.argv[1]);import runner;runner.process_identity=lambda pid:'fixture-kernel-'+str(pid);sys.argv=[sys.argv[1]+'/runner.py','run',sys.argv[2]];raise SystemExit(runner.main())"
        p = subprocess.run([sys.executable, '-c', bootstrap, str(ROOT), str(directory)],
                           capture_output=True, text=True, env=dict(os.environ), timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr+'\n'+p.stdout)
        return runner.read(directory/'state.json')

    def test_runner_same_input_reuses_exact_original_log_and_provenance(self):
        r = self.create(); logs = self.check(r, 'first'); first = self.ledger(r)
        self.assertEqual(self.check(r, 'second'), logs); self.assertEqual(self.count(), 1)
        self.assertEqual(first, self.ledger(r))
        reused = next(e['receipt'] for e in self.events(r) if e['event'] == 'verification_reused')
        self.assertEqual(reused['returncode'], 0); self.assertEqual(reused['run'], str(r.dir))
        self.assertEqual(reused['evidence'], logs[0]); self.assertIn('source_sha256', reused['inputs'])
        self.assertLessEqual(reused['started_at'], reused['finished_at'])

    def test_input_bytes_mode_deletion_and_recreation_invalidate(self):
        r = self.create(); self.check(r)
        p = self.repo/'input.txt'; p.write_text('two'); self.check(r, 'bytes'); self.assertEqual(self.count(), 2)
        p.chmod(0o700); self.check(r, 'mode'); self.assertEqual(self.count(), 3)
        p.unlink(); self.check(r, 'deleted', ok=False); self.assertEqual(self.count(), 4)
        p.write_text('one'); self.check(r, 'recreated'); self.assertEqual(self.count(), 5)

    def test_environment_values_and_presence_invalidate_without_plaintext(self):
        r = self.create()
        with patch.dict(os.environ, FIXTURE_MODE='sensitive-fixture-value'):
            self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), 1)
            self.assertNotIn('sensitive-fixture-value', (cache._root(r)/'receipts.json').read_text())
        self.check(r, 'c'); self.assertEqual(self.count(), 2)
        with patch.dict(os.environ, UNDECLARED_FIXTURE_DEPENDENCY='changed'):
            self.check(r, 'd'); self.assertEqual(self.count(), 3)

    def test_script_is_automatic_dependency_even_when_not_declared(self):
        r = self.create(); self.check(r)
        p = self.repo/'check.py'; p.write_text(p.read_text()+'\n# changed verifier\n')
        self.check(r, 'changed'); self.assertEqual(self.count(), 2)
        p.chmod(0o700); self.check(r, 'changed-mode'); self.assertEqual(self.count(), 3)

    def test_declared_directory_membership_and_nested_modes_invalidate(self):
        folder = self.repo/'inputs'; folder.mkdir(); (folder/'a').write_text('a')
        self.policy['checks']['artifact']['inputs'].append('inputs')
        r = self.create(); self.check(r); self.check(r, 'same'); self.assertEqual(self.count(), 1)
        (folder/'b').mkdir(); self.check(r, 'added'); self.assertEqual(self.count(), 2)
        (folder/'b').chmod(0o700); self.check(r, 'mode'); self.assertEqual(self.count(), 3)
        (folder/'a').unlink(); self.check(r, 'removed'); self.assertEqual(self.count(), 4)

    def test_missing_tampered_or_linked_log_never_supplies_pass(self):
        r = self.create(); log = Path(self.check(r)[0]); log.unlink()
        log = Path(self.check(r, 'missing')[0]); self.assertEqual(self.count(), 2)
        log.write_text('forged pass'); log = Path(self.check(r, 'forged')[0]); self.assertEqual(self.count(), 3)
        log.unlink(); log.symlink_to(self.repo/'input.txt')
        self.check(r, 'linked'); self.assertEqual(self.count(), 4)

    def test_final_full_integration_and_explicit_fresh_always_execute(self):
        r = self.create(); self.check(r)
        for i, label in enumerate(('final-independent', 'full-regression', 'integration-probe', 'host-recheck'), 2):
            self.check(r, label); self.assertEqual(self.count(), i)
        self.check(r, 'ordinary', fresh=True); self.assertEqual(self.count(), 6)

    def test_final_failure_retires_earlier_success_before_execution(self):
        r = self.create(); self.check(r)
        original = r.command
        def failure(*args, **kwargs):
            _, log = original(*args, **kwargs)
            return 1, log  # Inject a host execution fault after actual execution.
        with patch.object(r, 'command', side_effect=failure):
            self.check(r, 'final-independent', ok=False)
        self.assertEqual(self.ledger(r)['entries'], {})
        self.check(r, 'after-fault'); self.assertEqual(self.count(), 3)

    def test_expired_future_or_incomplete_receipt_is_not_a_hit(self):
        r = self.create(); self.check(r)
        for label, update in (('expired', {'finished_at': time.time()-cache.MAX_AGE_SECONDS-1}),
                              ('future', {'finished_at': time.time()+100}), ('pending', {'returncode': None})):
            data = self.ledger(r); next(iter(data['entries'].values())).update(update)
            cache._private_save(cache._root(r)/'receipts.json', data)
            before = self.count(); self.check(r, label); self.assertEqual(self.count(), before+1)

    def test_failed_checks_are_never_cached(self):
        (self.repo/'input.txt').write_text('bad')
        r = self.create(); self.check(r, 'one', ok=False); self.check(r, 'two', ok=False)
        self.assertEqual(self.count(), 2); self.assertEqual(self.ledger(r)['entries'], {})

    def test_input_changes_during_command_prevent_recording(self):
        (self.repo/'check.py').write_text(SCRIPT+"\nPath('input.txt').write_text('two' if Path('input.txt').read_text()=='one' else 'one')\n")
        r = self.create(); self.check(r); self.check(r, 'again')
        self.assertEqual(self.count(), 2); self.assertEqual(self.ledger(r)['entries'], {})

    def test_unknown_external_and_dynamic_dependencies_run_original_argv(self):
        for extra in ('import time\n', 'import socket\n', 'import subprocess\n', "__import__('json')\n"):
            with self.subTest(extra=extra):
                (self.repo/'check.py').write_text(extra+SCRIPT)
                r = self.create(); before = self.count() if (self.repo/'count').exists() else 0
                self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), before+2)
                commands = [e['argv'] for e in self.events(r) if e['event'] == 'command_started']
                self.assertEqual(commands, self.task['checks']*2)

    def test_undeclared_read_and_dynamic_path_fail_closed(self):
        (self.repo/'hidden.txt').write_text('hidden')
        for code in ("assert Path('hidden.txt').read_text()=='hidden'\n",
                     "name='input.txt'\nassert Path(name).read_text()=='one'\n"):
            (self.repo/'check.py').write_text(SCRIPT+code)
            r = self.create(); before = self.count() if (self.repo/'count').exists() else 0
            self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), before+2)

    def test_local_import_content_and_lockfile_drift_invalidate(self):
        (self.repo/'helper.py').write_text("VALUE='one'\n")
        (self.repo/'check.py').write_text('import helper\n'+SCRIPT+'\nassert helper.VALUE\n')
        r = self.create(); self.check(r); self.check(r, 'same'); self.assertEqual(self.count(), 1)
        (self.repo/'helper.py').write_text("VALUE='two'\n"); self.check(r, 'import'); self.assertEqual(self.count(), 2)
        (self.repo/'uv.lock').write_text('fixture-lock-one'); self.check(r, 'lock'); self.assertEqual(self.count(), 3)

    def test_input_directory_and_script_symlinks_are_uncached(self):
        real = self.repo/'real.txt'; real.write_text('one')
        p = self.repo/'input.txt'; p.unlink(); p.symlink_to(real)
        r = self.create(); self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), 2)
        p.unlink(); p.write_text('one')
        (self.repo/'real.py').write_text(SCRIPT)
        p = self.repo/'check.py'; p.unlink(); p.symlink_to('real.py')
        r = self.create(); self.check(r, 'c'); self.check(r, 'd'); self.assertEqual(self.count(), 4)

    def test_noncacheable_and_legacy_absent_policy_retain_fresh_behavior(self):
        self.policy['checks']['artifact']['cacheable'] = False
        r = self.create(); self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), 2)
        self.plan.pop('verification_policy'); r = self.create()
        self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), 4)
        self.assertFalse(any(e['event'] == 'verification_reused' for e in self.events(r)))

    def test_missing_or_tampered_private_witness_cannot_be_recreated(self):
        r = self.create(); self.check(r); p = cache._root(r)/'receipts.json'; p.write_text('{')
        self.check(r, 'partial'); self.check(r, 'again'); self.assertEqual(self.count(), 3)
        self.assertEqual(p.read_text(), '{')
        p.unlink(); self.check(r, 'missing'); self.assertEqual(self.count(), 4); self.assertFalse(p.exists())

    def test_worker_writable_or_untrusted_private_location_disables_cache(self):
        os.environ['ATO_HOME'] = str(self.repo/'unsafe-private')
        r = self.create(); self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), 2)
        self.assertFalse((self.repo/'unsafe-private'/'verification-cache-v1').exists())

    def test_contract_authority_and_generation_drift_never_reuse(self):
        r = self.create(); self.check(r)
        r.state['execution_revision'] = 1; r.persist(); self.check(r, 'generation'); self.assertEqual(self.count(), 2)
        r.plan['sandbox'] = 'read-only'
        self.check(r, 'authority'); self.assertEqual(self.count(), 3)
        r = runner.Runner(r.dir); r.plan['objective'] = 'changed contract'
        runner.save(r.dir/'plan.json', r.plan)
        with self.assertRaises(runner.Halt): self.check(r, 'contract')
        self.assertEqual(self.count(), 3)

    def test_stop_guard_is_checked_before_a_cache_hit(self):
        r = self.create(); self.check(r); (r.dir/'STOP').touch()
        with self.assertRaisesRegex(runner.Halt, 'stopped'): self.check(r, 'after-stop')
        self.assertEqual(self.count(), 1)

    def test_runner_foreground_cli_completion_forces_final_execution(self):
        r = self.create(); self.check(r); r.state['tasks']['artifact']['status'] = 'completed'; r.persist()
        state = self.cli(r.dir)
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(self.count(), 2); self.assertEqual(state['tasks']['artifact']['attempts'], 0)
        self.assertTrue(any(e['event'] == 'verification_reused' for e in self.events(r)))

    def test_workflow_check_total_reuses_but_foreground_completion_is_fresh(self):
        w = self.total(); w.check_total(); original = w.state['criteria']['artifact']['evidence']
        w.check_total(); self.assertEqual(w.state['criteria']['artifact']['evidence'], original)
        self.assertEqual(self.count(), 1)
        state = self.cli(w.dir); self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(self.count(), 2); self.assertEqual(state['stages'], [])

    def test_workflow_manual_ack_is_rechecked_and_never_cached(self):
        w = self.total(manual=True); w.check_total()
        self.assertEqual(w.state['criteria']['human']['status'], 'needs_input')
        evidence = self.repo/'user-decision.txt'; evidence.write_text('fixture actual decision')
        workflow.acknowledge(w.dir, 'human', evidence, 'fixture explicit user answer')
        w.check_total(); self.assertEqual(w.state['criteria']['human']['status'], 'passed')
        evidence.write_text('changed decision evidence'); w.check_total()
        self.assertEqual(w.state['criteria']['human']['status'], 'needs_input'); self.assertEqual(self.count(), 1)
        self.assertFalse(any(e.get('task') == 'human' and e['event'] == 'verification_reused' for e in self.events(w)))

    def test_workflow_stage_inherits_only_exact_host_selected_policy(self):
        w = self.total(stage=True); preset = w.contract['stages'][0]
        w.stage(preset['plan'], preset['covers'], preset['id'])
        child = runner.Runner(Path(w.state['active_run'])); child.parent_guard = w.guard
        self.assertEqual(child.plan['verification_policy'], self.policy)
        self.check(child, 'a'); self.check(child, 'b'); self.assertEqual(self.count(), 1)
        w.state['execution_revision'] = 2; w.persist()
        self.check(child, 'generation'); self.assertEqual(self.count(), 2)
        forged = copy.deepcopy(preset['plan']); forged['verification_policy'] = copy.deepcopy(self.policy)
        forged['verification_policy']['checks']['artifact']['inputs'] = ['check.py']
        with self.assertRaisesRegex(ValueError, 'override'): w.stage(forged, preset['covers'])

    def test_policy_schema_rejects_unknown_ids_paths_fields_and_versions(self):
        for change in (lambda p: p.update(version=2), lambda p: p.update(version=True),
                       lambda p: p['checks'].update(unknown=p['checks']['artifact']),
                       lambda p: p['checks']['artifact'].update(inputs=['../secret']),
                       lambda p: p['checks']['artifact'].update(inputs=['/absolute']),
                       lambda p: p['checks']['artifact'].update(env=['NAME=value']),
                       lambda p: p['checks']['artifact'].update(cacheable=1),
                       lambda p: p['checks']['artifact'].update(ttl=999)):
            plan = copy.deepcopy(self.plan); change(plan['verification_policy'])
            with self.subTest(policy=plan['verification_policy']), self.assertRaises(ValueError): runner.validate(plan)

    def test_argv_and_toolchain_unknown_keep_exact_original_execution(self):
        # A shell/executable wrapper is not silently substituted by the cache.
        wrapper = self.repo/'fixture-python'; wrapper.write_text('#!/bin/sh\nexec "'+sys.executable+'" "$@"\n'); wrapper.chmod(0o700)
        self.task['checks'] = [[str(wrapper), 'check.py']]
        r = self.create(); self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), 2)
        self.assertEqual([e['argv'] for e in self.events(r) if e['event'] == 'command_started'], self.task['checks']*2)

    def test_hidden_output_acceptance_dependency_is_not_a_trusted_complete_input(self):
        (self.repo/'check.py').write_text(SCRIPT+"\nassert int(p.read_text()) <= 1\n")
        r = self.create(); self.check(r, 'first'); self.check(r, 'second', ok=False)
        self.assertEqual(self.count(), 2); self.assertEqual(self.ledger(r)['entries'], {})

    def test_aliased_builtin_stream_and_escaping_glob_are_unknown_dependencies(self):
        (self.repo/'hidden.txt').write_text('hidden')
        folder = self.repo/'inputs'; folder.mkdir()
        self.policy['checks']['artifact']['inputs'].append('inputs')
        for extra in ("f=open\nwith f('hidden.txt') as stream: assert stream.read()=='hidden'\n",
                      "assert list(Path('inputs').glob('../hidden.txt'))\n",
                      "from pathlib import os\nassert os.path.exists('hidden.txt')\n"):
            with self.subTest(extra=extra):
                (self.repo/'check.py').write_text(SCRIPT+extra)
                r = self.create(); before = self.count() if (self.repo/'count').exists() else 0
                self.check(r, 'a'); self.check(r, 'b'); self.assertEqual(self.count(), before+2)

    def test_nested_input_directory_symlink_invalidates_reuse(self):
        folder = self.repo/'inputs'; folder.mkdir()
        self.policy['checks']['artifact']['inputs'].append('inputs')
        r = self.create(); self.check(r); (folder/'linked').symlink_to(self.repo)
        self.check(r, 'link'); self.check(r, 'still-linked'); self.assertEqual(self.count(), 3)

    def test_multiple_original_commands_reuse_independently(self):
        (self.repo/'other.py').write_text(SCRIPT.replace("'count'", "'count-two'"))
        self.task['checks'].append([sys.executable, 'other.py'])
        r = self.create(); first = self.check(r, 'a'); self.assertEqual(self.check(r, 'b'), first)
        self.assertEqual((self.count(), int((self.repo/'count-two').read_text())), (1, 1))
        p = self.repo/'other.py'; p.write_text(p.read_text()+'\n# changed second verifier\n')
        logs = self.check(r, 'c'); self.assertEqual(logs[0], first[0]); self.assertNotEqual(logs[1], first[1])
        self.assertEqual((self.count(), int((self.repo/'count-two').read_text())), (1, 2))

    def test_cannot_spawn_fresh_run_until_prior_success_durably_retires(self):
        r = self.create(); self.check(r)
        with patch.object(cache, '_private_save', side_effect=OSError('fixture disk unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'durably retire'):
                self.check(r, 'final-independent')
        self.assertEqual(self.count(), 1)
        self.check(r, 'final-independent'); self.assertEqual(self.count(), 2)

    def test_unknown_owner_and_missing_toolchain_evidence_are_uncached(self):
        r = self.create(); self.check(r)
        r.state.update(owner_pid=os.getpid(), owner_identity=None)
        self.check(r, 'unknown-owner'); self.check(r, 'still-unknown'); self.assertEqual(self.count(), 3)
        r.state.update(owner_pid=None)
        original = cache.sysconfig.get_config_var
        with patch.object(cache.sysconfig, 'get_config_var', side_effect=lambda n: None if n == 'LDLIBRARY' else original(n)):
            self.check(r, 'missing-toolchain'); self.assertEqual(self.count(), 4)

    def test_invalid_python_script_still_runs_original_argv_and_fails(self):
        (self.repo/'check.py').write_text('this is invalid Python !!!\n')
        r = self.create(); self.check(r, 'syntax', ok=False)
        self.assertEqual(self.ledger(r)['entries'], {})
        self.assertEqual([e['argv'] for e in self.events(r) if e['event'] == 'command_started'], self.task['checks'])

    def test_checkpoint_reuse_preserves_original_receipt_and_revalidates_inputs(self):
        import incremental_checkpoint as checkpoint
        from test_worker_session_reuse import EXECUTION
        binary = self.base/'unused-native-fixture'
        binary.write_text('#!/bin/sh\nexit 99\n'); binary.chmod(0o700)
        os.environ['CTASK_CODEX_BIN'] = str(binary)  # Profile hashing only; never invoked.
        self.plan.update(execution_policy=copy.deepcopy(EXECUTION),
                         checkpoint_policy=dict(version=1, closing_margin_seconds=2, interval_seconds=1))
        r = self.create(); original = self.check(r, 'first'); self.check(r, 'second')
        self.assertEqual(self.count(), 1)
        result = checkpoint._results(r, self.task)[0]
        self.assertEqual(result['status'], 'passed')
        reused = result['receipt']['verification_reuse']
        self.assertEqual(reused['evidence'], original[0]); self.assertEqual(reused['run'], str(r.dir))
        (self.repo/'input.txt').write_text('two')
        self.assertEqual(checkpoint._results(r, self.task)[0]['status'], 'invalidated')

    def test_diagnostic_poststate_drift_is_not_ignored(self):
        r = self.create(); self.check(r); self.check(r, 'same'); self.assertEqual(self.count(), 1)
        (self.repo/'count').write_text('broken diagnostic')
        self.check(r, 'changed-diagnostic', ok=False)
        self.assertEqual(self.ledger(r)['entries'], {})
        (self.repo/'count').unlink(); self.check(r, 'missing-diagnostic')
        self.assertEqual(self.count(), 1)
        self.assertEqual(len([e for e in self.events(r) if e['event'] == 'command_started']), 3)

    def test_oversized_inventory_fails_closed_before_enumerating_unbounded_tree(self):
        folder = self.repo/'inputs'; folder.mkdir()
        self.policy['checks']['artifact']['inputs'].append('inputs')
        for i in range(10): (folder/str(i)).write_text('fixture')
        r = self.create()
        with patch.object(cache, 'MAX_FILES', 5):
            self.check(r, 'a'); self.check(r, 'b')
        self.assertEqual(self.count(), 2); self.assertEqual(self.ledger(r)['entries'], {})

    def test_wrong_json_witness_types_and_malformed_receipt_are_not_passes(self):
        for malformed in ([], None, 'wrong type'):
            with self.subTest(malformed=malformed):
                r = self.create(); self.check(r, 'initial')
                before = self.count(); p = cache._root(r)/'receipts.json'
                p.write_text(json.dumps(malformed))
                self.check(r, 'unknown-a'); self.check(r, 'unknown-b')
                self.assertEqual(self.count(), before+2)
                self.assertEqual(json.loads(p.read_text()), malformed)
        r = self.create(); self.check(r, 'initial'); before = self.count()
        data = self.ledger(r); key = next(iter(data['entries'])); data['entries'][key] = []
        cache._private_save(cache._root(r)/'receipts.json', data)
        self.check(r, 'bad-receipt'); self.check(r, 'valid-after-real-execution')
        self.assertEqual(self.count(), before+1)


if __name__ == '__main__':
    unittest.main()
