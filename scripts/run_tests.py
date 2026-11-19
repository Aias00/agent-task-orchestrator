#!/usr/bin/env python3
"""Run offline regressions with an isolated local interaction/configuration home."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='ctask-test-config-') as folder:
        # Do not inherit the operator's advisory/enforced policy, hub or queues.
        os.environ['CODEX_HOME'] = str(Path(folder)/'config')
        suite = unittest.TestLoader().discover(str(ROOT/'tests'), pattern='test_*.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1

if __name__ == '__main__':
    raise SystemExit(main())
