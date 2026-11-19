"""Explicit trusted bootstrap for exercising the public runtime CLI offline."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import agents
import fixture_adapter
fixture_adapter.register()
import agent_registry
agent_registry.set_runtime_entrypoint(str(Path(__file__).resolve()))
import runner

if __name__ == '__main__':
    raise SystemExit(runner.main())
