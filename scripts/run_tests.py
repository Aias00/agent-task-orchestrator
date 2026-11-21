#!/usr/bin/env python3
"""Run offline regressions with an isolated local interaction/configuration home."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def preflight():
    if sys.version_info < (3, 12):
        print('Python 3.12 or later is required; run with .venv/bin/python or python3.12.', file=sys.stderr)
        return False
    missing = []
    if importlib.util.find_spec('acp') is None:
        missing.append('Python package agent-client-protocol (import acp)')
    if not (ROOT/'integrations/pi-acp/node_modules/pi-acp/dist/index.js').is_file():
        missing.append('integrations/pi-acp/node_modules/pi-acp/dist/index.js')
    if missing:
        print('Missing offline test dependencies:\n  '+'\n  '.join(missing), file=sys.stderr)
        print('From the repository root, install the pinned dependencies:\n'
              '  python3.12 -m venv .venv\n'
              '  .venv/bin/python -m pip install -r requirements-acp.lock\n'
              '  npm ci --ignore-scripts --no-audit --no-fund --prefix integrations/pi-acp\n'
              'Then run: .venv/bin/python scripts/run_tests.py', file=sys.stderr)
        return False
    return True


def main():
    if not preflight():
        return 2
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='ctask-test-config-') as folder:
        # Do not inherit the operator's advisory/enforced policy, hub or queues.
        os.environ['CODEX_HOME'] = str(Path(folder)/'config')
        os.environ['ATO_HOME'] = str(Path(folder)/'ato')
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN',
                    'CTASK_HANDOFF_READER'):
            os.environ.pop(key, None)
        suite = unittest.TestLoader().discover(str(ROOT/'tests'), pattern='test_*.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1

if __name__ == '__main__':
    raise SystemExit(main())
