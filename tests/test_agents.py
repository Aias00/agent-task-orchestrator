import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import agents
import runner
import workflow

FAKE='''#!/usr/bin/env python3
import json,sys,os,time
from pathlib import Path
if '--version' in sys.argv:print('fixture-1.0');sys.exit()
prompt=sys.stdin.read()
if 'auth-fixture' in prompt:
 if '--json-schema' in sys.argv:print(json.dumps(dict(type='result',subtype='success',is_error=True,result='Not logged in · Please run /login')))
 else:print('No API key found for the selected model.',file=sys.stderr)
 sys.exit(1)
if 'sleep-fixture' in prompt:
 Path('entered').write_text('yes');time.sleep(30)
if 'Readonly fixture' in prompt:
 try:Path('must-not-write').write_text('oops')
 except PermissionError:pass
 else:sys.exit(4)
if 'Sandbox fixture' in prompt:
 for p in [Path(os.environ['OUTSIDE_TARGET']),Path('.codex/tasks/tampered'),Path('.git/tampered'),Path('escape/symlink-write')]:
  try:p.write_text('oops')
  except PermissionError:pass
  else:sys.exit(4)
if 'planner for an already' in prompt:
 value=dict(decision='stage',reason='Finish missing second file',tasks=[dict(id='second',prompt='create second',depends_on=[],covers=['second'])],retry_after_seconds=1)
elif 'block-fixture' in prompt:
 value=dict(status='blocked',summary='Need user decision',next_step='ask user',blocker='Choose a variant',retry_after_seconds=1)
else:
 file='second' if 'create second' in prompt else 'first'
 if 'Readonly fixture' not in prompt:Path(file).write_text('yes')
 value=dict(status='complete',summary='fixture done',next_step='',blocker='',retry_after_seconds=1)
if '--json-schema' in sys.argv:
 print(json.dumps(dict(type='result',subtype='success',is_error=False,session_id='fixture-session',structured_output=value)))
else:
 print(json.dumps(dict(type='session',id='fixture-session')))
 print(json.dumps(dict(type='message_end',message=dict(role='assistant',content=[dict(type='text',text=json.dumps(value))],stopReason='stop'))))
 print(json.dumps(dict(type='agent_end')))
 print(json.dumps(dict(type='agent_settled',aborted=False)))
'''
from legacy_support import LEGACY_CLI


class AdapterTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.repo=self.base/'repo';self.repo.mkdir()
  subprocess.run(['git','init','-q',str(self.repo)],check=True)
  self.fake=self.base/'agent';self.fake.write_text(FAKE);self.fake.chmod(0o755)
  self.env=dict(os.environ,CTASK_CLAUDE_BIN=str(self.fake),CTASK_PI_BIN=str(self.fake))
  self.proc=None
 def tearDown(self):
  if self.proc and self.proc.poll() is None:self.proc.terminate();self.proc.wait(timeout=8)
  self.tmp.cleanup()
 def create(self,kind='claude',prompt='create first',sandbox='workspace-write',**limits):
  p=self.repo/'input.json';p.write_text(json.dumps(dict(objective='Adapter fixture',memory=False,agent=dict(kind=kind),sandbox=sandbox,tasks=[dict(id='first',prompt=prompt,checks=[[sys.executable,'-c',"from pathlib import Path; assert Path('first').read_text()=='yes'"]])],limits=dict(runtime_seconds=30,attempt_seconds=10,check_seconds=2,max_attempts=1)|limits)))
  return runner.create(self.repo,p)
 def run_task(self,d):
  p=subprocess.run([sys.executable,str(LEGACY_CLI),'run',str(d)],env=self.env,capture_output=True,text=True,timeout=40)
  self.assertEqual(p.returncode,0,p.stderr)
  return runner.read(d/'state.json')
 def test_codex_argv_unchanged(self):
  argv=agents.command({'model':'existing'},self.repo,'a',self.repo/'schema',self.repo/'out')
  self.assertEqual(argv[-1],'-');self.assertIn('--output-schema',argv);self.assertIn('existing',argv)
 def test_invalid_configuration_refused(self):
  for cfg in [{'kind':'other'},{'kind':'pi','args':['--unsafe']},'pi',{'kind':'claude','model':'--unsafe'}]:
   with self.subTest(cfg=cfg),self.assertRaises(ValueError):agents.validate(cfg)
  p={'objective':'x','agent':None,'tasks':[{'id':'a','prompt':'a','checks':[['true']]}]}
  with self.assertRaises(ValueError):runner.validate(p)
 def test_native_model_not_sent_to_other_provider(self):
  p={'objective':'x','model':'codex-only','agent':{'kind':'pi'},'tasks':[{'id':'a','prompt':'a','checks':[['true']]}]}
  with self.assertRaises(ValueError):runner.validate(p)
 def test_claude_exec_and_independent_acceptance(self):
  d=self.create();s=self.run_task(d);self.assertEqual(s['status'],'completed')
  metadata=list((d/'checkpoints').glob('*.agent-runtime/result-metadata.json'))
  self.assertEqual(runner.read(metadata[0])['session_id'],'fixture-session')
 def test_pi_exec_and_independent_acceptance(self):
  d=self.create('pi');self.assertEqual(self.run_task(d)['status'],'completed')
 def test_claude_auth_failure_is_blocker_not_retryable_implementation(self):
  d=self.create('claude','auth-fixture',max_attempts=3);s=self.run_task(d)
  self.assertEqual(s['tasks']['first']['status'],'blocked');self.assertEqual(s['tasks']['first']['attempts'],1)
  self.assertIn('authentication',s['tasks']['first']['feedback'])
 def test_pi_auth_failure_is_blocker_not_retryable_implementation(self):
  d=self.create('pi','auth-fixture',max_attempts=3);s=self.run_task(d)
  self.assertEqual(s['tasks']['first']['status'],'blocked');self.assertEqual(s['tasks']['first']['attempts'],1)
  self.assertIn('authentication',s['tasks']['first']['feedback'])
 def test_worker_claim_does_not_override_check_failure(self):
  d=self.create('pi');plan=runner.read(d/'plan.json');plan['tasks'][0]['checks']=[['false']]
  runner.save(d/'plan.json',plan);s=runner.read(d/'state.json');s['plan_hash']=runner.digest(plan);runner.save(d/'state.json',s)
  self.assertEqual(self.run_task(d)['status'],'blocked')
 def test_blocked_result_preserved(self):
  d=self.create('claude','block-fixture');s=self.run_task(d)
  self.assertEqual(s['tasks']['first']['status'],'blocked');self.assertIn('Choose',s['tasks']['first']['feedback'])
 def test_workspace_blocks_outside_scheduler_git_and_symlink_writes(self):
  outside=self.base/'outside';outside.mkdir();(self.repo/'escape').symlink_to(outside,target_is_directory=True)
  self.env['OUTSIDE_TARGET']=str(outside/'outside-write')
  d=self.create('pi','Sandbox fixture');self.assertEqual(self.run_task(d)['status'],'completed')
  self.assertFalse((outside/'outside-write').exists());self.assertFalse((outside/'symlink-write').exists())
  self.assertFalse((self.repo/'.git/tampered').exists());self.assertFalse((self.repo/'.codex/tasks/tampered').exists())
 def test_readonly_planner_cannot_modify_repo(self):
  d=self.create('pi','Readonly fixture',sandbox='read-only');self.run_task(d)
  self.assertFalse((self.repo/'must-not-write').exists());self.assertFalse((self.repo/'first').exists())
  runtime=next((d/'checkpoints').glob('*.agent-runtime'))
  self.assertEqual(runner.read(runtime/'result-metadata.json')['kind'],'pi')
 def test_stop_reaps_bridge_and_cli(self):
  d=self.create('claude','sleep-fixture',attempt_seconds=25)
  self.proc=subprocess.Popen([sys.executable,str(LEGACY_CLI),'run',str(d)],env=self.env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  deadline=time.monotonic()+8
  while not (self.repo/'entered').exists() and time.monotonic()<deadline:time.sleep(.05)
  self.assertTrue((self.repo/'entered').exists());(d/'STOP').touch();self.proc.wait(timeout=8)
  self.assertEqual(runner.read(d/'state.json')['status'],'stopped')
 def test_timeout_bounded_and_resume_uses_new_checkpoint(self):
  d=self.create('pi','sleep-fixture',attempt_seconds=1)
  self.assertEqual(self.run_task(d)['status'],'blocked')
  self.fake.write_text(FAKE.replace("Path('entered').write_text('yes');time.sleep(30)","Path('entered').write_text('yes')"))
  result=subprocess.run([sys.executable,str(LEGACY_CLI),'run',str(d),'--retry-failed'],env=self.env,capture_output=True,text=True,timeout=15)
  self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(runner.read(d/'state.json')['status'],'completed')
  self.assertEqual(len(list((d/'checkpoints').glob('*.agent-runtime'))),2)
 def decode(self,kind,records):
  p=self.base/'protocol';p.write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in records)+'\n')
  return agents.decode(kind,p)
 def test_claude_rejects_missing_terminal_error_and_missing_schema(self):
  for record in [dict(type='assistant'),dict(type='result',subtype='success',is_error=True),dict(type='result',subtype='success',is_error=False,result='complete')]:
   with self.subTest(record=record),self.assertRaises(ValueError):self.decode('claude',[record])
 def test_pi_rejects_truncated_tool_response_and_abort(self):
  for reason in ['length','error','toolUse']:
   with self.subTest(reason=reason),self.assertRaises(ValueError):
    self.decode('pi',[dict(type='message_end',message=dict(role='assistant',stopReason=reason,content=[dict(type='text',text='{}')])),dict(type='agent_settled',aborted=False)])
  with self.assertRaises(ValueError):self.decode('pi',[dict(type='agent_settled',aborted=True)])
 def test_pi_unicode_separator_not_treated_as_protocol_boundary(self):
  result,_=self.decode('pi',[dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text=json.dumps({'summary':'hello\u2028world'},ensure_ascii=False))])),dict(type='agent_end')])
  self.assertEqual(result['summary'],'hello\u2028world')
 def test_schema_validation_rejects_bool_for_integer_and_extra_fields(self):
  value=dict(status='complete',summary='',next_step='',blocker='',retry_after_seconds=True)
  with self.assertRaises(ValueError):agents.validate_result(value,runner.SCHEMA)
  value['retry_after_seconds']=1;value['extra']='bad'
  with self.assertRaises(ValueError):agents.validate_result(value,runner.SCHEMA)
 def test_missing_cli_and_missing_sandbox_fail_closed(self):
  with patch.dict(os.environ,CTASK_PI_BIN='/does-not-exist'):
   with self.assertRaises(ValueError):agents.executable('pi')
  d=self.create('pi');runtime=d/'checkpoints'
  with patch('agents.Path.is_file',return_value=False):
   from argparse import Namespace
   with self.assertRaisesRegex(ValueError,'sandbox-exec'):
    agents.invoke(Namespace(directory=str(d),output=str(runtime/'a.json'),sandbox='workspace-write'))
 def test_workflow_propagates_agent_to_planner_and_generated_stage(self):
  p=self.repo/'workflow.json';checks=lambda name:[[sys.executable,'-c',f'from pathlib import Path; assert Path({name!r}).exists()']]
  contract=dict(objective='Both files',scope='Isolated repo',authorization='Fixture only',agent=dict(kind='pi'),memory=False,
   acceptance=[dict(id=k,description=k,checks=checks(k)) for k in ['first','second']],
   stages=[dict(id='first-stage',covers=['first'],plan=dict(objective='First file',tasks=[dict(id='first',prompt='create first',checks=checks('first'))]))],
   limits=dict(runtime_seconds=30,attempt_seconds=8,check_seconds=2,max_attempts=1))
  runner.save(p,contract);d=workflow.create(self.repo,p)
  s=self.run_task(d);self.assertEqual(s['status'],'completed');self.assertEqual(len(s['stages']),2)
  for stage in s['stages']:self.assertEqual(runner.read(Path(stage['run_dir'])/'plan.json')['agent']['kind'],'pi')
  self.assertTrue((self.repo/'second').exists())

if __name__=='__main__':unittest.main()
