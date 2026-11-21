import os,sys,tempfile,unittest,json,time
from pathlib import Path
root=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(root),str(root/'tests')]
with tempfile.TemporaryDirectory(prefix='ato-runtime-stage-legacy-') as folder:
 os.environ.update(ATO_HOME=str(Path(folder)/'ato'),CODEX_HOME=str(Path(folder)/'native'),PYTHONDONTWRITEBYTECODE='1')
 for key in ('CODEX_THREAD_ID','CTASK_SESSION_REF','CTASK_SESSION_TOKEN','CTASK_ENDPOINT_TOKEN'):
  os.environ.pop(key,None)
 suite=unittest.TestSuite()
 # Every selected integration uses temporary repositories and synthetic workers.
 # Exclude inherited detached-start and native sandbox/socket tests in worker.
 names=['test_runtime_selection','test_agent_registry_contract','test_agent_neutral_e2e','test_host_recheck']
 for name in names:suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
 from test_runner import RunnerTests
 from test_workflow import WorkflowTests
 for cls,excluded in [(RunnerTests,{'test_detached_start_and_resume_after_stop'}),(WorkflowTests,{'test_stop_propagates_to_child_and_resume_continues'})]:
  for name in unittest.defaultTestLoader.getTestCaseNames(cls):
   if name not in excluded:suite.addTest(cls(name))
 started=time.time();r=unittest.TextTestRunner(verbosity=2).run(suite)
 result=dict(tests=r.testsRun,failures=len(r.failures),errors=len(r.errors),skips=len(r.skipped),successful=r.wasSuccessful(),wall_seconds=time.time()-started,scope='bounded foreground temporary-repo fixtures; inherited detached/native sandbox tests not run')
 (root/'docs/execution-efficiency/evidence').mkdir(exist_ok=True)
 (root/'docs/execution-efficiency/evidence/runtime-worker-tests.json').write_text(json.dumps(result,indent=2)+'\n')
 raise SystemExit(0 if r.wasSuccessful() and not r.skipped else 1)
