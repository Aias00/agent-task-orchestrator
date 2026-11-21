"""Bounded foreground integration subset. Never an original full host gate."""
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
with tempfile.TemporaryDirectory(prefix='ato-integrated-foreground-') as folder:
    os.environ.update(ATO_HOME=str(Path(folder)/'ato'), CODEX_HOME=str(Path(folder)/'synthetic-native'), PYTHONDONTWRITEBYTECODE='1')
    for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
        os.environ.pop(key, None)
    names = ('test_runtime_selection', 'test_task_responsibility', 'test_worker_session_reuse',
        'test_incremental_checkpoint', 'test_incremental_verification', 'test_execution_efficiency_metrics',
        'test_efficiency_delivery', 'test_task_trace', 'test_task_summary', 'test_host_recheck',
        'test_linger_delivery')
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
    spec = importlib.util.spec_from_file_location('frozen_integrated_host', OPS/'host_behaviors.py')
    module = importlib.util.module_from_spec(spec)
    os.environ['ATO_CANDIDATE_ROOT'] = str(ROOT); spec.loader.exec_module(module)
    for cls in (module.Responsibility, module.Verification):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    from test_runner import RunnerTests
    from test_workflow import WorkflowTests
    omitted = {'test_detached_start_and_resume_after_stop', 'test_stop_propagates_to_child_and_resume_continues'}
    for cls in (RunnerTests, WorkflowTests):
        for name in unittest.defaultTestLoader.getTestCaseNames(cls):
            if name not in omitted: suite.addTest(cls(name))
    watched = [p for p in ROOT.glob('*.py')] + list((ROOT/'scripts').glob('*.py'))
    watched += [ROOT/'tests'/ (name+'.py') for name in names]
    source_before = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    baseline = json.loads((OPS/'baseline.json').read_text())
    drift = [n for n,h in baseline['protected_tests'].items() if hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=h]
    source_drift = [n for n,h in source_before.items() if hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=h]
    report = dict(source_files=source_before, source_drift=source_drift, tests=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), skips=len(result.skipped),
        successful=result.wasSuccessful() and not drift and not source_drift and not result.skipped,
        wall_seconds=time.monotonic()-started, protected_files=len(baseline['protected_tests']), protected_drift=drift,
        omitted=sorted(omitted), suites=list(names),
        external='frozen host_behaviors.Responsibility and Verification read-only imports; original acceptance.py not run',
        scope='selected bounded foreground temporary-repo fixtures; complete host discovery/native qualification pending')
    (ROOT/'docs/execution-efficiency/evidence/integration-tests.json').write_text(json.dumps(report,indent=2)+'\n')
    raise SystemExit(0 if report['successful'] else 1)
