"""Bounded foreground temp-repository tests; no real model/native CLI calls.

The installed CLI is never invoked. The executable below is an explicit synthetic
protocol fixture, and kernel identities are supplied only to fixture Runner code.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
import execution_accounting as accounting
import incremental_checkpoint as cp
import runner
import worker_sessions
import workflow
from test_worker_session_reuse import FAKE, FIXTURE_CLI, POLICY, EXECUTION

CHECKPOINT = dict(version=1, closing_margin_seconds=2, interval_seconds=1)
# Keep native turns real within the synthetic native index/rollout. A cold
# reconstruction uses the workspace's increment rather than a reset native ID.
FIXTURE = FAKE.replace("count=len([json.loads(l) for l in path.read_text().splitlines() if json.loads(l).get('payload',{}).get('type')=='task_started'])+1",
    "count=len((repo/'calls.jsonl').read_text().splitlines())")
FIXTURE = FIXTURE.replace("if fault=='sleep':", "if fault=='cooperative':time.sleep(1.2)\nif fault=='sleep':")
FIXTURE = FIXTURE.replace("if fault=='always_complete':status='complete'", "if fault=='always_complete':status='complete'\nif fault=='bad_outcome':status='invented'\nif fault=='wait':status='waiting'")
FIXTURE = FIXTURE.replace("(repo/'artifact.txt').write_text(str(count))", "(repo/'artifact.txt').write_text('1' if fault=='no_progress' else str(count))")
FIXTURE = FIXTURE.replace("if fault=='wait':status='waiting'", "if fault=='wait':status='waiting'\nif fault=='no_progress':status='continue'")


class IncrementalCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-checkpoint-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve();self.repo=self.base/'repo';self.repo.mkdir()
        subprocess.run(['/usr/bin/git','init','-q',str(self.repo)],check=True)
        self.binary=self.base/'fixture-codex';self.binary.write_text(FIXTURE);self.binary.chmod(0o755)
        self.env=dict(os.environ,ATO_HOME=str(self.base/'ato'),CODEX_HOME=str(self.base/'native'),
            CTASK_CODEX_BIN=str(self.binary),PYTHONDONTWRITEBYTECODE='1')
        for k in ('CODEX_THREAD_ID','CTASK_SESSION_REF','CTASK_SESSION_TOKEN','CTASK_ENDPOINT_TOKEN'):
            self.env.pop(k,None)
        envpatch=patch.dict(os.environ,self.env,clear=True);envpatch.start();self.addCleanup(envpatch.stop)
        ownerpatch=patch.object(runner,'process_identity',side_effect=lambda pid:'fixture-kernel-'+str(pid))
        ownerpatch.start();self.addCleanup(ownerpatch.stop)
        self.task=dict(id='artifact',prompt='Increment artifact twice; all activity is a synthetic fixture.',depends_on=[],
            checks=[[sys.executable,'-c',"from pathlib import Path;assert Path('artifact.txt').read_text()=='2'"]])
        self.plan=dict(objective='Exact complete authorized fixture objective',memory=False,tasks=[self.task],
            execution_policy=copy.deepcopy(EXECUTION),worker_session_policy=copy.deepcopy(POLICY),
            checkpoint_policy=copy.deepcopy(CHECKPOINT),limits=dict(runtime_seconds=30,attempt_seconds=4,check_seconds=3,max_attempts=2))

    def create(self, owned=True):
        p=self.repo/'plan-input.json';runner.save(p,self.plan);self.d=runner.create(self.repo,p)
        return self.owned() if owned else runner.Runner(self.d)

    def owned(self):
        r=runner.Runner(self.d)
        r.state.update(owner_pid=os.getpid(),owner_identity=runner.process_identity(os.getpid()))
        r.persist();return r

    def one(self):
        r=self.create();r.attempt(self.task,'');self.assertEqual(r.state['tasks']['artifact']['status'],'pending',r.state)
        return r

    def row(self,r):return r.state['tasks']['artifact']
    def entry(self,r):return cp._head(r)['tasks']['artifact']
    def calls(self):
        p=self.repo/'calls.jsonl';return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    def fault(self,name):(self.repo/'fault').write_text(name)
    def last(self,r):
        ref=self.entry(r)['last'];return json.loads((cp._root(r)/ref['file']).read_text())
    def cli(self,*args):
        result=subprocess.run([sys.executable,'-c',FIXTURE_CLI,str(ROOT),'run',str(self.d),*args],
            env=self.env,capture_output=True,text=True,timeout=25)
        self.assertEqual(result.returncode,0,result.stderr);return runner.read(self.d/'state.json')

    def test_real_runner_two_turns_terminal_proof_and_history_not_reset(self):
        self.create(owned=False);state=self.cli();row=state['tasks']['artifact']
        self.assertEqual(state['status'],'completed',state)
        ledger=accounting.ledger(row);self.assertEqual((ledger['invocations'],ledger['continuations'],ledger['failures']),(2,1,0))
        self.assertEqual([h['outcome'] for h in ledger['history']],['continue','complete'])
        self.assertIn('resume',self.calls()[1]);self.assertEqual((self.repo/'artifact.txt').read_text(),'2')
        r=self.owned();saved=cp.preflight(r,self.task)
        self.assertEqual(saved['goal'],self.plan['objective']);self.assertTrue(saved['cleanup_confirmed'])
        self.assertEqual(saved['native']['turns'],2)
        self.assertEqual(saved['results'][0]['status'],'passed')

    def test_private_atomic_checkpoint_contains_hashes_not_raw_prompts_or_logs(self):
        r=self.one();saved=self.last(r);root=cp._root(r)
        self.assertEqual(root.stat().st_mode & 0o777,0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir()))
        self.assertIn('artifact.txt',saved['source']['files'])
        raw=json.dumps(saved);self.assertNotIn(self.task['prompt'],raw);self.assertNotIn('Traceback',raw)
        self.assertNotIn('worker_output',saved);self.assertIsNotNone(cp.preflight(r,self.task))

    def test_periodic_running_snapshot_is_nonresumable_and_terminal_follows(self):
        self.fault('cooperative');r=self.one()
        records=[json.loads(p.read_text()) for p in cp._root(r).glob('artifact-*.json')]
        running=[p for p in records if p['phase']=='running'];self.assertTrue(running)
        self.assertTrue(all(not p['resumable'] and not p['cleanup_confirmed'] for p in running))
        self.assertTrue(self.last(r)['resumable']);self.assertGreater(self.last(r)['sequence'],running[0]['sequence'])

    def test_crash_before_record_write_keeps_pending_not_summary_recovery(self):
        r=self.one();original=cp._private_save
        def crash(path,value):
            if Path(path).name.startswith('artifact-'):raise OSError('fixture crash before record')
            return original(path,value)
        with patch.object(cp,'_private_save',side_effect=crash),self.assertRaises(OSError):
            cp.publish(r,self.task,'terminal',output=Path(self.last(r)['output']['path']))
        self.assertIsNotNone(self.entry(r)['pending'])
        with self.assertRaisesRegex(ValueError,'publication'):cp.preflight(r,self.task)
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),1);self.assertTrue(self.row(r)['checkpoint_refused'])

    def test_crash_after_record_before_head_commit_cannot_promote_or_replay(self):
        r=self.one();original=cp._private_save;head_calls=[]
        def crash(path,value):
            if Path(path).name=='head.json':
                head_calls.append(1)
                if len(head_calls)==2:raise OSError('fixture crash after record')
            return original(path,value)
        with patch.object(cp,'_private_save',side_effect=crash),self.assertRaises(OSError):
            cp.publish(r,self.task,'terminal',output=Path(self.last(r)['output']['path']))
        self.assertTrue((cp._root(r)/self.entry(r)['pending']['file']).is_file())
        with self.assertRaises(ValueError):cp.preflight(r,self.task)

    def test_partial_json_fails_closed(self):
        r=self.one();ref=self.entry(r)['last'];(cp._root(r)/ref['file']).write_text('{')
        with self.assertRaises(ValueError):cp.preflight(r,self.task)
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),1)

    def test_checkpoint_replay_row_pointer_rejected(self):
        r=self.one();old=copy.deepcopy(self.row(r)['incremental_checkpoint']);r.attempt(self.task,'')
        self.row(r)['incremental_checkpoint']=old
        with self.assertRaisesRegex(ValueError,'replay'):cp.preflight(r,self.task)
        self.assertEqual(accounting.ledger(self.row(r))['invocations'],2)

    def test_stale_generation_and_wrong_project_are_rejected(self):
        r=self.one();r.state['repo']=str(self.base)
        with self.assertRaises(ValueError):cp.preflight(r,self.task)
        r.state['repo']=str(self.repo);self.row(r)['attempt_sequence']+=1
        with self.assertRaises(ValueError):cp.preflight(r,self.task)

    def test_forged_record_digest_cannot_replace_host_proof(self):
        r=self.one();ref=self.entry(r)['last'];p=cp._root(r)/ref['file'];value=json.loads(p.read_text())
        value['results'][0]['status']='passed';p.write_text(json.dumps(value))
        with self.assertRaises(ValueError):cp.preflight(r,self.task)

    def test_file_bytes_mode_deletion_and_symlink_invalidate(self):
        for change in ('bytes','mode','delete','symlink'):
            with self.subTest(change=change):
                # Restore exactly between independent preflight observations.
                r=self.one() if not hasattr(self,'d') else self.owned()
                p=self.repo/'artifact.txt';original=p.read_bytes();mode=p.stat().st_mode & 0o777
                if change=='bytes':p.write_text('changed')
                elif change=='mode':p.chmod(0o755 if mode!=0o755 else 0o644)
                elif change=='delete':p.unlink()
                else:p.unlink();p.symlink_to(self.binary)
                with self.assertRaises(ValueError):cp.preflight(r,self.task)
                if p.is_symlink():p.unlink()
                p.write_bytes(original);p.chmod(mode)

    def test_old_check_log_and_worker_output_invalidate_independently(self):
        r=self.one();saved=self.last(r)
        for path in (Path(saved['results'][0]['receipt']['evidence']),Path(saved['output']['path'])):
            before=path.read_bytes();path.write_bytes(before+b'forged')
            with self.assertRaises(ValueError):cp.preflight(r,self.task)
            path.write_bytes(before)
        self.assertIsNotNone(cp.preflight(r,self.task))

    def test_host_pass_input_fingerprint_changes_reject_even_unchanged_summary(self):
        r=self.one();r.attempt(self.task,'');self.assertEqual(self.last(r)['results'][0]['status'],'passed')
        (self.repo/'new-check-input.py').write_text('changed = True\n')
        with self.assertRaisesRegex(ValueError,'workspace'):cp.preflight(r,self.task)

    def test_continue_is_not_failure_no_summary_only_progress(self):
        r=self.one();ledger=accounting.ledger(self.row(r))
        self.assertEqual((ledger['invocations'],ledger['continuations'],ledger['failures'],ledger['no_progress']),(1,1,0,0))
        self.assertEqual(self.last(r)['current_step']['outcome'],'continue')
        self.assertEqual(self.last(r)['results'][0]['status'],'failed')
        self.row(r)['summary']='arbitrary summary';self.assertIsNotNone(cp.preflight(r,self.task))

    def test_bad_outcome_json_preserves_files_unknown_and_failure_once(self):
        self.fault('bad_outcome');self.create(owned=False);state=self.cli();row=state['tasks']['artifact']
        self.assertTrue((self.repo/'artifact.txt').exists());self.assertTrue(row['delivery_unknown'])
        self.assertEqual(accounting.ledger(row)['failures'],1);self.cli('--retry-failed');self.assertEqual(len(self.calls()),1)

    def test_hard_timeout_unknown_not_normal_continue_cleanup_before_handoff(self):
        self.plan['limits']['attempt_seconds']=3;self.plan['checkpoint_policy']['closing_margin_seconds']=1
        self.fault('sleep');self.create(owned=False);state=self.cli();row=state['tasks']['artifact']
        self.assertIsNone(state['active_process']);self.assertTrue(row['delivery_unknown'])
        self.assertEqual(accounting.ledger(row)['failures'],1)
        self.assertEqual(accounting.ledger(row)['history'][0]['outcome'],'timeout')
        self.cli('--retry-failed');self.assertEqual(len(self.calls()),1)
        self.assertTrue(any(json.loads(p.read_text())['phase']=='running' for p in cp._root(self.owned()).glob('artifact-*.json')))

    def test_closing_margin_prompt_each_invocation_no_steer_or_extra_wait(self):
        r=self.one();r.attempt(self.task,'')
        inputs=list((self.d/'checkpoints').glob('artifact-attempt-*.input.txt'));self.assertEqual(len(inputs),2)
        for p in inputs:
            prompt=p.read_text();self.assertIn('by 2s after invocation start',prompt);self.assertIn('No checkpoint.request/steer',prompt)
        self.assertIn('Host-validated checkpoint',inputs[-1].read_text() if 'Host-validated checkpoint' in inputs[-1].read_text() else inputs[0].read_text())
        self.assertEqual(len(self.calls()),2)

    def test_explicit_new_id_reconstruction_restores_files_goal_and_full_counters(self):
        self.plan['worker_session_policy']=dict(version=2,mode='checkpoint_reconstruction')
        r=self.one();first=worker_sessions.load(r,self.task);r=self.owned();r.attempt(self.task,'')
        last=worker_sessions.load(r,self.task)
        self.assertNotEqual(first['native_id'],last['native_id']);self.assertEqual(len(last['ancestors']),1)
        self.assertEqual(len(last['turns']),2);self.assertTrue(all('resume' not in c for c in self.calls()))
        self.assertEqual(self.row(r)['status'],'completed');saved=cp.preflight(r,self.task)
        self.assertEqual(saved['goal'],self.plan['objective']);self.assertEqual(saved['ledger']['invocations'],2)
        self.assertEqual(saved['ledger']['continuations'],1);self.assertEqual(saved['ledger']['failures'],0)
        self.assertEqual(saved['native']['mode'],'checkpoint_reconstruction')

    def test_reconstruction_never_escapes_unknown_delivery(self):
        self.plan['worker_session_policy']=dict(version=2,mode='checkpoint_reconstruction')
        self.fault('eof');self.create(owned=False);self.cli();self.cli('--retry-failed');self.assertEqual(len(self.calls()),1)

    def test_profile_and_permission_change_rejected_without_dispatch(self):
        r=self.one();(self.base/'native/config.toml').write_text('model="changed"\n')
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),1)
        self.assertTrue(self.row(r)['checkpoint_refused']);self.assertEqual(accounting.ledger(self.row(r))['failures'],0)

    def test_stop_owner_and_cleanup_barriers(self):
        r=self.one();(self.d/'STOP').touch()
        with self.assertRaises(runner.Halt):cp.preflight(r,self.task)
        (self.d/'STOP').unlink();r.state['owner_identity']='wrong'
        with self.assertRaises(ValueError):cp.preflight(r,self.task)
        r.state['owner_identity']=runner.process_identity(os.getpid());r.state['active_process']=dict(pid=123)
        with self.assertRaises(ValueError):cp.preflight(r,self.task)

    def test_ledger_counter_or_history_reset_is_not_reconstructed(self):
        r=self.one();ledger=accounting.ledger(self.row(r));ledger['failures']=1
        with self.assertRaisesRegex(ValueError,'counters'):cp.preflight(r,self.task)
        ledger['failures']=0;ledger['history'].clear()
        with self.assertRaises(ValueError):cp.preflight(r,self.task)

    def test_missing_private_head_and_wrong_mode_fail_closed(self):
        r=self.one();path=cp._root(r)/'head.json';path.chmod(0o644)
        with self.assertRaises(ValueError):cp.preflight(r,self.task)
        path.unlink()
        with self.assertRaises(OSError):cp.preflight(r,self.task)

    def test_expired_terminal_receipt_cannot_authorize_a_new_turn(self):
        r=self.one();saved=self.last(r)
        with patch.object(cp.time,'time',return_value=saved['observed_at']+86401):
            with self.assertRaisesRegex(ValueError,'expired'):cp.preflight(r,self.task)
        self.assertEqual(len(self.calls()),1)

    def test_earlier_record_retained_and_corruption_rejected(self):
        r=self.one();old=self.entry(r)['last'];r.attempt(self.task,'')
        old_path=cp._root(r)/old['file'];self.assertTrue(old_path.is_file())
        old_path.unlink()
        with self.assertRaises(OSError):cp.preflight(r,self.task)
        self.assertEqual(accounting.ledger(self.row(r))['invocations'],2)

    def test_periodic_snapshot_cap_does_not_extend_native_timeout(self):
        r=self.one();r._checkpoint_monitor=dict(task=self.task,started=time.monotonic()-10,
            invocation=1,published=cp.MAX_PERIODIC,next=0,fingerprint=None,closing=False,close_after=2)
        before=self.entry(r)['sequence'];cp.poll(r,self.task)
        self.assertTrue(r._checkpoint_monitor['closing']);self.assertEqual(self.entry(r)['sequence'],before)
        events=[json.loads(line) for line in (self.d/'events.jsonl').read_text().splitlines()]
        event=next(e for e in events if e['event']=='checkpoint_closing_margin')
        self.assertEqual(event['delivery'],'prompt_only');self.assertFalse(event['request_sent'])

    def test_summary_only_continuations_hit_no_progress_bound_without_failure_fee(self):
        self.fault('no_progress');self.plan['execution_policy']['max_no_progress']=2
        r=self.one();r.attempt(self.task,'');r.attempt(self.task,'')
        ledger=accounting.ledger(self.row(r))
        self.assertEqual((ledger['invocations'],ledger['failures'],ledger['no_progress']),(3,0,2))
        self.assertEqual(self.row(r)['accounting_stop'],'max_no_progress')
        self.assertEqual(len(ledger['history']),3)

    def test_post_result_atomic_failure_is_charged_once_and_never_replayed(self):
        r=self.create();original=cp._private_save
        def crash(path,value):
            if Path(path).name.startswith('artifact-'):raise OSError('fixture terminal publish failure')
            return original(path,value)
        with patch.object(cp,'_private_save',side_effect=crash),self.assertRaises(OSError):r.attempt(self.task,'')
        ledger=accounting.ledger(self.row(r));self.assertEqual(ledger['failures'],1)
        self.assertEqual(ledger['history'][0]['outcome'],'continue');self.assertTrue(self.row(r)['delivery_unknown'])
        r.attempt(self.task,'');self.assertEqual(ledger['failures'],1);self.assertEqual(len(self.calls()),1)

    def test_cleanup_failure_retains_unknown_and_failure_not_resumable(self):
        r=self.one();original=r.kill
        def unconfirmed():
            if r.state.get('active_process',{}).get('phase')=='agent':raise OSError('fixture cleanup unconfirmed')
            return original()
        try:
            with patch.object(r,'kill',side_effect=unconfirmed),self.assertRaises(runner.Halt):r.attempt(self.task,'')
            self.assertEqual(r.state['status'],'cleanup_blocked');self.assertTrue(self.row(r)['delivery_unknown'])
            self.assertEqual(accounting.ledger(self.row(r))['failures'],1)
            self.assertEqual(accounting.ledger(self.row(r))['history'][-1]['outcome'],'cleanup_unknown')
        finally:
            original();r.process=None;r.state['active_process']=None
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),2)

    def test_unsafe_grant_cannot_make_private_checkpoint_model_writable(self):
        self.plan['permissions']=dict(source='fixture only',writable_roots=[str(self.base)])
        with self.assertRaisesRegex(ValueError,'outside model'):self.create()
        plan=copy.deepcopy(self.plan);plan.pop('permissions');plan['sandbox']='danger-full-access'
        with self.assertRaises(ValueError):runner.validate(plan)

    def test_safe_snapshot_never_loads_auth_or_raw_private_history(self):
        (self.repo/'auth.json').write_text('fixture-private-never-copy')
        hidden=self.repo/'.codex';hidden.mkdir();(hidden/'history.json').write_text('fixture-history-never-copy')
        value=cp.snapshot(self.repo);self.assertTrue(value['complete'])
        self.assertNotIn('auth.json',value['files']);self.assertNotIn('.codex/history.json',value['files'])
        self.assertNotIn('never-copy',json.dumps(value))

    def test_extensionless_artifacts_and_session_named_source_are_verified(self):
        (self.repo/'artifact').write_text('original')
        (self.repo/'worker_sessions.py').write_text('value = 1\n')
        r=self.one();saved=self.last(r)
        self.assertIn('artifact',saved['source']['files']);self.assertIn('worker_sessions.py',saved['source']['files'])
        (self.repo/'artifact').write_text('changed')
        with self.assertRaises(ValueError):cp.preflight(r,self.task)

    def test_clean_native_resume_after_runner_reload_preserves_exact_session(self):
        r=self.one();first=worker_sessions.load(r,self.task);ledger=copy.deepcopy(accounting.ledger(self.row(r)))
        r=self.owned();self.assertEqual(cp.preflight(r,self.task)['ledger']['history'],ledger['history'])
        r.attempt(self.task,'');second=worker_sessions.load(r,self.task)
        self.assertEqual(first['native_id'],second['native_id']);self.assertIn('resume',self.calls()[1])
        self.assertEqual(accounting.ledger(self.row(r))['invocations'],2)
        self.assertEqual(accounting.ledger(self.row(r))['failures'],0)

    def test_reconstruction_ancestor_tamper_blocks_dispatch_and_host_promotion(self):
        self.plan['worker_session_policy']=dict(version=2,mode='checkpoint_reconstruction')
        r=self.one();r.attempt(self.task,'');receipt=worker_sessions.load(r,self.task)
        self.assertTrue(worker_sessions.host_acceptable(r,self.task))
        with Path(receipt['ancestors'][0]['rollout_path']).open('a') as stream:stream.write('{}\n')
        self.assertFalse(worker_sessions.host_acceptable(r,self.task))
        r.attempt(self.task,'');self.assertEqual(len(self.calls()),2)
        self.assertEqual(accounting.ledger(self.row(r))['failures'],0)
        inputs=[p.read_text() for p in (self.d/'checkpoints').glob('artifact-attempt-*.input.txt')]
        self.assertTrue(any('Explicit checkpoint reconstruction:' in p for p in inputs))
        self.assertFalse(any('Continue the exact task-owned native context' in p for p in inputs))

    def test_actual_workflow_runner_two_turns_and_total_acceptance_remain_independent(self):
        contract=dict(objective='Full workflow goal',scope='synthetic only',authorization='fixture only',memory=False,
            execution_policy=copy.deepcopy(EXECUTION),worker_session_policy=copy.deepcopy(POLICY),checkpoint_policy=copy.deepcopy(CHECKPOINT),
            limits=dict(runtime_seconds=30,attempt_seconds=4,check_seconds=3,max_attempts=2),
            acceptance=[dict(id='artifact',description='final two increments',checks=self.task['checks'])],
            stages=[dict(id='initial',covers=['artifact'],plan=copy.deepcopy(self.plan))])
        p=self.repo/'workflow-input.json';runner.save(p,contract)
        with patch('control.register'):d=workflow.create(self.repo,p)
        w=workflow.Workflow(d);w.execute()
        self.assertEqual(w.state['status'],'completed',w.state)
        child=runner.Runner(w.state['stages'][0]['run_dir']);row=child.state['tasks']['artifact']
        self.assertEqual(row['execution_accounting']['invocations'],2);self.assertEqual(row['execution_accounting']['failures'],0)
        self.assertEqual(len(self.calls()),2);self.assertEqual(w.state['criteria']['artifact']['status'],'passed')
        self.assertIn(self.plan['objective'],self.last(child)['goal'])

    def test_legacy_no_policy_no_new_receipts_or_state(self):
        self.plan.pop('checkpoint_policy');r=self.create()
        self.assertNotIn('incremental_checkpoint',self.row(r))
        import ato_config
        self.assertFalse((ato_config.ato_home()/'incremental-checkpoints-v1').exists())

    def test_policy_strict_bounds_and_no_implicit_legacy_migration(self):
        for field,value in (('version',True),('closing_margin_seconds',4),('closing_margin_seconds',0),('interval_seconds',0),('interval_seconds',301)):
            plan=copy.deepcopy(self.plan);plan['checkpoint_policy'][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):runner.validate(plan)
        plan=copy.deepcopy(self.plan);plan.pop('execution_policy')
        with self.assertRaises(ValueError):runner.validate(plan)

    def test_workflow_propagates_frozen_policy_and_rejects_override(self):
        contract=dict(objective='full',scope='temporary fixture',authorization='test only',memory=False,
            execution_policy=copy.deepcopy(EXECUTION),worker_session_policy=copy.deepcopy(POLICY),checkpoint_policy=copy.deepcopy(CHECKPOINT),
            limits=dict(runtime_seconds=30,attempt_seconds=4,check_seconds=3,max_attempts=2),
            acceptance=[dict(id='artifact',description='artifact',checks=self.task['checks'])],stages=[])
        source=self.repo/'contract-input.json';runner.save(source,contract)
        with patch('control.register'):
            d=workflow.create(self.repo,source)
        w=workflow.Workflow(d);w.stage(dict(objective='increment',tasks=[self.task]),['artifact'])
        child=runner.read(Path(w.state['active_run'])/'plan.json')
        self.assertEqual(child['checkpoint_policy'],CHECKPOINT)
        bad=copy.deepcopy(self.plan);bad['checkpoint_policy']['interval_seconds']=2
        with self.assertRaises(ValueError):w.stage(bad,['artifact'])


if __name__=='__main__':unittest.main()
