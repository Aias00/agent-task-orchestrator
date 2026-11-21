"""Runtime binding regressions. Only bounded foreground synthetic workers."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runner
import runtime_selection
import workflow

BOOTSTRAP = r'''import json,sys,os
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import agents,agent_registry,runner
MARKER=MARKER_VALUE
class Fixture:
 kind='runtime_fixture'
 capabilities=frozenset({'worker','planner','progress'})
 def command(self,request):
  return [sys.executable,str(ROOT/'fixture_agent.py'),str(request.output),
          'planner' if request.planner else 'worker',MARKER]
agent_registry.register(Fixture())
agent_registry.set_runtime_entrypoint(str(Path(__file__).resolve()))
# Resume/start are inspected at the exact spawn boundary, before any child is
# detached. A separate foreground _worker test executes the resulting route.
if os.environ.get('RUNTIME_CAPTURE_LAUNCH'):
 original=runner.subprocess.Popen
 def capture(argv,*args,**kwargs):
  if len(argv)>2 and argv[2]=='_worker':
   Path(os.environ['RUNTIME_CAPTURE_LAUNCH']).write_text(json.dumps(argv))
   raise OSError('fixture captured bounded launch; no detached worker')
  return original(argv,*args,**kwargs)
 runner.subprocess.Popen=capture
raise SystemExit(runner.main())
'''
AGENT = r'''import json,sys,os,time
from pathlib import Path
out,role,marker=sys.argv[1:]
prompt=sys.stdin.read()
with Path('fixture-calls.jsonl').open('a') as f:
 f.write(json.dumps(dict(role=role,marker=marker,script=__file__))+'\n')
if role=='planner':
 value=dict(decision='stage',reason='complete fixture artifact',retry_after_seconds=1,
  tasks=[dict(id='artifact',prompt='create artifact',depends_on=[],covers=['artifact'])])
else:
 if os.environ.get('RUNTIME_WORKER_GATE'):
  gate=Path(os.environ['RUNTIME_WORKER_GATE']);Path(str(gate)+'.entered').touch()
  until=time.monotonic()+8
  while not gate.exists() and time.monotonic()<until:time.sleep(.03)
  if not gate.exists():raise RuntimeError('bounded fixture gate expired')
 Path('artifact').write_text(marker)
 value=dict(status='complete',summary='fixture artifact',next_step='',blocker='',retry_after_seconds=1)
Path(out).write_text(json.dumps(value))
'''

class RuntimeSelectionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='ato-runtime-binding-')
  self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name).resolve();self.repo=self.base/'repo';self.repo.mkdir()
  subprocess.run(['git','init','-q',str(self.repo)],check=True)
  self.env=dict(os.environ,ATO_HOME=str(self.base/'ato'),CODEX_HOME=str(self.base/'native'),
                PYTHONDONTWRITEBYTECODE='1')
  for key in ('CODEX_THREAD_ID','CTASK_SESSION_REF','CTASK_SESSION_TOKEN','CTASK_ENDPOINT_TOKEN','PYTHONPATH'):
   self.env.pop(key,None)
  self.old=self.copy_runtime('old','OLD');self.new=self.copy_runtime('new','NEW')
  self.launcher=self.base/'default-ctask'
  self.switch(self.old)
  self.patch=patch.dict(os.environ,self.env,clear=True);self.patch.start();self.addCleanup(self.patch.stop)
 def copy_runtime(self,name,marker):
  root=self.base/name;root.mkdir()
  for p in ROOT.iterdir():
   if p.suffix in ('.py','.mjs','.lock'):shutil.copy2(p,root/p.name)
  for name in ('bin','scripts','tests/fixtures'):
   shutil.copytree(ROOT/name,root/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  folder=root/'integrations/pi-acp';folder.mkdir(parents=True)
  for p in (ROOT/'integrations/pi-acp').iterdir():
   if p.is_file():shutil.copy2(p,folder/p.name)
  (folder/'node_modules').symlink_to((ROOT/'integrations/pi-acp/node_modules').resolve(),target_is_directory=True)
  (root/'bootstrap.py').write_text(BOOTSTRAP.replace('MARKER_VALUE',repr(marker)))
  (root/'fixture_agent.py').write_text(AGENT)
  return root
 def switch(self,root):
  # Independent mutable fixture launcher, never the installed user launcher.
  self.launcher.write_text('#!/bin/sh\nexec "'+sys.executable+'" "'+str(root/'bootstrap.py')+'" "$@"\n')
  self.launcher.chmod(0o755)
 def cli(self,*args,ok=True,env=None):
  r=subprocess.run([str(self.launcher),*map(str,args)],env=env or self.env,capture_output=True,text=True,timeout=20)
  if ok:self.assertEqual(r.returncode,0,r.stderr+'\n'+r.stdout)
  return r
 def task(self):
  return dict(id='artifact',prompt='create artifact',depends_on=[],checks=[[sys.executable,'-c',
   "from pathlib import Path; assert Path('artifact').read_text() == 'OLD'"]])
 def create(self,total=False):
  plan=dict(objective='whole fixture objective',memory=False,agent=dict(kind='runtime_fixture'),
            limits=dict(runtime_seconds=30,attempt_seconds=5,check_seconds=3,max_attempts=1))
  if total:
   plan.update(scope='temporary files only',authorization='synthetic fixture only',
    host_handoff=dict(source='fixture authorization',instructions='frozen fixture checks only'),
    acceptance=[dict(id='artifact',description='old runtime artifact',checks=self.task()['checks'])])
  else:plan['tasks']=[self.task()]
  source=self.repo/'input.json';source.write_text(json.dumps(plan))
  return Path(self.cli('workflow-create' if total else 'create','--repo',self.repo,'--plan',source).stdout.strip())
 def read(self,d):return json.loads((d/runtime_selection.NAME).read_text())
 def load(self,d):
  # Read-only resolver can inspect a different bound runtime; it never imports it.
  return runtime_selection.load(d)
 def mutate(self,path,fn):
  data=json.loads(path.read_text());fn(data);path.write_text(json.dumps(data))
 def assert_refused(self,d,command='run'):
  before=(self.repo/'fixture-calls.jsonl').read_bytes() if (self.repo/'fixture-calls.jsonl').exists() else b''
  r=self.cli(command,d,ok=False);self.assertNotEqual(r.returncode,0,r.stdout)
  after=(self.repo/'fixture-calls.jsonl').read_bytes() if (self.repo/'fixture-calls.jsonl').exists() else b''
  self.assertEqual(before,after)
  return r
 def test_create_persists_auditable_exact_selection_outside_contract(self):
  d=self.create();record=self.load(d);selected=record['runtime']
  self.assertEqual(selected['runner'],str(self.old/'bootstrap.py'))
  self.assertEqual(selected['python'],str(Path(sys.executable).absolute()))
  self.assertEqual(len(selected['manifest_sha256']),64)
  self.assertNotIn('runtime_selection',json.loads((d/'plan.json').read_text()))
  self.assertEqual(record['binding']['project'],str(self.repo))
 def test_independent_default_switch_keeps_old_foreground_worker(self):
  d=self.create();before=self.read(d);self.switch(self.new)
  self.cli('run',d)
  self.assertEqual((self.repo/'artifact').read_text(),'OLD')
  self.assertEqual(self.read(d),before)
  events=[json.loads(x) for x in (d/'events.jsonl').read_text().splitlines()]
  self.assertTrue(any(e.get('argv',[None,None])[1:2]==[str(self.old/'fixture_agent.py')] for e in events))
 def test_future_creation_uses_new_default_without_rebinding_old(self):
  old=self.create();self.switch(self.new);new=self.create()
  self.assertEqual(self.load(old)['runtime']['root'],str(self.old))
  self.assertEqual(self.load(new)['runtime']['root'],str(self.new))
 def test_running_old_worker_survives_independent_default_switch(self):
  d=self.create();gate=self.base/'release';entered=Path(str(gate)+'.entered')
  process=subprocess.Popen([str(self.launcher),'run',str(d)],
   env=dict(self.env,RUNTIME_WORKER_GATE=str(gate)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  try:
   until=time.monotonic()+8
   while not entered.exists() and time.monotonic()<until and process.poll() is None:time.sleep(.03)
   self.assertTrue(entered.exists(),'foreground worker did not enter fixture gate')
   self.switch(self.new);gate.touch();stdout,stderr=process.communicate(timeout=12)
   self.assertEqual(process.returncode,0,stderr+stdout)
  finally:
   if process.poll() is None:process.terminate();process.communicate(timeout=5)
  self.assertEqual((self.repo/'artifact').read_text(),'OLD')
  self.assertEqual(json.loads((d/'state.json').read_text())['status'],'completed')
 def test_workflow_planner_stage_and_worker_inherit_old_runtime(self):
  d=self.create(total=True);self.switch(self.new);self.cli('run',d)
  state=json.loads((d/'state.json').read_text());self.assertEqual(state['status'],'completed')
  child=Path(state['stages'][0]['run_dir']);record=self.load(child)
  self.assertEqual(record['runtime'],self.load(d)['runtime'])
  self.assertEqual(record['parent']['directory'],str(d))
  calls=[json.loads(x) for x in (self.repo/'fixture-calls.jsonl').read_text().splitlines()]
  self.assertEqual([(r['role'],r['marker']) for r in calls],[('planner','OLD'),('worker','OLD')])
 def test_exact_worker_internal_entry_after_default_switch(self):
  d=self.create();self.switch(self.new);self.cli('_worker',d)
  self.assertEqual(json.loads((d/'state.json').read_text())['status'],'completed')
  self.assertEqual((self.repo/'artifact').read_text(),'OLD')
 def test_resume_selects_old_argv_before_bounded_spawn(self):
  d=self.create();self.switch(self.new);capture=self.base/'launch.json'
  r=self.cli('resume',d,env=dict(self.env,RUNTIME_CAPTURE_LAUNCH=str(capture)),ok=False)
  self.assertEqual(r.returncode,1);self.assertIn('captured bounded launch',r.stderr)
  self.assertEqual(json.loads(capture.read_text())[:4],
   [str(Path(sys.executable).absolute()),str(self.old/'bootstrap.py'),'_worker',str(d)])
 def test_status_after_switch_is_readonly_and_auditable(self):
  d=self.create();before={p.name:p.read_bytes() for p in d.iterdir() if p.is_file()}
  self.switch(self.new);state=json.loads(self.cli('status',d).stdout)
  self.assertEqual(state['runtime_binding']['runner'],str(self.old/'bootstrap.py'))
  self.assertEqual(before,{p.name:p.read_bytes() for p in d.iterdir() if p.is_file()})
 def test_host_checks_route_old_without_worker_or_planner(self):
  d=self.create(total=True);self.mutate(d/'state.json',lambda s:s.update(status='needs_host'))
  (self.repo/'artifact').write_text('OLD');self.switch(self.new)
  self.cli('host-checks',d,'--source','fixture host')
  state=json.loads((d/'state.json').read_text());self.assertEqual(state['status'],'completed')
  self.assertFalse((self.repo/'fixture-calls.jsonl').exists())
 def test_host_recheck_routes_bound_parent_and_child_without_new_attempt(self):
  d=self.create(total=True);self.cli('run',d)
  s=json.loads((d/'state.json').read_text());child=Path(s['stages'][0]['run_dir'])
  s.update(status='blocked',active_run=str(child));(d/'state.json').write_text(json.dumps(s))
  self.mutate(child/'state.json',lambda s:(s.update(status='blocked'),s['tasks']['artifact'].update(status='failed')))
  before=json.loads((child/'state.json').read_text())['tasks']['artifact']['attempts']
  calls=(self.repo/'fixture-calls.jsonl').read_bytes();self.switch(self.new)
  result=json.loads(self.cli('host-recheck',d,'--task','artifact','--source','fixture host').stdout)
  self.assertTrue(result['passed']);self.assertEqual(result['attempts'],before)
  self.assertEqual((self.repo/'fixture-calls.jsonl').read_bytes(),calls)
 def test_record_command_tampering_rejected_before_import(self):
  d=self.create();self.mutate(d/runtime_selection.NAME,lambda r:r['runtime'].update(runner=str(self.new/'bootstrap.py')))
  self.assert_refused(d)
 def test_removed_record_never_downgrades_to_legacy(self):
  d=self.create();(d/runtime_selection.NAME).unlink();self.assert_refused(d)
 def test_missing_control_receipt_fails_closed(self):
  d=self.create();runtime_selection._receipt(d).unlink();self.assert_refused(d)
 def test_wrong_project_rejected(self):
  d=self.create();self.mutate(d/'state.json',lambda s:s.update(repo=str(self.base)))
  self.assert_refused(d)
 def test_wrong_task_generation_rejected(self):
  d=self.create();self.mutate(d/'state.json',lambda s:s['runtime_selection'].update(generation='forged'))
  self.assert_refused(d)
 def test_contract_and_state_hash_rewrite_cannot_rebind(self):
  d=self.create();p=json.loads((d/'plan.json').read_text());p['objective']='replaced'
  (d/'plan.json').write_text(json.dumps(p))
  self.mutate(d/'state.json',lambda s:s.update(plan_hash=runner.digest(p)))
  self.assert_refused(d)
 def test_wrong_origin_actor_is_rejected(self):
  d=self.create(total=True)
  self.mutate(d/'origin.json',lambda s:s.update(origin_thread_id='forged-origin'))
  self.assert_refused(d)
 def test_selected_source_content_and_added_module_drift_rejected(self):
  for addition in (False,True):
   with self.subTest(addition=addition):
    d=self.create();p=self.old/('shadow_module.py' if addition else 'fixture_agent.py')
    before=p.read_bytes() if p.exists() else None;p.write_text('# drift\n')
    self.assert_refused(d)
    if before is None:p.unlink()
    else:p.write_bytes(before)
 def test_mode_drift_rejected(self):
  d=self.create();p=self.old/'fixture_agent.py';p.chmod(p.stat().st_mode ^ 0o100)
  self.assert_refused(d)
 def test_dependency_drift_rejected_without_mutating_inherited_dependency(self):
  deps=self.old/'integrations/pi-acp/node_modules';deps.unlink();deps.mkdir()
  fixture=deps/'fixture.js';fixture.write_text('immutable dependency')
  d=self.create();fixture.write_text('changed dependency');self.assert_refused(d)
 def test_interpreter_drift_rejected_without_touching_real_python(self):
  d=self.create();record=self.read(d)
  # An altered audit record cannot introduce even a legitimate other Python.
  record['runtime']['python']='/usr/bin/python3'
  (d/runtime_selection.NAME).write_text(json.dumps(record));self.assert_refused(d)
 def test_record_symlink_rejected(self):
  d=self.create();p=d/runtime_selection.NAME;copy=self.base/'copy.json';copy.write_bytes(p.read_bytes())
  p.unlink();p.symlink_to(copy);self.assert_refused(d)
 def test_changed_home_is_not_authority_or_legacy_fallback(self):
  d=self.create();r=self.cli('run',d,ok=False,env=dict(self.env,ATO_HOME=str(self.base/'wrong-home')))
  self.assertNotEqual(r.returncode,0);self.assertFalse((self.repo/'artifact').exists())
 def test_planted_receipt_in_new_home_is_not_authority(self):
  d=self.create();home=self.base/'forged-home';folder=home/'runtime-selections-v1';folder.mkdir(parents=True,mode=0o700)
  (folder/runtime_selection._receipt(d).name).write_bytes((d/runtime_selection.NAME).read_bytes())
  (folder/runtime_selection._receipt(d).name).chmod(0o600)
  r=self.cli('run',d,ok=False,env=dict(self.env,ATO_HOME=str(home)))
  self.assertNotEqual(r.returncode,0);self.assertIn('locator is not authority',r.stderr)
  self.assertFalse((self.repo/'fixture-calls.jsonl').exists())
 def test_interrupted_creation_receipt_cannot_become_legacy(self):
  source=self.repo/'unbound.json';source.write_text(json.dumps(dict(objective='fixture',memory=False,tasks=[self.task()])))
  d=runner.create(self.repo,source);original=runtime_selection._exclusive
  def interrupt(path,value):
   if Path(path)==d/runtime_selection.NAME:raise OSError('fixture crash before task record')
   return original(path,value)
  with patch.object(runtime_selection,'_exclusive',side_effect=interrupt),self.assertRaises(OSError):runtime_selection.pin(d)
  self.assertNotIn('runtime_selection',json.loads((d/'state.json').read_text()))
  with self.assertRaisesRegex(ValueError,'downgrade refused'):self.load(d)
 def test_child_parent_binding_tamper_rejected(self):
  d=self.create(total=True);self.cli('run',d)
  child=Path(json.loads((d/'state.json').read_text())['stages'][0]['run_dir'])
  self.mutate(child/'parent.json',lambda r:r.update(workflow_dir=str(self.base/'another')))
  self.assert_refused(child)
 def test_pin_is_create_only_and_never_rebinds(self):
  d=self.create();before=(d/runtime_selection.NAME).read_bytes()
  with self.assertRaises(ValueError):runtime_selection.pin(d)
  self.assertEqual(before,(d/runtime_selection.NAME).read_bytes())
 def test_delegation_status_and_dispatcher_argv_keep_task_runtime(self):
  d=self.create(total=True);delegation=self.repo/'.codex/tasks/delegations/fixture';delegation.mkdir(parents=True)
  (delegation/'delegation.json').write_text(json.dumps(dict(workflow_dir=str(d),repo=str(self.repo),state='accepted')))
  self.switch(self.new)
  result=json.loads(self.cli('delegation-status',delegation).stdout)
  self.assertEqual(result['workflow']['status'],'created')
  argv=runtime_selection.command(delegation,'_dispatcher',delegation)
  self.assertEqual(argv[1],str(self.old/'bootstrap.py'));self.assertEqual(argv[2],'_dispatcher')
 def test_wrong_loaded_python_api_runtime_refused(self):
  d=self.create()
  with self.assertRaisesRegex(ValueError,'wrong runtime'):runner.Runner(d)
  with self.assertRaisesRegex(ValueError,'wrong runtime'):__import__('agents').command({},d,'x',d/'schema',d/'out')
 def test_legacy_api_run_keeps_original_shape_and_readonly_load(self):
  source=self.repo/'legacy.json';source.write_text(json.dumps(dict(objective='legacy',memory=False,tasks=[self.task()])))
  d=runner.create(self.repo,source);before={p.name:p.read_bytes() for p in d.iterdir() if p.is_file()}
  self.assertIsNone(self.load(d));self.assertIsNone(runner.Runner(d).runtime_selection)
  self.assertNotIn('runtime_selection',json.loads((d/'state.json').read_text()))
  self.assertEqual(before,{p.name:p.read_bytes() for p in d.iterdir() if p.is_file()})
 def test_model_runtime_overrides_rejected_by_existing_schema(self):
  for key in ('runtime_selection','runner','python','import','command'):
   with self.subTest(key=key),self.assertRaises(ValueError):
    runner.validate(dict(objective='fixture',tasks=[self.task()],**{key:str(self.new/'bootstrap.py')}))
 def test_stop_survives_new_launcher_and_prevents_all_fixture_work(self):
  d=self.create();(d/'STOP').touch();self.switch(self.new);self.assert_refused(d)
  self.assertTrue((d/'STOP').exists());self.assertFalse((self.repo/'fixture-calls.jsonl').exists())
 def test_stop_can_still_fence_a_drifted_selected_runtime(self):
  d=self.create();(self.old/'fixture_agent.py').write_text('# selected source drift\n');self.switch(self.new)
  self.cli('stop',d)
  self.assertTrue((d/'STOP').exists());self.assertFalse((self.repo/'fixture-calls.jsonl').exists())

if __name__=='__main__':unittest.main()
