"""Inactive portable delivery and origin-engine qualification; synthetic only."""
from contextlib import ExitStack
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import execution_efficiency_actual as actual
from scripts import prepare_efficiency_candidate as bundle
from scripts import verify_execution_efficiency_local as proof
import runner


class PortableCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build/check only from bundled read-only provenance; no external writes.
        cls.current = bundle.build(ROOT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-portable-test-'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in self.current['files']:
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/name, path); path.chmod(self.current['file_modes'][name])
        self.manifest = bundle.build(self.root)
        bundle.atomic_write(self.root/bundle.MANIFEST, bundle.encoded(self.manifest))
        bundle.atomic_write(self.root/bundle.ARCHIVE, bundle.archive_bytes(self.root, self.manifest))

    def test_manifest_complete_and_inactive(self):
        value = bundle.check(self.root)
        self.assertFalse(value['runtime_activated']); self.assertFalse(value['overall_accepted'])
        self.assertEqual(value['predecessor_manifest_sha256'], '2256534fb9f58ec277f18511b5662479a8da82aa0433a58657a121b200bc02f6')
        self.assertTrue({p.name for p in ROOT.glob('*.py')} <= set(value['files']))
        self.assertIn('scripts/execution_efficiency_actual.py', value['files'])
        self.assertIn('tests/test_efficiency_delivery.py', value['files'])

    def test_deterministic_archive_ignores_source_mtime(self):
        before = (self.root/bundle.ARCHIVE).read_bytes()
        os.utime(self.root/'runner.py', (123456, 123456))
        self.assertEqual(before, bundle.archive_bytes(self.root, bundle.build(self.root)))

    def test_source_change_refuses_original_manifest(self):
        path = self.root/'runner.py'; path.write_text(path.read_text()+'\n# changed\n')
        with self.assertRaises(ValueError): bundle.check(self.root)

    def test_mode_change_refuses_original_manifest(self):
        (self.root/'runner.py').chmod(0o755 if self.manifest['file_modes']['runner.py'] != 0o755 else 0o644)
        with self.assertRaises(ValueError): bundle.check(self.root)

    def test_missing_new_source_is_not_silently_omitted(self):
        (self.root/'worker_sessions.py').unlink()
        with self.assertRaises(ValueError): bundle.check(self.root)

    def test_added_shadow_module_invalidates_inventory(self):
        (self.root/'shadow.py').write_text('# new runtime input\n')
        with self.assertRaises(ValueError): bundle.check(self.root)

    def test_protected_test_drift_refused_before_packaging(self):
        baseline = json.loads((self.root/bundle.BASELINE).read_text())
        name = next(iter(baseline['protected_tests']))
        (self.root/name).write_text('changed protected test')
        with self.assertRaisesRegex(ValueError, 'protected'): bundle.build(self.root)

    def test_archive_change_refused(self):
        path = self.root/bundle.ARCHIVE; path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError, 'archive'): bundle.check(self.root)

    def test_links_and_parent_paths_refused(self):
        outside = self.root/'outside'; outside.write_text('untouched')
        (self.root/'escape.py').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'): bundle.build(self.root)
        for name in ('../outside', '/tmp/outside', '.codex/state.json', 'auth/file.json'):
            with self.subTest(name=name), self.assertRaises(ValueError): bundle.read_portable(self.root, name)
        self.assertEqual(outside.read_text(), 'untouched')

    def test_provenance_chain_cannot_be_replaced(self):
        (self.root/bundle.PREDECESSOR).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'predecessor'): bundle.build(self.root)

    def test_archive_has_only_portable_regular_members_and_exact_modes(self):
        with tarfile.open(self.root/bundle.ARCHIVE) as archive:
            names = []
            for member in archive:
                names.append(member.name)
                self.assertTrue(member.isfile()); self.assertFalse(Path(member.name).is_absolute())
                self.assertFalse(set(Path(member.name).parts) & bundle.FORBIDDEN)
                self.assertEqual(member.uid, 0); self.assertEqual(member.gid, 0); self.assertEqual(member.mtime, 0)
                if member.name in self.manifest['files']:
                    self.assertEqual(member.mode, self.manifest['file_modes'][member.name])
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(set(names), set(self.manifest['files']) | {str(bundle.MANIFEST)})

    def test_extracted_bundle_standard_library_cli_without_original_archive(self):
        target = self.root/'extract'; target.mkdir()
        with tarfile.open(self.root/bundle.ARCHIVE) as archive:
            for member in archive:
                destination = target/member.name; destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read()); destination.chmod(member.mode)
        result = subprocess.run([sys.executable, str(target/'scripts/prepare_efficiency_candidate.py'), '--check-extracted'],
            cwd=target, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        shutil.copyfile(self.root/bundle.ARCHIVE, target/bundle.ARCHIVE)
        self.assertEqual(bundle.check(target)['files'], self.manifest['files'])


class OriginEngineOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-origin-engine-fixture-'); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        env = dict(os.environ, CODEX_HOME=str(self.folder/'synthetic-native'),
            ATO_HOME=str(self.folder/'synthetic-ato'), CTASK_CODEX_BIN=str(ROOT/'tests/fixtures/execution_efficiency_cli.py'),
            PYTHONDONTWRITEBYTECODE='1')
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN', 'CODEX_PROFILE', 'CODEX_CONFIG_PROFILE'):
            env.pop(key, None)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        self.stack.enter_context(patch.object(runner, 'process_identity', side_effect=lambda pid:'synthetic-owner-'+str(pid)))

    def test_three_actual_engine_arms_use_synthetic_two_turn_workflows(self):
        results = {arm: actual.sample(self.folder, arm, 1, 'fixture-model-v1', time.monotonic()+500,
                    require_native_tools=False) for arm in actual.ARMS}
        for arm, result in results.items():
            self.assertEqual(len(result['turns']), 2)
            self.assertEqual(len({r['native_id'] for r in result['turns']}), 2 if arm == 'legacy_ato' else 1)
            self.assertTrue(result['independent_check']['fresh'])
            self.assertEqual(result['independent_check']['returncode'], 0)
            self.assertEqual(result['counts']['cli_process_startups'], 2)
            self.assertEqual(result['counts']['model_requests'], 'unknown')
            self.assertEqual(runner.read(Path(result['workflow'])/'state.json')['criteria']['artifact']['status'], 'passed')
        for field in ('profile_fingerprint', 'contract_sha256', 'initial_files_sha256', 'artifact_sha256'):
            self.assertEqual(len({r[field] for r in results.values()}), 1, field)
        self.assertLess(results['warm_ato']['counts']['check_executions'], results['legacy_ato']['counts']['check_executions'])
        self.assertTrue((Path(results['warm_ato']['stage'])/'runtime-selection.json').exists())
        self.assertEqual(runner.read(Path(results['warm_ato']['stage'])/'state.json')['tasks']['artifact']['execution_accounting']['failures'], 0)
        # Engine test receipts never claim actual qualification.
        for value in results.values():
            with self.assertRaises(ValueError): proof.validate_proof(value)

    def test_changed_checker_refused_before_host_can_execute_it(self):
        original = actual.native.receipt
        def replace_checker(*args, **kwargs):
            value = original(*args, **kwargs)
            (Path(args[2])/'check.py').write_text("raise RuntimeError('model replacement must never run')\n")
            return value
        with patch.object(actual.native, 'receipt', side_effect=replace_checker):
            with self.assertRaisesRegex(ValueError, 'refuse host execution'):
                actual.sample(self.folder, 'foreground', 1, 'fixture-model-v1', time.monotonic()+500,
                              require_native_tools=False)
        self.assertFalse(list(self.folder.rglob('check-count')))
        records = list(self.folder.rglob('fixture-receipts.jsonl'))
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0].read_text().splitlines()), 1)

    def test_missing_owner_stops_before_native_process(self):
        with patch.object(runner, 'process_identity', return_value=None):
            with self.assertRaisesRegex(ValueError, 'owner identity'):
                actual.sample(self.folder, 'warm_ato', 1, 'fixture-model-v1', time.monotonic()+500)
        self.assertFalse(list(self.folder.rglob('fixture-receipts.jsonl')))

    def test_expired_window_stops_before_native_process(self):
        with self.assertRaisesRegex(ValueError, 'bounded host window'):
            actual.sample(self.folder, 'foreground', 1, 'fixture-model-v1', time.monotonic())
        self.assertFalse(list(self.folder.rglob('fixture-receipts.jsonl')))

    def test_native_binary_override_rejected_without_invocation(self):
        with patch.object(actual.native, '_binary', side_effect=AssertionError('must not select native')):
            with self.assertRaisesRegex(ValueError, 'overrides forbidden'):
                actual.run_origin(self.folder/'proof.json', 12)

    def test_actual_claim_cannot_be_automatically_replayed(self):
        fake = self.folder/'fake-launcher'; fake.write_bytes(b'synthetic launcher')
        # Only gate plumbing is patched; no sample/native/credential operation.
        manifest = self.folder/'build/execution-efficiency/candidate.json'; manifest.parent.mkdir(parents=True)
        manifest.write_text('{}')
        claim = self.folder/'build/execution-efficiency/host-evidence'/actual.sha(manifest.read_bytes()); claim.mkdir(parents=True)
        os.environ.pop('CTASK_CODEX_BIN')
        with patch.object(actual, 'ROOT', self.folder), patch.object(bundle, 'check', return_value={}), \
             patch.object(actual.shutil, 'which', return_value=str(fake)), \
             patch.object(actual.native, 'TRUSTED_QUEUE_GATE', actual.sha(fake.read_bytes())), \
             patch.object(actual.native, '_binary', return_value=(str(fake), 'b'*64, {})), \
             patch.object(actual, 'selected_model', return_value='fixture-model-v1'), \
             patch.object(actual, 'sample', side_effect=AssertionError('no replay')):
            with self.assertRaisesRegex(ValueError, 'already claimed'): actual.run_origin(self.folder/'proof.json', 12)

    def test_failed_origin_sample_retains_claim_and_cannot_resample(self):
        fake = self.folder/'fake-launcher'; fake.write_bytes(b'synthetic launcher')
        manifest = self.folder/'build/execution-efficiency/candidate.json'; manifest.parent.mkdir(parents=True)
        manifest.write_text('{}')
        os.environ.pop('CTASK_CODEX_BIN')
        output = self.folder/'proof.json'
        with patch.object(actual, 'ROOT', self.folder), patch.object(bundle, 'check', return_value={}), \
             patch.object(actual.shutil, 'which', return_value=str(fake)), \
             patch.object(actual.native, 'TRUSTED_QUEUE_GATE', actual.sha(fake.read_bytes())), \
             patch.object(actual.native, '_binary', return_value=(str(fake), 'b'*64, {})), \
             patch.object(actual, 'selected_model', return_value='fixture-model-v1'), \
             patch.object(actual, 'sample', side_effect=ValueError('synthetic uncertain delivery')) as execute:
            with self.assertRaisesRegex(ValueError, 'uncertain delivery'): actual.run_origin(output, 12)
            record = json.loads(output.read_text())
            evidence = Path(record['retained_evidence'])
            journal = json.loads((evidence/'journal.json').read_text())
            self.assertEqual(journal['status'], 'failed_or_unknown_no_replay')
            self.assertFalse(journal['automatic_resample'])
            self.assertEqual(execute.call_count, 1)
            with self.assertRaisesRegex(ValueError, 'already claimed'): actual.run_origin(output, 12)
            self.assertEqual(execute.call_count, 1)
            self.assertTrue((evidence/'journal.json').exists())

    def test_config_model_selection_does_not_open_auth(self):
        home = self.folder/'synthetic-native'; home.mkdir()
        (home/'config.toml').write_text('model="existing-model"\nprofile="chosen"\n[profiles.chosen]\nmodel="same-profile-model"\n')
        (home/'auth.json').write_text('must never be read')
        original = actual.native._file
        def safe(path):
            self.assertNotEqual(Path(path).name, 'auth.json')
            return original(path)
        with patch.object(actual.native, '_file', side_effect=safe):
            self.assertEqual(actual.selected_model(), 'same-profile-model')
        self.assertEqual((home/'auth.json').read_text(), 'must never be read')

    def test_unknown_default_model_fails_before_native(self):
        with self.assertRaisesRegex(ValueError, 'model unknown'): actual.selected_model()

    def test_only_successful_file_tool_events_qualify(self):
        for rows in ([], [dict(type='item.completed', item=dict(type='command_execution', status='failed', exit_code=1))],
                [dict(type='item.started', item=dict(type='file_change', status='completed'))]):
            self.assertFalse(actual.observed_tool(rows))
        self.assertTrue(actual.observed_tool([dict(type='item.completed', item=dict(type='file_change', status='completed'))]))

    def test_legacy_raw_diagnostics_retained_without_hiding_json_failure(self):
        log = self.folder/'raw.log'; data = b'diagnostic retained\n{"type":"error","message":"failed"}\n'
        log.write_bytes(data)
        protocol, rows = actual.protocol_log(log)
        self.assertEqual(log.read_bytes(), data); self.assertEqual(rows[0]['type'], 'error')
        self.assertEqual(json.loads(protocol.read_text())['type'], 'error')

    def test_describe_does_not_launch_or_write_output(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/verify_execution_efficiency_local.py'), '--describe'],
            text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['max_native_turns'], 12)
        self.assertFalse((self.folder/'synthetic-native').exists())

if __name__ == '__main__': unittest.main()
