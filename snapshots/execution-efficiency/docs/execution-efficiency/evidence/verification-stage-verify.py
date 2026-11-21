"""Bounded foreground verification stage gate; never runs native/provider code."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
OPS = Path('OPERATOR_EVIDENCE/ato-execution-efficiency-20261119')
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
with tempfile.TemporaryDirectory(prefix='ato-verification-gate-') as folder:
    os.environ.update(ATO_HOME=str(Path(folder)/'ato'), CODEX_HOME=str(Path(folder)/'native'), PYTHONDONTWRITEBYTECODE='1')
    for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
        os.environ.pop(key, None)
    suite = unittest.TestSuite()
    names = ('test_incremental_verification', 'test_host_recheck', 'test_incremental_checkpoint',
             'test_worker_session_reuse', 'test_runtime_selection')
    for name in names:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    spec = importlib.util.spec_from_file_location('frozen_verification_host', OPS/'host_behaviors.py')
    module = importlib.util.module_from_spec(spec)
    os.environ['ATO_CANDIDATE_ROOT'] = str(ROOT)
    spec.loader.exec_module(module)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(module.Verification))
    from test_runner import RunnerTests
    from test_workflow import WorkflowTests
    omitted = {'test_detached_start_and_resume_after_stop', 'test_stop_propagates_to_child_and_resume_continues'}
    for cls in (RunnerTests, WorkflowTests):
        for name in unittest.defaultTestLoader.getTestCaseNames(cls):
            if name not in omitted:
                suite.addTest(cls(name))
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    protected = json.loads((OPS/'baseline.json').read_text())['protected_tests']
    drift = [name for name, expected in protected.items() if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != expected]
    report = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skips=len(result.skipped),
        successful=result.wasSuccessful() and not drift, wall_seconds=time.monotonic()-started,
        protected_files=len(protected), protected_drift=drift, omitted=sorted(omitted), suites=list(names),
        external='frozen host_behaviors.Verification (read-only import; original acceptance.py not executed)',
        scope='bounded foreground temporary-repo fixtures; no native/provider or final full-task qualification')
    (ROOT/'docs/execution-efficiency/evidence/verification-stage-tests.json').write_text(json.dumps(report, indent=2)+'\n')
    raise SystemExit(0 if report['successful'] and not result.skipped else 1)
