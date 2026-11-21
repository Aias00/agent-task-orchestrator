"""Real foreground subprocess protocol fixtures; no provider/native CLI calls.

The synthetic CLI writes its own exact index/rollout, changes an artifact in two
turns, and models cold/resume receipt faults. All state is in temporary repos.
"""
import copy
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agents
import runner
import worker_sessions
import workflow

# Synthetic kernel start identities keep protocol fixtures portable under a
# worker sandbox that denies ps. Production code has no fallback; the dedicated
# unavailable-identity test verifies refusal. Host acceptance also runs this suite.
FIXTURE_CLI = "import os,sys;sys.path.insert(0,sys.argv.pop(1));import runner;runner.process_identity=lambda pid: 'fixture-kernel-'+str(pid);raise SystemExit(runner.main())"

POLICY = dict(version=1, mode='task_bound_native')
EXECUTION = dict(version=2, max_continuations=6, max_waits=4, max_yields=3, max_no_progress=4)

FAKE = r'''#!/usr/bin/env python3
import json,os,sqlite3,sys,time,uuid
from pathlib import Path
args=sys.argv[1:];prompt=sys.stdin.read();home=Path(os.environ['CODEX_HOME']);home.mkdir(exist_ok=True)
repo=Path.cwd();fault=(repo/'fault').read_text() if (repo/'fault').exists() else ''
with (repo/'calls.jsonl').open('a') as f:f.write(json.dumps(args)+'\n')
warm='resume' in args
identifier=args[-2] if warm else str(uuid.uuid4())
if fault=='wrong_id' and warm:identifier=str(uuid.uuid4())
path=home/(identifier+'.jsonl')
source=args[args.index('--thread-source')+1] if not warm else json.loads(path.read_text().splitlines()[0])['payload']['source']
if fault=='wrong_source':source='user'
project=str(repo) if fault!='wrong_project' else str(repo.parent)
if not path.exists():
 path.write_text(json.dumps(dict(type='session_meta',payload=dict(id=identifier,cwd=project,source=source)))+'\n')
 with sqlite3.connect(home/'state_5.sqlite') as con:
  con.execute('create table if not exists threads(id text primary key,rollout_path text)');con.execute('insert into threads values(?,?)',(identifier,str(path)))
turn=str(uuid.uuid4());correlation=prompt.splitlines()[0].split()[-1]
mode=args[args.index('--sandbox')+1];sandbox=dict(type=mode)
if mode=='workspace-write':sandbox.update(writable_roots=[],network_access=False)
if fault=='wide_permissions':sandbox['network_access']=True
count=len([json.loads(l) for l in path.read_text().splitlines() if json.loads(l).get('payload',{}).get('type')=='task_started'])+1
context=dict(turn_id=turn,cwd=str(repo),approval_policy='never',sandbox_policy=sandbox,model='fixture-model',model_provider='fixture')
if fault=='changed_model' and warm:context['model']='other-model'
def emit(type,payload):
 with path.open('a') as f:f.write(json.dumps(dict(type=type,payload=payload))+'\n')
emit('event_msg',dict(type='task_started',turn_id=turn))
emit('turn_context',context)
emit('event_msg',dict(type='user_message',message=prompt if fault!='wrong_message' else 'unrelated'))
print(json.dumps(dict(type='thread.started',thread_id=identifier)),flush=True)
print(json.dumps(dict(type='turn.started')),flush=True)
if fault=='stderr':print('fixture diagnostic on stderr',file=sys.stderr,flush=True)
if fault=='duplicate_thread':print(json.dumps(dict(type='thread.started',thread_id=identifier)),flush=True)
(repo/'artifact.txt').write_text(str(count))
if fault=='sleep':
 (repo/'entered').write_text('yes');time.sleep(20)
if fault=='eof':sys.exit(0)
emit('event_msg',dict(type='task_complete',turn_id=turn if fault!='wrong_turn' else 'unrelated'))
status='continue' if count==1 else 'complete'
if fault=='always_complete':status='complete'
result=dict(status=status,summary='fixture artifact changed',next_step='second increment',blocker='',retry_after_seconds=1)
out=Path(args[args.index('-o')+1]);out.write_text(json.dumps(result) if fault!='bad_json' else '{')
print(json.dumps(dict(type='turn.completed',usage=dict(input_tokens=1,output_tokens=1))),flush=True)
if fault=='partial':
 with path.open('a') as f:f.write('{')
if fault=='change_config':(home/'config.toml').write_text('model="changed"\n')
'''


class WorkerSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-worker-session-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base/'repo';self.repo.mkdir()
        subprocess.run(['/usr/bin/git','init','-q',str(self.repo)],check=True)
        self.binary = self.base/'fixture-codex';self.binary.write_text(FAKE);self.binary.chmod(0o755)
        self.env = dict(os.environ, ATO_HOME=str(self.base/'ato'), CODEX_HOME=str(self.base/'native'),
                        CTASK_CODEX_BIN=str(self.binary), PYTHONDONTWRITEBYTECODE='1')
        for key in ('CODEX_THREAD_ID','CTASK_SESSION_REF','CTASK_SESSION_TOKEN','CTASK_ENDPOINT_TOKEN'):
            self.env.pop(key,None)
        self.patch = patch.dict(os.environ,self.env,clear=True);self.patch.start();self.addCleanup(self.patch.stop)
        self.owner_patch = patch.object(runner,'process_identity',side_effect=lambda pid:'fixture-kernel-'+str(pid))
        self.owner_patch.start();self.addCleanup(self.owner_patch.stop)
        self.task = dict(id='artifact',prompt='increment artifact twice in exact native context',depends_on=[],
            checks=[[sys.executable,'-c',"from pathlib import Path;assert Path('artifact.txt').read_text()=='2'"]])
        self.plan = dict(objective='Full fixture objective',memory=False,tasks=[self.task],
            worker_session_policy=POLICY,execution_policy=EXECUTION,
            limits=dict(runtime_seconds=30,attempt_seconds=4,check_seconds=3,max_attempts=2))

    def create(self, pin_runtime=False):
        path=self.repo/'plan-input.json';runner.save(path,self.plan)
        self.d=runner.create(self.repo,path,pin_runtime=pin_runtime)
        return self.d

    def run_cli(self,*args):
        result=subprocess.run([sys.executable,'-c',FIXTURE_CLI,str(ROOT),'run',str(self.d),*args],
            env=self.env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return runner.read(self.d/'state.json')

    def calls(self):
        path=self.repo/'calls.jsonl'
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def receipt(self):
        r=runner.Runner(self.d)
        return worker_sessions.load(r,self.task)

    def fault(self,name):
        (self.repo/'fault').write_text(name)

    def one_turn(self):
        self.create();r=runner.Runner(self.d)
        r.state.update(owner_pid=os.getpid(),owner_identity=runner.process_identity(os.getpid()))
        r.persist();r.attempt(self.task,'')
        self.assertEqual(r.state['tasks']['artifact']['status'],'pending',r.state)
        self.assertEqual(len(self.calls()),1)
        return r

    def test_cold_warm_native_session_and_two_exact_turns_artifact_independent_checks(self):
        self.create();state=self.run_cli();self.assertEqual(state['status'],'completed',state)
        receipt=self.receipt();self.assertEqual(len(receipt['turns']),2)
        self.assertEqual(len({t['native_id'] for t in receipt['turns']}),1)
        self.assertEqual(len({t['turn_id'] for t in receipt['turns']}),2)
        self.assertEqual(len({t['message_id'] for t in receipt['turns']}),2)
        self.assertNotIn('resume',self.calls()[0]);self.assertIn('resume',self.calls()[1])
        self.assertEqual(self.calls()[1][-2],receipt['native_id'])
        self.assertTrue(all('--last' not in c and '--dangerously-bypass-approvals-and-sandbox' not in c for c in self.calls()))
        self.assertEqual([c[c.index('--sandbox')+1] for c in self.calls()],['workspace-write']*2)
        self.assertFalse(state['tasks']['artifact']['worker_session']['process_reuse'])
        self.assertTrue(all(Path(p).is_file() for p in state['tasks']['artifact']['evidence']))
        self.assertEqual(state['tasks']['artifact']['execution_accounting']['failures'],0)

    def test_eof_is_unknown_never_replayed_even_host_checks_or_retry(self):
        self.fault('eof');self.create();state=self.run_cli();self.assertEqual(state['status'],'blocked',state)
        self.assertTrue(state['tasks']['artifact']['delivery_unknown'])
        self.run_cli('--retry-failed')
        r=runner.Runner(self.d);r.execute(host_checks=True)
        self.assertEqual(r.state['tasks']['artifact']['status'],'blocked')
        self.assertEqual(len(self.calls()),1)

    def test_wrong_resumed_native_id_quarantines(self):
        r=self.one_turn();self.fault('wrong_id');r.attempt(self.task,'')
        self.assertTrue(r.state['tasks']['artifact']['delivery_unknown'])
        self.assertEqual(self.receipt()['state'],'delivery_unknown')

    def test_config_change_refuses_before_second_native_write(self):
        r=self.one_turn();(self.base/'native/config.toml').write_text('model="changed"\n')
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),1)
        self.assertTrue(r.state['tasks']['artifact']['session_refused'])

    def test_effective_permission_widening_rejected(self):
        self.fault('wide_permissions');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['delivery_unknown']);self.assertEqual(len(self.calls()),1)

    def test_effective_model_changed_in_native_resume_rejected(self):
        r=self.one_turn();self.fault('changed_model');r.attempt(self.task,'')
        self.assertTrue(r.state['tasks']['artifact']['delivery_unknown'])

    def test_wrong_native_project_rejected(self):
        self.fault('wrong_project');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['delivery_unknown'])

    def test_origin_or_user_source_rejected(self):
        self.fault('wrong_source');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['delivery_unknown'])

    def test_wrong_message_correlation_rejected(self):
        self.fault('wrong_message');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['delivery_unknown'])

    def test_wrong_native_turn_completion_rejected(self):
        self.fault('wrong_turn');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['delivery_unknown'])

    def test_bad_result_json_does_not_hide_side_effect_or_replay(self):
        self.fault('bad_json');self.create();s=self.run_cli()
        self.assertTrue((self.repo/'artifact.txt').exists())
        self.assertTrue(s['tasks']['artifact']['delivery_unknown']);self.assertEqual(len(self.calls()),1)

    def test_partial_native_record_is_unknown(self):
        self.fault('partial');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['delivery_unknown'])

    def test_external_native_append_blocks_next_turn(self):
        r=self.one_turn();path=Path(self.receipt()['rollout_path'])
        with path.open('a') as f:f.write('{}\n')
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),1)
        self.assertTrue(r.state['tasks']['artifact']['session_refused'])

    def test_receipt_deleted_cannot_silently_reconstruct(self):
        r=self.one_turn();worker_sessions._path(r).unlink()
        with self.assertRaises(FileNotFoundError):worker_sessions.load(r,self.task)

    def test_stop_during_native_turn_kills_process_preserves_unknown(self):
        self.fault('sleep');self.create()
        proc=subprocess.Popen([sys.executable,'-c',FIXTURE_CLI,str(ROOT),'run',str(self.d)],env=self.env,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+8
            while not (self.repo/'entered').exists() and proc.poll() is None and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertTrue((self.repo/'entered').exists())
            pid=runner.read(self.d/'state.json')['active_process']['pid']
            (self.d/'STOP').touch();proc.communicate(timeout=8)
            state=runner.read(self.d/'state.json');self.assertEqual(state['status'],'stopped')
            self.assertTrue(state['tasks']['artifact']['delivery_unknown'])
            self.assertIsNone(state['active_process'])
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        finally:
            if proc.poll() is None:proc.kill();proc.communicate(timeout=3)

    def test_timeout_delivery_unknown_spends_real_failure_only_once(self):
        self.plan['limits']['attempt_seconds']=1;self.fault('sleep');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['delivery_unknown'])
        self.assertEqual(s['tasks']['artifact']['execution_accounting']['failures'],1)
        self.assertEqual(len(self.calls()),1)

    def test_unconfirmed_cleanup_never_allows_second_launch(self):
        r=self.one_turn();value=self.receipt();value['cleanup_confirmed']=False
        worker_sessions._save(r,self.task,value)
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),1)
        self.assertTrue(r.state['tasks']['artifact']['session_refused'])

    def test_unknown_kernel_owner_identity_refuses_without_native_write(self):
        self.create();r=runner.Runner(self.d)
        r.state.update(owner_pid=os.getpid(),owner_identity='fixture-kernel-'+str(os.getpid()))
        with patch.object(runner,'process_identity',return_value=None):r.attempt(self.task,'')
        self.assertEqual(len(self.calls()),0)
        self.assertTrue(r.state['tasks']['artifact']['session_refused'])

    def test_wrong_owner_cannot_send(self):
        r=self.one_turn();r.state['owner_pid']=999999
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),1)
        self.assertTrue(r.state['tasks']['artifact']['session_refused'])

    def test_workflow_copies_frozen_policy_and_checks_total_after_stage(self):
        check=self.task['checks']
        contract=dict(objective='Full workflow objective',scope='fixture only',authorization='fixture authority',
            memory=False,worker_session_policy=POLICY,execution_policy=EXECUTION,
            limits=self.plan['limits'],acceptance=[dict(id='artifact',description='two increments',checks=check)],
            stages=[dict(id='one',covers=['artifact'],plan=dict(objective='stage',tasks=[self.task]))])
        path=self.repo/'workflow-input.json';runner.save(path,contract)
        d=workflow.create(self.repo,path)
        w=workflow.Workflow(d);w.execute()
        self.assertEqual(w.state['status'],'completed',w.state)
        child=Path(w.state['stages'][0]['run_dir'])
        self.assertEqual(runner.read(child/'plan.json')['worker_session_policy'],POLICY)
        self.assertEqual(runner.read(child/'state.json')['status'],'completed')
        self.assertEqual(w.state['criteria']['artifact']['status'],'passed')
        self.assertEqual(len(self.calls()),2)

    def test_workflow_rejects_stage_permission_or_policy_override(self):
        contract=dict(objective='full',scope='fixture',authorization='fixture',worker_session_policy=POLICY,
            acceptance=[dict(id='artifact',description='x',checks=self.task['checks'])],
            stages=[dict(id='x',covers=['artifact'],plan=dict(objective='child',tasks=[self.task],
                worker_session_policy=dict(version=1,mode='other')))])
        with self.assertRaises(ValueError):workflow.validate(contract)

    def test_unknown_adapter_capability_is_not_success(self):
        self.plan['agent']=dict(kind='pi');self.create();s=self.run_cli()
        self.assertEqual(len(self.calls()),0);self.assertEqual(s['status'],'blocked',s)
        self.assertEqual(worker_sessions.capabilities('pi')['native_continuation'],'unsupported')

    def test_attempt_sequence_reset_is_refused_without_a_new_native_write(self):
        r=self.one_turn();r.state['tasks']['artifact']['attempt_sequence']=0
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),1)
        self.assertTrue(r.state['tasks']['artifact']['session_refused'])
        self.assertEqual(r.state['tasks']['artifact']['execution_accounting']['invocations'],1)

    def test_unsupported_capability_does_not_mint_invocation_or_failure(self):
        self.plan['agent']=dict(kind='pi');self.create();s=self.run_cli()
        ledger=s['tasks']['artifact']['execution_accounting']
        self.assertEqual((ledger['invocations'],ledger['failures']),(0,0))
        self.assertEqual(len(self.calls()),0)

    def test_host_checks_cannot_promote_an_unstarted_native_task(self):
        self.create();(self.repo/'artifact.txt').write_text('2')
        r=runner.Runner(self.d);r.execute(host_checks=True)
        self.assertEqual(r.state['tasks']['artifact']['status'],'blocked')
        self.assertEqual(len(self.calls()),0)

    def test_native_result_schema_cannot_be_replaced(self):
        self.create();runner.save(self.d/'result-schema.json',dict(properties={},required=[]))
        s=self.run_cli();self.assertTrue(s['tasks']['artifact']['session_refused'])
        self.assertEqual(len(self.calls()),0)

    def test_stderr_diagnostics_are_not_native_json_records(self):
        self.fault('stderr');self.create();s=self.run_cli()
        self.assertEqual(s['status'],'completed',s)
        logs=list((self.d/'evidence').glob('*.stderr.log'))
        self.assertEqual(len(logs),2)
        self.assertTrue(all('fixture diagnostic' in p.read_text() for p in logs))

    def test_duplicate_native_thread_receipt_is_quarantined(self):
        self.fault('duplicate_thread');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['delivery_unknown'])
        self.assertEqual(len(self.calls()),1)

    def test_stop_before_next_turn_preserves_native_generation_and_no_write(self):
        r=self.one_turn();(self.d/'STOP').touch()
        with self.assertRaises(runner.Halt):r.attempt(self.task,'')
        self.assertEqual(len(self.calls()),1)
        self.assertEqual(self.receipt()['generation'],1)
        self.assertEqual(r.state['tasks']['artifact']['execution_accounting']['invocations'],1)

    def test_wrong_private_contract_binding_refuses_next_native_write(self):
        r=self.one_turn();value=self.receipt();value['binding']['contract']='other-contract'
        worker_sessions._save(r,self.task,value);r.attempt(self.task,'')
        self.assertEqual(len(self.calls()),1)
        self.assertTrue(r.state['tasks']['artifact']['session_refused'])

    def test_private_receipt_mode_change_is_rejected(self):
        r=self.one_turn();path=worker_sessions._path(r);path.chmod(0o644)
        with self.assertRaises(ValueError):worker_sessions.load(r,self.task)
        path.chmod(0o600)

    def test_independent_worker_tasks_never_share_native_identity(self):
        self.plan['tasks'].append(dict(self.task,id='second'))
        self.create();s=self.run_cli();self.assertEqual(s['status'],'completed',s)
        r=runner.Runner(self.d)
        first=worker_sessions.load(r,self.task)
        second=worker_sessions.load(r,dict(id='second'))
        self.assertNotEqual(first['native_id'],second['native_id'])
        self.assertNotEqual(first['source'],second['source'])
        self.assertEqual(len(self.calls()),4)

    def test_warm_prompt_uses_objective_and_increment_without_repeated_memory(self):
        r=self.one_turn()
        text=r.worker_prompt(self.task,'SYNTHETIC_MEMORY_NOT_REPEATED')
        self.assertIn(self.plan['objective'],text)
        self.assertIn(self.task['prompt'],text)
        self.assertNotIn('SYNTHETIC_MEMORY_NOT_REPEATED',text)
        self.assertNotIn('Predecessors:',text)

    def test_refused_eligibility_does_not_turn_into_unknown_delivery_on_retry(self):
        self.plan['agent']=dict(kind='pi');self.create();s=self.run_cli()
        self.assertTrue(s['tasks']['artifact']['session_refused'])
        s=self.run_cli('--retry-failed')
        self.assertFalse(s['tasks']['artifact'].get('delivery_unknown',False))
        self.assertEqual(s['tasks']['artifact']['execution_accounting']['invocations'],0)

    def test_host_acceptance_rejects_changed_native_file_after_terminal_receipt(self):
        r=self.one_turn();self.assertTrue(worker_sessions.host_acceptable(r,self.task))
        with Path(self.receipt()['rollout_path']).open('a') as f:f.write('{}\n')
        self.assertFalse(worker_sessions.host_acceptable(r,self.task))

    def test_pinned_runtime_identity_survives_both_native_turns(self):
        self.create(pin_runtime=True);s=self.run_cli()
        self.assertEqual(s['status'],'completed',s)
        import runtime_selection
        selected=runtime_selection.load(self.d)
        self.assertEqual(self.receipt()['binding']['runtime'],selected['runtime']['manifest_sha256'])
        self.assertEqual(len(self.receipt()['turns']),2)

    def test_policy_strict_versioned_validation(self):
        for value in (None,{},dict(version=True,mode='task_bound_native'),dict(version=2,mode='task_bound_native'),
            dict(version=1,mode='task_bound_native',native_id='model-chosen')):
            with self.subTest(value=value),self.assertRaises(ValueError):worker_sessions.validate(value)

    def test_legacy_mode_keeps_cold_adapter_and_no_worker_receipt(self):
        self.plan.pop('worker_session_policy');self.create()
        argv=agents.command(self.plan,self.d,'fixture',self.d/'result-schema.json',self.d/'checkpoints/out.json')
        self.assertNotIn('resume',argv);self.assertNotIn('--thread-source',argv)
        self.assertFalse(worker_sessions._path(runner.Runner(self.d)).exists())


if __name__=='__main__':unittest.main()
