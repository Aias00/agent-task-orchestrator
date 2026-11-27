#!/usr/bin/env python3
"""Run fast isolated ATO v2 offline test suite."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if sys.version_info < (3, 12):
        print("Python 3.12+ is required.", file=sys.stderr)
        return 2
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="ato-v2-test-") as folder:
        os.environ["ATO_HOME"] = str(Path(folder) / "ato")
        os.environ["CODEX_HOME"] = str(Path(folder) / "codex")
        for key in ("CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE"):
            os.environ.pop(key, None)
        suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
