"""Bounded foreground metrics/compatibility qualification, no real native calls."""
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
with tempfile.TemporaryDirectory(prefix='ato-metrics-stage-gate-') as folder:
    os.environ.update(ATO_HOME=str(Path(folder)/'ato'), CODEX_HOME=str(Path(folder)/'native'), PYTHONDONTWRITEBYTECODE='1')
    for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
        os.environ.pop(key, None)
    names = ('test_execution_efficiency_metrics', 'test_task_trace', 'test_task_summary',
        'test_incremental_verification', 'test_incremental_checkpoint', 'test_worker_session_reuse', 'test_runtime_selection')
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
    spec = importlib.util.spec_from_file_location('frozen_metrics_host', OPS/'host_behaviors.py')
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
    watched = ('execution_metrics.py', 'runner.py', 'worker_sessions.py', 'incremental_checkpoint.py',
        'task_trace.py', 'task_presentation.py', 'tests/test_execution_efficiency_metrics.py',
        'tests/fixtures/execution_efficiency_cli.py', 'scripts/benchmark_execution_efficiency_fixture.py',
        'scripts/verify_execution_efficiency_local.py')
    source_before = {n: hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in watched}
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    baseline = json.loads((OPS/'baseline.json').read_text())
    drift = [name for name, expected in baseline['protected_tests'].items() if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != expected]
    source_drift = [n for n, h in source_before.items() if hashlib.sha256((ROOT/n).read_bytes()).hexdigest() != h]
    report = dict(source_files=source_before, source_drift=source_drift, tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skips=len(result.skipped),
        successful=result.wasSuccessful() and not drift and not source_drift, wall_seconds=time.monotonic()-started,
        protected_files=len(baseline['protected_tests']), protected_drift=drift,
        omitted=sorted(omitted), suites=list(names),
        external='frozen host_behaviors.Verification imported read-only; original acceptance.py not executed',
        scope='bounded foreground temporary-repo fixtures only; no native/provider/full-task qualification')
    (ROOT/'docs/execution-efficiency/evidence/metrics-stage-tests.json').write_text(json.dumps(report, indent=2)+'\n')
    raise SystemExit(0 if report['successful'] and not result.skipped else 1)
