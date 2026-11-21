"""Trusted offline external coordinator entry; never exposed by production CLI."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import agents
import agent_registry
import external_coordinator


class ExternalFixtureAdapter:
    kind = 'external_fixture_worker'
    capabilities = frozenset({'worker'})

    def command(self, request):
        if request.planner:
            raise AssertionError('external fixture must never invoke an implicit planner')
        return [sys.executable, str(Path(__file__).with_name('external_fixture_agent.py')), str(request.output)]


def register():
    if ExternalFixtureAdapter.kind not in agent_registry.kinds():
        agent_registry.register(ExternalFixtureAdapter())


register()
if __name__ == '__main__':
    sys.exit(external_coordinator.main())
