"""Structural contracts supplement the unchanged runtime/safety regression suite."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ato_core
import control
import recovery
import runner
from scripts.check_architecture import report

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ('now', 'read', 'save', 'digest', 'process_identity', 'Halt', 'locked', 'alive', 'pid_alive')


class ArchitectureStructureTests(unittest.TestCase):
    def test_foundational_modules_import_only_standard_library_and_private_directory_primitive(self):
        for name in ('ato_core', 'ato_config', 'agent_registry'):
            with self.subTest(module=name):
                tree = ast.parse((ROOT/(name+'.py')).read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        self.assertEqual(node.level, 0)
                        if (name, node.module) == ('ato_config', 'ato_core'):
                            self.assertEqual([alias.name for alias in node.names], ['ensure_private_dir'])
                            continue
                        modules = [node.module]
                    else:
                        continue
                    self.assertTrue(all(module.split('.')[0] in sys.stdlib_module_names for module in modules), modules)

    def test_runtime_never_imports_release_scripts(self):
        for source in (*ROOT.glob('*.py'), *(ROOT/'ato_cli').glob('*.py')):
            with self.subTest(source=source.name):
                tree = ast.parse(source.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or '']
                    else:
                        continue
                    self.assertTrue(all(name.split('.')[0] != 'scripts' for name in names))

    def test_mutual_dependencies_include_cli_and_introduce_no_new_pairs(self):
        baseline = json.loads((ROOT/'tests/fixtures/architecture-baseline.json').read_text())
        current = report(ROOT)
        self.assertEqual(baseline['count'], 31)
        self.assertLessEqual(current['root_mutual_count'], 8)
        self.assertLessEqual(current['runtime_mutual_count'], 8)
        self.assertTrue(set(map(tuple, current['runtime_mutual_pairs'])) <= set(map(tuple, baseline['mutual_pairs'])))
        self.assertIn('ato_cli', current['graph']['ato'])
        self.assertIn('ato_application', current['graph']['ato_cli'])
        self.assertIn('ato_execution', current['graph']['ato_application'])

    def test_legacy_modules_export_the_complete_primitive_api(self):
        for name in PRIMITIVES:
            self.assertIs(getattr(runner, name), getattr(ato_core, name))
            self.assertTrue(callable(getattr(recovery, name)))
            if name != 'pid_alive':
                self.assertIs(getattr(recovery, name), getattr(ato_core, name))
        self.assertIs(recovery.Halt, runner.Halt)
        with self.assertRaises(runner.Halt):
            raise ato_core.Halt('same public exception type')

    def test_foundational_imports_do_not_bootstrap_adapters_or_write_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command = ('import sys; sys.path.insert(0, '+repr(str(ROOT))+'); '
                       'import ato_core, ato_config, agent_registry; '
                       'assert "runner" not in sys.modules and "agents" not in sys.modules; '
                       'assert not agent_registry.kinds()')
            result = subprocess.run([sys.executable, '-I', '-c', command], cwd=root,
                env=dict(HOME=str(root/'home'), ATO_HOME=str(root/'ato'), CODEX_HOME=str(root/'native'), PATH=os.defpath),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(root.iterdir()), [])

    def test_legacy_runner_has_no_cli_facade_and_core_paths_are_preserved(self):
        tree = ast.parse((ROOT/'runner.py').read_text())
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == 'main' for node in tree.body))
        for name in ('control', 'recovery', 'interaction', 'delegation', 'workflow', 'session_router', 'message_bus', 'transport'):
            self.assertTrue((ROOT/(name+'.py')).is_file())

    def test_hub_switch_checks_pid_identity_and_preserves_legacy_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for identity, enabled, allowed in (({'pid_identity': 'reused'}, False, True),
                    ({'pid_identity': 'current'}, False, False), ({}, False, False),
                    ({'pid_identity': 'reused'}, True, False)):
                with self.subTest(identity=identity, enabled=enabled):
                    runner.save(directory/'config.json', dict(enabled=enabled, hub_thread='old', created_at=1))
                    runner.save(directory/'process.json', dict(pid=123, **identity))
                    with patch('control.directory', return_value=directory), \
                            patch('interaction.config', return_value={'thread_id': 'new'}), patch('interaction.actor'), \
                            patch('recovery.alive', return_value=True), patch('runner.process_identity', return_value='current'):
                        if allowed:
                            self.assertEqual(control.enable('synthetic hub rebind')['hub_thread'], 'new')
                        else:
                            with self.assertRaisesRegex(ValueError, 'stop the controller'):
                                control.enable('synthetic hub rebind')
