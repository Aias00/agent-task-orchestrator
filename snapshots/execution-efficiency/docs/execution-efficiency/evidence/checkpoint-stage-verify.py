"""Bounded foreground fixture gate; does not run origin-only acceptance/models."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
BASE=Path('OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/baseline.json')
with tempfile.TemporaryDirectory(prefix='ato-checkpoint-stage-gate-') as temporary:
    os.environ.update(ATO_HOME=str(Path(temporary)/'ato'),CODEX_HOME=str(Path(temporary)/'native'),PYTHONDONTWRITEBYTECODE='1')
    for key in ('CODEX_THREAD_ID','CTASK_SESSION_REF','CTASK_SESSION_TOKEN','CTASK_ENDPOINT_TOKEN'):
        os.environ.pop(key,None)
    suite=unittest.TestSuite()
    for name in ('test_incremental_checkpoint','test_worker_session_reuse','test_host_recheck','test_advisory_time','test_capacity_queue','test_linger_integration'):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    from test_runner import RunnerTests
    from test_workflow import WorkflowTests
    omitted={'test_detached_start_and_resume_after_stop','test_stop_propagates_to_child_and_resume_continues'}
    for cls in (RunnerTests,WorkflowTests):
        for name in unittest.defaultTestLoader.getTestCaseNames(cls):
            if name not in omitted:suite.addTest(cls(name))
    started=time.monotonic();result=unittest.TextTestRunner(verbosity=2).run(suite)
    protected=json.loads(BASE.read_text())['protected_tests']
    drift=[name for name,expected in protected.items() if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected]
    report=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped),
        successful=result.wasSuccessful() and not drift,wall_seconds=time.monotonic()-started,
        protected_files=len(protected),protected_drift=drift,
        scope='bounded foreground synthetic fixtures only; no native/provider qualification; two inherited detached tests omitted',omitted=sorted(omitted))
    (ROOT/'docs/execution-efficiency/evidence/checkpoint-stage-tests.json').write_text(json.dumps(report,indent=2)+'\n')
    raise SystemExit(0 if report['successful'] and not result.skipped else 1)
