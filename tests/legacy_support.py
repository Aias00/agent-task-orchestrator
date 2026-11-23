"""Explicit bootstrap used by offline regressions of the retained kernel."""
from pathlib import Path
from unittest.mock import patch

import agent_registry


LEGACY_CLI = Path(__file__).resolve().parent / 'fixtures/legacy_ctask.py'


def legacy_runtime():
    return patch.object(agent_registry, '_runtime_entrypoint', str(LEGACY_CLI))
