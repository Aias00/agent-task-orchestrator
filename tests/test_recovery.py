import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runner
import workflow
import recovery
from test_workflow import PLANNER

THREAD = '11111111-1111-4111-8111-000000000001'
from legacy_support import LEGACY_CLI


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.home = self.repo/'codex-home'; self.home.mkdir()
        self.rollout = self.home/'session.jsonl'
        self.rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':THREAD}})+'\n')
        with sqlite3.connect(self.home/'state_5.sqlite') as con:
            con.execute('create table threads (id text, rollout_path text)')
            con.execute('insert into threads values (?,?)', (THREAD, str(self.rollout)))
        self.fake = self.repo/'fake-codex'; self.fake.write_text(PLANNER); self.fake.chmod(0o755)
        self.env = dict(os.environ, CODEX_HOME=str(self.home), CTASK_CODEX_BIN=str(self.fake))
        self.patcher = patch.dict(os.environ, self.env); self.patcher.start()
        self.contract = dict(objective='Both required files', scope='This isolated repo',
                             authorization='Local test fixtures only', memory=False,
                             acceptance=[dict(id=k, description=k, checks=[[sys.executable, '-c',
                             f'from pathlib import Path; assert Path({k!r}).exists()']]) for k in ['first','second']],
                             limits=dict(runtime_seconds=60,attempt_seconds=10,check_seconds=2,max_attempts=1))
        plan=self.repo/'contract.json'; runner.save(plan,self.contract)
        self.d=workflow.create(self.repo,plan,THREAD)
        self.set_state('blocked')
        recovery.enable(self.d,'User explicitly requested automatic original-session recovery',response_seconds=15)

    def tearDown(self):
        recovery.disable(self.d)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            p=self.d/'supervisor.json'
            if not p.exists() or not recovery.alive(runner.read(p).get('pid')):break
            time.sleep(.1)
        self.patcher.stop(); self.tmp.cleanup()

    def set_state(self,status):
        s=runner.read(self.d/'state.json');s.update(status=status,owner_pid=None)
        runner.save(self.d/'state.json',s)

    def resumed_then_blocked(self):
        with (self.d/'events.jsonl').open('a') as f:
            f.write(json.dumps(dict(event='workflow_started',time=time.time()))+'\n')

    def queued(self):
        return subprocess.CompletedProcess([],0,f'Queued message {THREAD} for thread {THREAD}.\n','')

    def ledger(self):
        return runner.read(self.d/'recovery.json')

    def test_queue_once_until_actual_resume_and_new_failure(self):
        with patch('recovery.subprocess.run', return_value=self.queued()) as send:
            self.assertTrue(recovery.tick(self.d))
            for _ in range(3):self.assertTrue(recovery.tick(self.d))
            self.assertEqual(send.call_count,1)
            self.resumed_then_blocked()
            self.assertTrue(recovery.tick(self.d))
            self.assertEqual(send.call_count,2)
            self.assertEqual(self.ledger()['requests'][0]['state'],'resumed')
            self.resumed_then_blocked()
            self.assertTrue(recovery.tick(self.d))
            self.assertEqual(send.call_count,3)
            self.assertEqual(self.ledger()['requests'][-1]['escalation'],'repeated_blocker')
            self.assertEqual(self.ledger()['requests'][-1]['mode'],'report')
            recovery.tick(self.d);self.assertEqual(send.call_count,3)

    def test_unknown_delivery_and_crash_never_resend(self):
        with patch('recovery.subprocess.run', side_effect=subprocess.TimeoutExpired('queue',25)) as send:
            self.assertFalse(recovery.tick(self.d))
            self.assertFalse(recovery.tick(self.d))
            self.assertEqual(send.call_count,1)
        r=self.ledger();r['requests'][0]['state']='dispatching';runner.save(self.d/'recovery.json',r)
        with patch('recovery.subprocess.run') as send:
            self.assertFalse(recovery.tick(self.d));send.assert_not_called()
        self.assertEqual(self.ledger()['status'],'delivery_unknown')

    def test_concurrent_watchers_share_one_durable_outbox(self):
        queue=self.repo/'concurrent-queue'
        queue.write_text(f'''#!{sys.executable}
import sys,time
with open('queue-calls','a') as f:f.write('sent\\n')
time.sleep(.2)
thread=sys.argv[sys.argv.index('--thread')+1]
print('Queued message '+thread+' for thread '+thread+'.')
''');queue.chmod(0o755)
        env=dict(self.env,CTASK_QUEUE_BIN=str(queue))
        cmd=[sys.executable,'-c',f'import recovery; recovery.tick({str(self.d)!r})']
        processes=[subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        for proc in processes:
            out,err=proc.communicate(timeout=8);self.assertEqual(proc.returncode,0,err)
        self.assertEqual((self.repo/'queue-calls').read_text(),'sent\n')
        self.assertEqual(len(self.ledger()['requests']),1)

    def test_same_episode_cannot_be_reset_by_reenable(self):
        with patch('recovery.subprocess.run',return_value=self.queued()) as send:
            recovery.tick(self.d)
            before=self.ledger()
            recovery.disable(self.d)
            recovery.enable(self.d,'re-enable does not reset budget',max_requests=99)
            recovery.tick(self.d)
            self.assertEqual(send.call_count,1)
            self.assertEqual(self.ledger()['max_requests'],before['max_requests'])
            self.assertEqual(self.ledger()['deadline'],before['deadline'])

    def test_missing_or_changed_binding_prevents_dispatch(self):
        origin=runner.read(self.d/'origin.json');origin['origin_thread_id']=str(__import__('uuid').uuid4())
        runner.save(self.d/'origin.json',origin)
        with patch('recovery.subprocess.run') as send:
            self.assertFalse(recovery.tick(self.d));send.assert_not_called()
        self.assertEqual(self.ledger()['status'],'binding_changed')
        with self.assertRaises(ValueError):recovery.enable(self.d,'invalid session')

    def test_contract_tamper_prevents_dispatch(self):
        c=runner.read(self.d/'contract.json');c['acceptance']=[];runner.save(self.d/'contract.json',c)
        with patch('recovery.subprocess.run') as send:
            self.assertFalse(recovery.tick(self.d));send.assert_not_called()

    def test_repository_busy_and_live_owner_do_not_dispatch(self):
        with patch('recovery.subprocess.run') as send:
            with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
                self.assertTrue(recovery.tick(self.d));send.assert_not_called()
            s=runner.read(self.d/'state.json');s.update(owner_pid=os.getpid());runner.save(self.d/'state.json',s)
            self.assertTrue(recovery.tick(self.d));send.assert_not_called()

    def test_claim_does_not_count_as_resumed_and_input_stops(self):
        with patch('recovery.subprocess.run',return_value=self.queued()) as send:
            recovery.tick(self.d);q=self.ledger()['requests'][-1]
            recovery.takeover(self.d,q['id'],'working','Inspecting evidence')
            recovery.tick(self.d)
            self.assertEqual(self.ledger()['status'],'working')
            self.assertEqual(runner.read(self.d/'state.json')['status'],'blocked')
            recovery.takeover(self.d,q['id'],'needs_input','Need user business decision')
            self.assertFalse(recovery.tick(self.d));self.assertEqual(send.call_count,1)

    def test_stale_stopped_and_expired_claims_are_rejected(self):
        with patch('recovery.subprocess.run',return_value=self.queued()):recovery.tick(self.d)
        q=self.ledger()['requests'][-1]
        (self.d/'STOP').touch()
        with self.assertRaises(ValueError):recovery.takeover(self.d,q['id'],'working','late')
        self.assertFalse(recovery.tick(self.d))
        (self.d/'STOP').unlink()
        r=self.ledger();r['requests'][-1]['expires_at']=0;runner.save(self.d/'recovery.json',r)
        with self.assertRaises(ValueError):recovery.takeover(self.d,q['id'],'working','expired')
        self.assertFalse(recovery.tick(self.d));self.assertEqual(self.ledger()['status'],'stalled')

    def test_response_is_observed_only_after_matching_user_request(self):
        with patch('recovery.subprocess.run',return_value=self.queued()):recovery.tick(self.d)
        q=self.ledger()['requests'][-1]
        with self.rollout.open('a') as f:
            f.write(json.dumps({'type':'response_item','payload':{'role':'assistant','content':[{'text':'unrelated'}]}})+'\n')
        recovery.tick(self.d);self.assertNotIn('responded_at',self.ledger()['requests'][-1])
        with self.rollout.open('a') as f:
            for role,text in [('user',q['message']),('assistant','Inspecting this request')]:
                f.write(json.dumps({'type':'response_item','payload':{'role':role,'content':[{'text':text}]}})+'\n')
        recovery.tick(self.d);self.assertIn('responded_at',self.ledger()['requests'][-1])
        self.assertEqual(self.ledger()['requests'][-1]['state'],'queued')

    def test_manual_and_runtime_limits_only_request_reporting(self):
        for status in ['needs_input','limit']:
            self.set_state(status)
            self.resumed_then_blocked()
            with patch('recovery.subprocess.run',return_value=self.queued()):recovery.tick(self.d)
            self.assertEqual(self.ledger()['requests'][-1]['mode'],'report')
            self.assertEqual(runner.read(self.d/'state.json')['status'],status)

    def test_changed_errors_still_obey_total_request_budget(self):
        with patch('recovery.subprocess.run',return_value=self.queued()) as send:
            for i in range(5):
                s=runner.read(self.d/'state.json');s['error']='different-'+chr(65+i);runner.save(self.d/'state.json',s)
                self.resumed_then_blocked();recovery.tick(self.d)
            self.assertEqual(send.call_count,4)  # Three repair requests, one report-only escalation.
            self.assertEqual(self.ledger()['status'],'budget_exhausted')

    def test_repair_requires_unchanged_evidence_and_report_cannot_resume(self):
        with patch('recovery.subprocess.run',return_value=self.queued()):recovery.tick(self.d)
        q=self.ledger()['requests'][-1]
        with self.assertRaises(ValueError):recovery.takeover(self.d,q['id'],'repaired','no evidence')
        proof=self.repo/'repair.md';proof.write_text('verified fixture fix')
        recovery.takeover(self.d,q['id'],'repaired','fixed',str(proof))
        proof.write_text('changed after acknowledgement')
        with patch('recovery.subprocess.run') as launch:
            self.assertFalse(recovery.tick(self.d));launch.assert_not_called()
        self.set_state('needs_input');self.resumed_then_blocked()
        with patch('recovery.subprocess.run',return_value=self.queued()):recovery.tick(self.d)
        q=self.ledger()['requests'][-1]
        with self.assertRaises(ValueError):recovery.takeover(self.d,q['id'],'repaired','not user approval',str(proof))

    def test_expired_window_only_notifies_and_cannot_launch_repair(self):
        r=self.ledger();r['deadline']=0;runner.save(self.d/'recovery.json',r)
        with patch('recovery.subprocess.run',return_value=self.queued()) as send:
            self.assertTrue(recovery.tick(self.d));recovery.tick(self.d)
            self.assertEqual(send.call_count,1)
        q=self.ledger()['requests'][-1]
        self.assertEqual(q['mode'],'report');self.assertEqual(q['escalation'],'window_exhausted')
        recovery.takeover(self.d,q['id'],'needs_input','Supervision budget requires a user decision')
        self.assertFalse(recovery.tick(self.d))

    def test_end_to_end_queue_claim_fix_resume_and_next_stage(self):
        # A separate fixture coordinator consumes the queue message and runs the
        # public takeover/resume commands. Workers and watcher are real processes.
        self.set_state('created')
        fake=PLANNER.replace("else:Path(task).write_text('yes')", "else:\n  if task=='first' and not Path('repaired').exists():\n   out.write_text(json.dumps(dict(status='blocked',summary='Missing fixture prerequisite',next_step='',blocker='Coordinator must repair prerequisite',retry_after_seconds=1)));sys.exit(0)\n  Path(task).write_text('yes')")
        self.fake.write_text(fake)
        coordinator=self.repo/'coordinator.py'
        coordinator.write_text('''import subprocess,sys,time
from pathlib import Path
runner,d,request=sys.argv[1:]
subprocess.run([sys.executable,runner,'takeover',d,'--request',request,'--state','working','--note','Fix fixture prerequisite'],check=True)
Path('repaired').write_text('yes')
Path('repair-evidence.md').write_text('Fixed the missing fixture prerequisite; local file verified.')
subprocess.run([sys.executable,runner,'takeover',d,'--request',request,'--state','repaired','--evidence','repair-evidence.md','--note','Fixture prerequisite exists'],check=True)
''')
        queue=self.repo/'queue-fixture'
        queue.write_text(f'''#!{sys.executable}
import subprocess,sys,re
from pathlib import Path
message=sys.argv[sys.argv.index('--message')+1]
request=re.search(r'request=(recover-[a-f0-9]+)',message)[1]
thread=sys.argv[sys.argv.index('--thread')+1]
with open('coordinator.log','ab') as log:
 subprocess.Popen([sys.executable,{str(coordinator)!r},{str(LEGACY_CLI)!r},{str(self.d)!r},request],stdout=log,stderr=log,start_new_session=True)
print('Queued message '+thread+' for thread '+thread+'.')
''');queue.chmod(0o755)
        env=dict(self.env,CTASK_QUEUE_BIN=str(queue))
        result=subprocess.run([sys.executable,str(LEGACY_CLI),'start',str(self.d)],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            s=runner.read(self.d/'state.json')
            if s['status']=='completed' and self.ledger()['status']=='completed':break
            time.sleep(.1)
        self.assertEqual(s['status'],'completed',(self.repo/'coordinator.log').read_text() if (self.repo/'coordinator.log').exists() else self.ledger())
        self.assertEqual(len(s['stages']),2)
        self.assertEqual(len(self.ledger()['requests']),1)
        self.assertEqual(self.ledger()['requests'][0]['state'],'completed')
        self.assertTrue((self.repo/'first').exists() and (self.repo/'second').exists())
        self.assertGreater(s['elapsed_seconds'],0)
        self.assertEqual(runner.digest(runner.read(self.d/'contract.json')),s['contract_hash'])


if __name__ == '__main__':unittest.main()
