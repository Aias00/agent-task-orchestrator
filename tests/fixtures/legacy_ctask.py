"""Historical parser harness for offline kernel regression tests."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agent_registry
import runner
from legacy_cli import main

if __name__ == "__main__":
    agent_registry.set_runtime_entrypoint(__file__)
    raise SystemExit(main(runner))
