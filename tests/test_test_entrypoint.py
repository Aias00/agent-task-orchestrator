"""Preflight failures must be actionable and must never masquerade as skips."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_tests


class TestEntrypointTests(unittest.TestCase):
    def test_old_python_fails_before_dependency_checks_or_test_imports(self):
        error = io.StringIO()
        with patch.object(run_tests.sys, 'version_info', (3, 9, 6)), redirect_stderr(error), \
                patch.object(run_tests.importlib.util, 'find_spec') as probe, \
                patch.object(run_tests.unittest, 'TestLoader') as loader:
            self.assertEqual(run_tests.main(), 2)
        self.assertIn('Python 3.12 or later', error.getvalue())
        self.assertIn('.venv/bin/python', error.getvalue())
        probe.assert_not_called()
        loader.assert_not_called()

    def test_missing_dependencies_are_listed_with_pinned_install_commands(self):
        for python_missing, node_missing in ((True, True), (True, False), (False, True)):
            with self.subTest(python_missing=python_missing, node_missing=node_missing), \
                    tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                if not node_missing:
                    entry = root/'integrations/pi-acp/node_modules/pi-acp/dist/index.js'
                    entry.parent.mkdir(parents=True)
                    entry.write_text('// synthetic installed entry point')
                error = io.StringIO()
                with patch.object(run_tests, 'ROOT', root), redirect_stderr(error), \
                        patch.object(run_tests.importlib.util, 'find_spec',
                                     return_value=None if python_missing else object()) as probe, \
                        patch.object(run_tests.unittest, 'TestLoader') as loader:
                    self.assertEqual(run_tests.main(), 2)
                output = error.getvalue()
                probe.assert_called_once_with('acp')
                loader.assert_not_called()
                self.assertEqual('agent-client-protocol' in output, python_missing)
                self.assertEqual('node_modules/pi-acp/dist/index.js' in output, node_missing)
                for command in ('python3.12 -m venv .venv',
                                '.venv/bin/python -m pip install -r requirements-acp.lock',
                                'npm ci --ignore-scripts --no-audit --no-fund --prefix integrations/pi-acp',
                                '.venv/bin/python scripts/run_tests.py'):
                    self.assertIn(command, output)

    def test_ready_dependencies_pass_without_diagnostics(self):
        error = io.StringIO()
        with patch.object(run_tests.importlib.util, 'find_spec', return_value=object()), \
                patch.object(Path, 'is_file', return_value=True), redirect_stderr(error):
            self.assertTrue(run_tests.preflight())
        self.assertEqual(error.getvalue(), '')

    def test_failures_and_skips_cannot_return_success(self):
        for successful, skipped, expected in ((True, [], 0), (False, [], 1), (True, ['synthetic skip'], 1)):
            with self.subTest(successful=successful, skipped=skipped), \
                    patch.object(run_tests, 'preflight', return_value=True), \
                    patch.object(run_tests.os, 'chdir'), \
                    patch.dict(run_tests.os.environ, CTASK_HANDOFF_READER='/untrusted/inherited-reader'), \
                    patch.object(run_tests.sys, 'path', list(run_tests.sys.path)), \
                    patch.object(run_tests.unittest, 'TestLoader'), \
                    patch.object(run_tests.unittest, 'TextTestRunner') as executor:
                executor.return_value.run.return_value = SimpleNamespace(
                    wasSuccessful=lambda: successful, skipped=skipped)
                self.assertEqual(run_tests.main(), expected)
                self.assertNotIn('CTASK_HANDOFF_READER', run_tests.os.environ)
