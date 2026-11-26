"""Metadata, document inventory and inert historical tooling contracts."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

from scripts.history import prepare_session_candidate

ROOT = Path(__file__).resolve().parents[1]
HISTORY = Path('scripts/history')
SCRIPTS = ('prepare_acp_process_candidate.py', 'prepare_agent_candidate.py',
           'prepare_recovery_candidate.py', 'prepare_session_candidate.py')


class RepositoryMetadataTests(unittest.TestCase):
    def test_metadata_core_has_no_dependencies_and_acp_matches_exact_lock(self):
        data = tomllib.loads((ROOT/'pyproject.toml').read_text())
        self.assertEqual(data['project']['requires-python'], '>=3.12')
        self.assertEqual(data['project']['dependencies'], [])
        expected = [line for line in (ROOT/'requirements-acp.lock').read_text().splitlines() if line and not line.startswith('#')]
        self.assertEqual(data['project']['optional-dependencies']['acp'], expected)
        self.assertFalse(data['project'].get('scripts'))
        self.assertEqual(set(data['tool']['setuptools']['py-modules']), {p.stem for p in ROOT.glob('*.py')})
        self.assertEqual(set(data['tool']['setuptools']['packages']),
                         {path.parent.name for path in ROOT.glob('ato_*/__init__.py')})

    def test_document_index_covers_existing_markdown(self):
        index = (ROOT/'docs/README.md').read_text()
        for section in ('## 现行指南', '## 能力状态', '## 历史验收档案（只读参考）'):
            self.assertIn(section, index)
        targets = {(ROOT/'docs'/target).resolve() for target in re.findall(r'\]\(([^)]+)\)', index)}
        expected = {p.resolve() for p in (ROOT/'docs').rglob('*.md') if p.name != 'README.md'}
        self.assertTrue(expected <= targets)

    def test_historical_scripts_missing_private_inputs_fail_cleanly_before_any_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/HISTORY).mkdir(parents=True)
            for name in SCRIPTS:
                shutil.copyfile(ROOT/HISTORY/name, root/HISTORY/name)
                for args in ([], ['--check']):
                    with self.subTest(script=name, args=args):
                        result = subprocess.run([sys.executable, str(root/HISTORY/name), *args],
                            cwd=root, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                        self.assertIn('Historical internal qualification', result.stderr)
                        self.assertIn('not source installation or daily tests', result.stderr)
                        self.assertIn('operator-evidence', result.stderr)
                        self.assertNotIn('Traceback', result.stderr)
                        self.assertFalse((root/'build').exists())

    def test_historical_script_help_does_not_require_operator_evidence(self):
        for name in SCRIPTS:
            with self.subTest(script=name):
                result = subprocess.run([sys.executable, str(ROOT/HISTORY/name), '--help'],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--check', result.stdout)

    def test_source_inventory_includes_runtime_packages_and_project_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            names = {'runner.py', 'ato_common/ato_core.py', 'ato_cli/__init__.py', 'ato_task/ato_application.py',
                     'ato_adapters/codex_session_adapter.py', 'ato_release/seal.py',
                     'pyproject.toml', 'requirements-acp.lock'}
            for name in names:
                target = root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('synthetic portable input')
            self.assertEqual(set(prepare_session_candidate.files(root)), names)
