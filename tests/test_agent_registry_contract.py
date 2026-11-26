import sys
import tempfile
import unittest
from pathlib import Path

import agent_registry
from ato_adapters import agents


class FixtureAdapter:
    kind = 'fixture_registry_contract'
    capabilities = frozenset({'worker', 'planner'})

    def command(self, request):
        return [sys.executable, '-c',
                'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())',
                str(request.output)]


class RegistryContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        agent_registry.register(FixtureAdapter())

    def test_fourth_adapter_without_core_changes(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'result'
            command = agents.command({'agent': {'kind': FixtureAdapter.kind}}, folder,
                                     'attempt', 'schema', output)
            subprocess.run(command, input='fixture result', text=True, check=True)
            self.assertEqual(output.read_text(), 'fixture result')

    def test_unsupported_capability_before_effect(self):
        with self.assertRaises(ValueError):
            agent_registry.get(FixtureAdapter.kind, 'session_create')

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(ValueError):
            agents.validate({'kind': 'untrusted.module:Adapter'})

    def test_duplicate_registration_is_rejected(self):
        with self.assertRaises(ValueError):
            agent_registry.register(FixtureAdapter())
