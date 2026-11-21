"""Trusted test-only CLI plus synthetic ACP transport; no model/provider."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# This reviewed fixture installs its profile in code, never through task/env fields.
import acpx_fixture_cli
import external_coordinator

if __name__ == '__main__':
    sys.exit(external_coordinator.main())
