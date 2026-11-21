"""Bounded foreground fixture gate; not the frozen origin acceptance command."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
BASELINE = Path('OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/baseline.json')
with tempfile.TemporaryDirectory(prefix='ato-worker-stage-gate-') as temporary:
    os.environ.update(ATO_HOME=str(Path(temporary)/'ato'), CODEX_HOME=str(Path(temporary)/'native'),
                      PYTHONDONTWRITEBYTECODE='1')
    for key in ('CODEX_THREAD_ID','CTASK_SESSION_REF','CTASK_SESSION_TOKEN','CTASK_ENDPOINT_TOKEN'):
        os.environ.pop(key,None)
    suite=unittest.TestSuite()
    for name in ('test_worker_session_reuse','test_agent_registry_contract',
                 'test_agent_neutral_e2e','test_codex_session_adapter','test_host_recheck'):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    from test_runner import RunnerTests
    from test_workflow import WorkflowTests
    # The omitted inherited tests launch detached runners; this worker may run
    # only bounded foreground tests. No skip is presented as a passing check.
    omitted = {'test_detached_start_and_resume_after_stop','test_stop_propagates_to_child_and_resume_continues'}
    for cls in (RunnerTests,WorkflowTests):
        for name in unittest.defaultTestLoader.getTestCaseNames(cls):
            if name not in omitted:suite.addTest(cls(name))
    started=time.monotonic()
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    protected=json.loads(BASELINE.read_text())['protected_tests']
    drift=[name for name,expected in protected.items()
           if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected]
    evidence=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        skips=len(result.skipped),successful=result.wasSuccessful() and not drift,
        wall_seconds=time.monotonic()-started,protected_files=len(protected),protected_drift=drift,
        scope='foreground synthetic fixtures only; native qualification pending; detached tests omitted',
        omitted=sorted(omitted))
    (ROOT/'docs/execution-efficiency/evidence/worker-stage-tests.json').write_text(json.dumps(evidence,indent=2)+'\n')
    raise SystemExit(0 if evidence['successful'] and not result.skipped else 1)
