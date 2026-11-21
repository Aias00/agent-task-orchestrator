"""Source-only profile boundary qualification; all CLI traffic is synthetic."""
from contextlib import ExitStack
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
import codex_worker_session as native
import profile_evidence as evidence
import runner
import worker_sessions
from scripts import execution_efficiency_actual as actual
from scripts import prepare_efficiency_candidate as bundle


class ProfileDriftEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-drift-synthetic-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        self.home = self.folder/'synthetic-native'; self.home.mkdir()
        self.config = self.home/'config.toml'
        self.config.write_text('model="fixture-model-v1"\n# PRIVATE_CONFIG_SENTINEL\n')
        self.auth = self.home/'auth.json'; self.auth.write_text('PRIVATE_AUTH_SENTINEL')
        self.binary = self.folder/'synthetic-cli'
        self.binary.write_bytes((ROOT/'tests/fixtures/execution_efficiency_cli.py').read_bytes())
        self.binary.chmod(0o755)
        env = dict(os.environ, ATO_HOME=str(self.folder/'ato'), CODEX_HOME=str(self.home),
            CTASK_CODEX_BIN=str(self.binary), OPENAI_API_KEY='PRIVATE_ENV_SENTINEL', PYTHONDONTWRITEBYTECODE='1')
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN',
                    'CODEX_PROFILE', 'CODEX_CONFIG_PROFILE'):
            env.pop(key, None)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        self.stack.enter_context(patch.object(runner, 'process_identity', side_effect=lambda pid:'synthetic-owner-'+str(pid)))
        original_file = native._file
        def no_auth(path):
            self.assertNotEqual(Path(path).name, 'auth.json')
            return original_file(path)
        self.stack.enter_context(patch.object(native, '_file', side_effect=no_auth))

    def sample(self, arm='foreground', index=1):
        return actual.sample(self.folder, arm, index, 'fixture-model-v1', time.monotonic()+500,
                             require_native_tools=False)

    def observations(self):
        return [json.loads(p.read_text()) for p in sorted((self.folder/'profile-evidence').glob('*.json'))]

    def calls(self):
        return sum(len(p.read_text().splitlines()) for p in self.folder.rglob('fixture-receipts.jsonl'))

    def assert_private(self, value):
        text = json.dumps(value)
        for body in ('PRIVATE_CONFIG_SENTINEL', 'PRIVATE_ENV_SENTINEL', 'PRIVATE_AUTH_SENTINEL',
                     str(self.home), str(self.binary), 'fixture-model-v1', actual.OBJECTIVE):
            self.assertNotIn(body, text)
        self.assertLess(len(text), 25000)

    def mutate_after_receipt(self, action):
        original = native.receipt
        def receipt(*args, **kwargs):
            value = original(*args, **kwargs)
            action()
            return value
        return patch.object(native, 'receipt', side_effect=receipt)

    def assert_drift(self, field, action, arm='foreground'):
        with self.mutate_after_receipt(action):
            with self.assertRaises(ValueError): self.sample(arm)
        observations = self.observations()
        self.assertEqual(len(observations), 1)
        record = observations[0]
        self.assertEqual(record['status'], 'post_drift')
        self.assertIn(field, record['comparison']['changed_fields'])
        self.assertFalse(record['comparison']['equal'])
        self.assertEqual(self.calls(), 1)
        self.assert_private(record)
        self.assertEqual(self.auth.read_text(), 'PRIVATE_AUTH_SENTINEL')
        return record

    def test_config_body_change_records_hashes_and_stops_before_second_turn(self):
        record = self.assert_drift('configs', lambda: self.config.write_text('model="other"\nPRIVATE_CHANGED_BODY'))
        key = 'configs:'+evidence.digest(str(self.config))
        self.assertEqual(record['pre']['config_states'][key], 'present')
        self.assertEqual(record['post']['config_states'][key], 'present')

    def test_binary_replacement_fails_closed_without_second_launch(self):
        self.assert_drift('binary_sha256', lambda: self.binary.write_bytes(self.binary.read_bytes()+b'\n# changed\n'))

    def test_environment_change_is_hash_only_and_fails_closed(self):
        self.assert_drift('environment_hash', lambda: os.environ.update(OPENAI_API_KEY='PRIVATE_CHANGED_KEY'))

    def test_model_change_in_sample_profile_is_not_ignored(self):
        original = native.profile; changed = []
        def read(plan, repo):
            value = original(plan, repo)
            if changed: value['model'] = 'PRIVATE_CHANGED_MODEL'
            return value
        with patch.object(native, 'profile', side_effect=read):
            self.assert_drift('model', lambda: changed.append(True))

    def test_permission_change_in_sample_profile_is_not_ignored(self):
        original = native.profile; changed = []
        def read(plan, repo):
            value = copy.deepcopy(original(plan, repo))
            if changed: value['permissions']['network_access'] = True
            return value
        with patch.object(native, 'profile', side_effect=read):
            self.assert_drift('permissions', lambda: changed.append(True))

    def test_source_manifest_change_fails_closed(self):
        original = native.profile; changed = []
        def read(plan, repo):
            value = original(plan, repo)
            if changed: value['source_manifest_sha256'] = 'f'*64
            return value
        with patch.object(native, 'profile', side_effect=read):
            self.assert_drift('source_manifest_sha256', lambda: changed.append(True))

    def test_synthetic_startup_created_config_is_drift_never_whitelisted(self):
        self.config.unlink()
        # The copied synthetic subprocess materializes config at startup; no
        # preflight/warmup or real native home is involved in this reproduction.
        self.binary.write_text(self.binary.read_text().replace(
            'home.mkdir(parents=True, exist_ok=True)',
            "home.mkdir(parents=True, exist_ok=True)\n(home/'config.toml').write_text('model=\"fixture-model-v1\"\\n')"))
        with self.assertRaises(evidence.ProfileDriftError): self.sample()
        record = self.observations()[0]
        self.assertEqual(record['status'], 'post_drift')
        self.assertIn('configs', record['comparison']['changed_fields'])
        self.assertEqual(self.calls(), 1)
        self.assert_private(record)
        key = 'configs:'+evidence.digest(str(self.config))
        self.assertEqual(record['pre']['config_states'][key], 'absent')
        self.assertEqual(record['post']['config_states'][key], 'present')
        self.assertNotEqual(record['pre']['profile_sha256'], record['post']['profile_sha256'])
        self.assertTrue(any(k.startswith('configs:') for k in record['comparison']['changed_fields']))

    def test_runner_drift_retains_claim_unknown_cleanup_and_no_replay_or_host_promotion(self):
        self.assert_drift('configs', lambda: self.config.write_text('PRIVATE_DRIFT'), arm='warm_ato')
        stage = next((self.folder/'warm_ato-1/.codex/tasks').glob('20*'))
        r = runner.Runner(stage); task = r.plan['tasks'][0]
        saved = worker_sessions.load(r, task)
        self.assertEqual(saved['state'], 'delivery_unknown')
        self.assertTrue(saved['cleanup_confirmed'])
        self.assertEqual(saved['generation'], 1)
        self.assertEqual(saved['turns'], [])
        self.assertEqual(saved['profile_observation']['status'], 'post_drift')
        self.assert_private(saved['profile_observation'])
        history = copy.deepcopy(r.state['tasks']['artifact']['execution_accounting'])
        self.assertEqual(history['invocations'], 1)
        self.assertEqual(history['failures'], 1)
        r.attempt(task, '')
        # The cleaned synthetic sample owner relinquishes its foreground run.
        # A normal new host owner still cannot promote an unknown delivery.
        r.state.update(owner_pid=None, owner_identity=None, status='blocked')
        r.persist()
        r.execute(host_checks=True)
        self.assertFalse(worker_sessions.host_acceptable(r, task))
        self.assertEqual(self.calls(), 1)
        self.assertEqual(r.state['tasks']['artifact']['status'], 'blocked')
        self.assertEqual(r.state['tasks']['artifact']['execution_accounting'], history)
        self.assertEqual(worker_sessions.load(r, task)['correlation_id'], saved['correlation_id'])

    def test_success_keeps_both_turn_records_and_runner_exact_identity(self):
        for arm in actual.ARMS:
            result = self.sample(arm)
            records = [json.loads(Path(t['profile_evidence']).read_text()) for t in result['turns']]
            self.assertEqual([r['sequence'] for r in records], [1, 2])
            for record in records:
                self.assertEqual(record['status'], 'unchanged')
                self.assertTrue(record['comparison']['equal'])
                self.assertFalse(record['execution_raised'])
                self.assert_private(record)
            if arm == 'warm_ato':
                r = runner.Runner(result['stage']); saved = worker_sessions.load(r, r.plan['tasks'][0])
                self.assertEqual([t['profile_observation']['sequence'] for t in saved['turns']], [1, 2])
                self.assertEqual(len({t['native_id'] for t in saved['turns']}), 1)
                self.assertTrue(all(t['profile_observation']['status']=='unchanged' for t in saved['turns']))

    def test_failed_receipt_still_saves_post_evidence_without_exception_body(self):
        with patch.object(native, 'receipt', side_effect=ValueError('PRIVATE_EXCEPTION_BODY')):
            with self.assertRaisesRegex(ValueError, 'PRIVATE_EXCEPTION_BODY'): self.sample()
        record = self.observations()[0]
        self.assertIsNotNone(record['post'])
        self.assertTrue(record['execution_raised'])
        self.assertNotIn('PRIVATE_EXCEPTION_BODY', json.dumps(record))
        self.assertEqual(self.calls(), 1)

    def test_unreadable_post_is_unknown_not_success_and_no_second_turn(self):
        with self.mutate_after_receipt(lambda: self.config.unlink()):
            original = native.profile
            def read(plan, repo):
                if not self.config.exists(): raise OSError('PRIVATE_EXCEPTION_BODY')
                return original(plan, repo)
            with patch.object(native, 'profile', side_effect=read):
                with self.assertRaisesRegex(ValueError, 'post-observation unavailable'): self.sample()
        record = self.observations()[0]
        self.assertEqual(record['status'], 'post_unobservable')
        self.assertIsNone(record['post']); self.assertEqual(self.calls(), 1)
        self.assertNotIn('PRIVATE_EXCEPTION_BODY', json.dumps(record))

    def test_effective_native_model_mismatch_has_body_free_evidence(self):
        self.binary.write_text(self.binary.read_text().replace(
            "model='fixture-model-v1', model_provider='scripted-offline')",
            "model='PRIVATE_EFFECTIVE_MODEL', model_provider='scripted-offline')"))
        with self.assertRaisesRegex(evidence.ProfileDriftError, 'effective model/permissions mismatch'):
            self.sample()
        record = self.observations()[0]
        self.assertIn('model', record['effective_context_drift']['changed_fields'])
        self.assertTrue(record['execution_raised'])
        self.assertNotIn('PRIVATE_EFFECTIVE_MODEL', json.dumps(record))
        self.assert_private(record)
        self.assertEqual(self.calls(), 1)

    def test_effective_native_grant_widening_is_unknown_in_runner(self):
        self.binary.write_text(self.binary.read_text().replace(
            "sandbox_policy=dict(type=mode, writable_roots=[], network_access=False),",
            "sandbox_policy=dict(type=mode, writable_roots=[], network_access=True),"))
        with self.assertRaises(ValueError): self.sample('warm_ato')
        stage = next((self.folder/'warm_ato-1/.codex/tasks').glob('20*'))
        r = runner.Runner(stage); saved = worker_sessions.load(r, r.plan['tasks'][0])
        self.assertEqual(saved['state'], 'delivery_unknown')
        self.assertIn('sandbox_policy', saved['profile_observation']['effective_context_drift']['changed_fields'])
        self.assertEqual(self.calls(), 1)
        self.assert_private(saved['profile_observation'])

    def test_bounded_diagnostics_do_not_turn_truncation_into_admission(self):
        before = {('PRIVATE_FIELD_'+str(n)): 'PRIVATE_VALUE' for n in range(400)}
        after = dict(before, PRIVATE_LAST='PRIVATE_VALUE')
        records = []
        with self.assertRaises(evidence.ProfileDriftError):
            with evidence.Turn(before, lambda: after, lambda r: records.append(copy.deepcopy(r)), 1):
                self.fail('must not launch')
        self.assertEqual(records[-1]['status'], 'pre_drift')
        self.assertTrue(records[-1]['admission']['truncated'])
        self.assertFalse(records[-1]['admission']['equal'])
        self.assertLessEqual(len(records[-1]['pre']['fields']), evidence.MAX_FIELDS)
        self.assertNotIn('PRIVATE_', json.dumps(records))

    def test_profile_capture_is_read_only_no_home_copy_or_warmup_invocation(self):
        repo = self.folder/'read-only-repo'; repo.mkdir()
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        for _ in range(2): evidence.snapshot(native.profile(dict(model='fixture-model-v1'), repo))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.home.iterdir()})
        self.assertEqual(list(repo.iterdir()), [])
        self.assertEqual(self.calls(), 0)

    def test_per_manifest_failed_claim_survives_and_blocks_second_origin_attempt(self):
        fake = self.folder/'fake-launcher'; fake.write_bytes(b'synthetic launcher')
        manifest = self.folder/bundle.MANIFEST; manifest.parent.mkdir(parents=True); manifest.write_text('{}')
        os.environ.pop('CTASK_CODEX_BIN')
        with patch.object(actual, 'ROOT', self.folder), patch.object(bundle, 'check', return_value={}), \
             patch.object(actual.shutil, 'which', return_value=str(fake)), \
             patch.object(native, 'TRUSTED_QUEUE_GATE', actual.sha(fake.read_bytes())), \
             patch.object(native, '_binary', return_value=(str(self.binary), 'b'*64, {})), \
             patch.object(actual, 'selected_model', return_value='fixture-model-v1'):
            # run_origin's binary gate is mocked exclusively for an offline test.
            # sample itself uses only the copied synthetic subprocess fixture.
            original = actual.sample
            def synthetic(*args, **kwargs):
                kwargs['require_native_tools'] = False
                return original(*args, **kwargs)
            with patch.object(actual, 'sample', side_effect=synthetic), \
                 self.mutate_after_receipt(lambda: self.config.write_text('PRIVATE_DRIFT')):
                with self.assertRaises(evidence.ProfileDriftError): actual.run_origin(self.folder/'proof.json', 12)
            claim = self.folder/'build/execution-efficiency/host-evidence'/actual.sha(manifest.read_bytes())
            journal = json.loads((claim/'journal.json').read_text())
            self.assertEqual(journal['status'], 'failed_or_unknown_no_replay')
            self.assertFalse(journal['automatic_resample'])
            self.assertEqual(len(list(claim.rglob('*turn-1.json'))), 1)
            with patch.object(actual, 'sample', side_effect=AssertionError('must not replay')):
                with self.assertRaisesRegex(ValueError, 'already claimed'): actual.run_origin(self.folder/'proof.json', 12)
            self.assertEqual(self.calls(), 1)


if __name__ == '__main__': unittest.main()
