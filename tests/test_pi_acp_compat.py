"""Versioned backend boundaries and failure atomicity; no native/provider calls."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from ato_pi import pi_acp_compat as compat
from ato_pi import pi_session_profile
from scripts import derive_pi_acp
from scripts.history import prepare_acp_process_candidate, prepare_session_candidate

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'worker': '6dfbd37ca70ece07c4fe7242cf0c4389eafc7a6dc48102db43bb3f0c1b12d275',
    'coordinator': '1f9eb67469862d835192f2ea86266ecf7a1d58eefd4dec4e77d5f35447790bc8',
}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.raw = compat.SOURCE.read_text()

    def test_exact_role_outputs_are_deterministic_and_facades_are_shared(self):
        self.assertIs(derive_pi_acp.derive, compat.derive)
        self.assertIs(derive_pi_acp.replace_once, compat.replace_once)
        self.assertIs(pi_session_profile.derive_coordinator, compat.derive_coordinator)
        for role, expected in EXPECTED.items():
            with self.subTest(role=role):
                first = compat.derive(self.raw, role=role)
                self.assertEqual(first, compat.derive(self.raw, role=role))
                self.assertEqual(hashlib.sha256(first.encode()).hexdigest(), expected)
                self.assertEqual(compat.backend(role)['derived_sha256'][role], expected)
        self.assertEqual(compat.derive_coordinator(self.raw), compat.derive(self.raw, role='coordinator'))
        self.assertEqual(len(compat.BASE_TRANSFORMS), 9)
        self.assertEqual(len(compat.COORDINATOR_TRANSFORMS), 5)
        self.assertEqual(len({t.name for t in compat.BASE_TRANSFORMS+compat.COORDINATOR_TRANSFORMS}), 14)

    def test_each_named_transformation_rejects_missing_and_duplicate_points_safely(self):
        for role in EXPECTED:
            source = self.raw
            transforms = compat.BASE_TRANSFORMS+(compat.COORDINATOR_TRANSFORMS if role == 'coordinator' else ())
            for transform in transforms:
                for damaged in (source.replace(transform.before, 'synthetic private source'),
                                source+'\n'+transform.before):
                    with self.subTest(role=role, transform=transform.name), self.assertRaises(ValueError) as err:
                        transform.apply(damaged)
                    self.assertIn(transform.name, str(err.exception))
                    self.assertNotIn('synthetic private source', str(err.exception))
                    self.assertNotIn(transform.before, str(err.exception))
                source = transform.apply(source)

    def test_unknown_backend_role_capability_and_incomplete_role_contract_reject(self):
        for change in ('backend', 'version', 'name', 'extra_capability', 'missing_capability',
                       'duplicate_capability', 'missing_role', 'extra_role', 'invalid_hash'):
            data = compat.registry()
            entry = data['adapter']
            if change in ('backend', 'version', 'name'): entry[change] = 'unsupported synthetic'
            if change == 'extra_capability': entry['capabilities']['worker'].append('load_resume')
            if change == 'missing_capability': entry['capabilities']['coordinator'].remove('native_identity')
            if change == 'duplicate_capability': entry['capabilities']['worker'].append('native_receipt')
            if change == 'missing_role': del entry['capabilities']['coordinator']
            if change == 'extra_role': entry['derived_sha256']['planner'] = '0'*64
            if change == 'invalid_hash': entry['source_sha256'] = 'malformed'
            with self.subTest(change=change), patch.object(compat, 'registry', return_value=data), \
                    self.assertRaises(compat.CompatibilityError):
                compat.derive_directory(self.base/'output')
            self.assertFalse((self.base/'output').exists())
        for role in ('planner', '', None):
            with self.assertRaises(compat.CompatibilityError): compat.derive(self.raw, role=role)

    def test_unknown_raw_hash_is_rejected_before_any_transform(self):
        for source in (self.raw+'\n', 'synthetic credential source', compat.derive(self.raw)):
            with patch.object(compat.Transformation, 'apply') as apply, self.assertRaises(ValueError) as err:
                compat.derive(source)
            apply.assert_not_called()
            self.assertNotIn('synthetic credential source', str(err.exception))

    def test_wrong_final_hash_rejects_without_publishing(self):
        data = compat.registry()
        data['adapter']['derived_sha256']['worker'] = '0'*64
        with patch.object(compat, 'registry', return_value=data), self.assertRaisesRegex(ValueError, 'output fingerprint'):
            compat.derive_directory(self.base/'output')
        self.assertEqual(list(self.base.iterdir()), [])

    def test_installed_package_identity_and_source_link_are_rejected(self):
        source = self.base/'package/dist/index.js'; source.parent.mkdir(parents=True)
        source.write_text(self.raw)
        package = source.parents[1]/'package.json'
        for identity in (dict(name='other', version='0.0.34'), dict(name='pi-acp', version='9.9.9')):
            package.write_text(json.dumps(identity))
            with patch.object(compat, 'SOURCE', source), self.assertRaisesRegex(ValueError, 'identity'):
                compat.derive_directory(self.base/'output')
        source.unlink(); source.symlink_to(compat.SOURCE)
        with patch.object(compat, 'SOURCE', source), self.assertRaises(OSError):
            compat.derive_directory(self.base/'output')
        self.assertFalse((self.base/'output').exists())

    def test_publication_is_private_complete_and_never_overwrites_existing_paths(self):
        for role in EXPECTED:
            output = self.base/role
            adapter = compat.derive_directory(output, role=role)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            for path in (adapter, output/'package.json'):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashlib.sha256(adapter.read_bytes()).hexdigest(), EXPECTED[role])
            for name in ('@agentclientprotocol', 'cross-spawn'):
                self.assertTrue((output/'node_modules'/name).is_symlink())
                self.assertEqual((output/'node_modules'/name).resolve(), compat.dependency(name))
            old = adapter.read_bytes()
            with self.assertRaisesRegex(ValueError, 'already exists'): compat.derive_directory(output, role=role)
            self.assertEqual(adapter.read_bytes(), old)
        for name in ('empty', 'regular', 'alias', 'dangling'):
            output = self.base/name
            if name == 'empty': output.mkdir()
            elif name == 'regular': output.write_text('owned')
            else: output.symlink_to(self.base/('empty' if name == 'alias' else 'absent'))
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'already exists'):
                compat.derive_directory(output)
        self.assertFalse(list(self.base.glob('.ato-adapter-*')))

    def test_atomic_publish_does_not_replace_a_directory_created_concurrently(self):
        output = self.base/'race'
        publish = compat._publish
        def raced(staging, target):
            target.mkdir()  # Another host wins immediately before the OS rename.
            publish(staging, target)
        with patch.object(compat, '_publish', side_effect=raced), self.assertRaisesRegex(ValueError, 'already exists'):
            compat.derive_directory(output)
        self.assertEqual(list(output.iterdir()), [])
        self.assertFalse(list(self.base.glob('.ato-adapter-*')))

    def test_write_or_publish_failure_cleans_temporary_output_and_keeps_old_runtime(self):
        old = self.base/'old'; old.mkdir(); (old/'evidence').write_text('retained')
        for method in ('_write_new', '_publish'):
            with self.subTest(method=method), patch.object(compat, method, side_effect=OSError('synthetic disk failure')), \
                    self.assertRaises(OSError):
                compat.derive_directory(self.base/'new')
            self.assertFalse((self.base/'new').exists())
            self.assertFalse(list(self.base.glob('.ato-adapter-*')))
            self.assertEqual((old/'evidence').read_text(), 'retained')

    def test_derivation_never_changes_source_dependencies_or_lock(self):
        root = ROOT/'integrations/pi-acp'
        def snapshot():
            return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in root.rglob('*') if p.is_file()}
        before = snapshot()
        for role in EXPECTED: compat.derive_directory(self.base/role, role=role)
        self.assertEqual(before, snapshot())

    def test_cli_preserves_hash_output_and_safe_errors(self):
        command = [sys.executable, str(ROOT/'scripts/derive_pi_acp.py'), str(self.base/'adapter')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), EXPECTED['worker'])
        rejected = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn('Traceback', rejected.stderr)
        self.assertIn('compatibility contract rejected', rejected.stderr)

    def test_compat_has_no_business_imports_or_scattered_transformations(self):
        tree = ast.parse((ROOT/'ato_pi/pi_acp_compat.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom): names = [node.module]
            else: continue
            self.assertTrue(all(n.split('.')[0] in sys.stdlib_module_names
                                or n == 'ato_pi.pi_toolchain' for n in names), names)
        for name in ('ato_pi/pi_session_profile.py', 'ato_pi/pi_acp_worker.py', 'scripts/derive_pi_acp.py'):
            tree = ast.parse((ROOT/name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotEqual(node.func.id, 'replace_once', name)
        code = ('import sys; sys.path.insert(0, '+repr(str(ROOT))+'); '
                'from ato_pi import pi_acp_compat; assert "ato_adapters.agents" not in sys.modules')
        empty = self.base/'empty'; empty.mkdir()
        result = subprocess.run([sys.executable, '-I', '-c', code], cwd=empty,
            env=dict(HOME=str(empty), PATH=''), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(empty.iterdir()), [])

    def test_toolchain_modules_and_registry_are_distributed_and_indexed(self):
        data = tomllib.loads((ROOT/'pyproject.toml').read_text())['tool']['setuptools']
        self.assertIn('ato_pi', data['packages'])
        for path in ('ato_pi/pi_toolchain.py', 'ato_pi/pi_acp_compat.py'):
            self.assertTrue((ROOT/path).is_file())
            self.assertIn(path, (ROOT/'docs/source-map.md').read_text())
            self.assertIn(path, prepare_acp_process_candidate.FILES)
        self.assertIn('integrations/pi-acp/compatibility.json',
            data['data-files']['share/agent-task-orchestrator/integrations/pi-acp'])
        for name in ('prepare_acp_process_candidate.py', 'prepare_agent_candidate.py',
                     'prepare_recovery_candidate.py', 'prepare_session_candidate.py'):
            self.assertIn('compatibility.json', (ROOT/'scripts/history'/name).read_text())
        for name in ('ato_pi/pi_toolchain.py', 'ato_pi/pi_acp_compat.py', 'integrations/pi-acp/compatibility.json'):
            self.assertIn(name, prepare_session_candidate.files(ROOT))
