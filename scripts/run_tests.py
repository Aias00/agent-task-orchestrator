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
    if any(arg != "--server" for arg in sys.argv[1:]):
        print("Usage: python3.12 scripts/run_tests.py [--server]", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="ato-v2-test-") as folder:
        os.environ["ATO_HOME"] = str(Path(folder) / "ato")
        os.environ["CODEX_HOME"] = str(Path(folder) / "codex")
        for key in ("CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE"):
            os.environ.pop(key, None)
        suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern="test_*.py")
        print(f"Core/service/journal discovery: {suite.countTestCases()} tests", flush=True)
        if "--server" in sys.argv[1:]:
            # Optional SDK tests are deliberately outside core discovery. Missing server
            # dependencies are import failures, never silent skips or package installation.
            server_suite = unittest.TestLoader().discover(str(ROOT / "tests/server"), pattern="test_*.py")
            print(f"Optional tests/server discovery: {server_suite.countTestCases()} tests", flush=True)
            if not server_suite.countTestCases():
                print("Server test discovery is empty.", file=sys.stderr)
                return 1
            suite.addTests(server_suite)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
