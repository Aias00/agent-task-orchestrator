import json
from legacy_support import LEGACY_CLI
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runner
from ato_workflow import workflow
from test_runner import FAKE

PLANNER = '''#!/usr/bin/env python3
import sys,json,os
from pathlib import Path
prompt=sys.stdin.read()
out=Path(sys.argv[sys.argv.index('-o')+1])
if 'You are the planner for an already authorized total task' in prompt:
 failures=json.loads(prompt.split('Current failed total-task criteria and evidence: ',1)[1].split('\\nPrior stages:',1)[0])
 key=next(iter(failures))
 mode=os.environ.get('TEST_PLAN_MODE','normal')
 decision='stage'; reason='Implement remaining criterion '+key
 tasks=[dict(id=key,prompt=key,depends_on=[],covers=[key])]
 if mode in ('waiting','blocked'):decision=mode;tasks=[]
 if mode=='invalid-complete':decision='complete';tasks=[]
 if mode=='replace-checks':tasks[0]['checks']=[['true']]
 if mode=='unknown':tasks[0]['covers']=['invented']
 if mode=='break-earlier':tasks[0]['prompt']='break-earlier'
 out.write_text(json.dumps(dict(decision=decision,reason=reason,tasks=tasks,retry_after_seconds=1)))
else:
 task=prompt.split('Task: ',1)[1].split('\\n',1)[0]
 if task=='sleep':
  import time
  Path('entered').write_text('yes');time.sleep(30)
 elif task=='break-earlier':
  Path('second').write_text('yes')
  Path('first').unlink(missing_ok=True)
 else:Path(task).write_text('yes')
 out.write_text(json.dumps(dict(status='complete',summary=task,next_step='',blocker='',retry_after_seconds=1)))
'''


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', self.tmp.name], check=True)
        self.fake = self.repo/'fake-codex'
        self.fake.write_text(PLANNER); self.fake.chmod(0o755)
        self.env = dict(os.environ, CTASK_CODEX_BIN=str(self.fake))
        self.processes = []

    def tearDown(self):
        for p in self.processes:
            if p.poll() is None:
                p.terminate(); p.wait(timeout=5)
        self.tmp.cleanup()

    def criterion(self, key):
        return dict(id=key, description='Required output '+key,
                    checks=[[sys.executable, '-c', f'from pathlib import Path; assert Path({key!r}).exists()']])

    def contract(self, keys=('first', 'second'), stages=True):
        criteria = [self.criterion(k) for k in keys]
        initial = [dict(id='initial', covers=[keys[0]], plan=dict(objective='First phase only', memory=False,
                    tasks=[dict(id=keys[0], prompt=keys[0], checks=criteria[0]['checks'])]))] if stages else []
        return dict(objective='Deliver the complete two-part feature', scope='Only files in this test repository',
                    authorization='Local implementation and tests only; no external actions', memory=False,
                    acceptance=criteria, stages=initial,
                    limits=dict(runtime_seconds=45,attempt_seconds=10,check_seconds=3,max_attempts=2,max_stages=4,max_planning_attempts=4,max_wait_seconds=1))

    def create(self, contract):
        p=self.repo/'contract-input.json';runner.save(p,contract)
        return workflow.create(self.repo,p)

    def run_workflow(self, d, mode='normal', retry=False):
        argv=[sys.executable,str(LEGACY_CLI),'run',str(d)]
        if retry:argv.append('--retry-failed')
        r=subprocess.run(argv,env=dict(self.env,TEST_PLAN_MODE=mode),capture_output=True,text=True,timeout=55)
        self.assertEqual(r.returncode,0,r.stderr)
        return runner.read(d/'state.json')

    def test_stage_completion_continues_to_full_objective(self):
        d=self.create(self.contract())
        s=self.run_workflow(d)
        self.assertEqual(s['status'],'completed')
        self.assertEqual(len(s['stages']),2)
        self.assertTrue((self.repo/'first').exists() and (self.repo/'second').exists())
        second=Path(s['stages'][1]['run_dir']);plan=runner.read(second/'plan.json')
        self.assertIn('complete two-part feature',plan['objective'])
        self.assertEqual(plan['tasks'][0]['checks'],self.criterion('second')['checks'])
        self.assertEqual(runner.status(second)['completion_scope'],'stage_only')
        self.assertEqual(runner.status(d)['remaining'],[])
        self.assertEqual(runner.read(second/'parent.json')['workflow_dir'],str(d))

    def test_stage_success_does_not_hide_later_regression(self):
        c=self.contract();c['limits']['max_stages']=2
        d=self.create(c);s=self.run_workflow(d,'break-earlier')
        self.assertNotEqual(s['status'],'completed')
        self.assertEqual(s['criteria']['first']['status'],'failed')
        self.assertEqual(s['criteria']['second']['status'],'passed')
        self.assertTrue(all(r['status']=='completed' for r in s['stages']))

    def test_manual_acceptance_is_not_inferred_and_evidence_is_checked(self):
        c=self.contract(keys=('first',));c['acceptance'].append(dict(id='human',description='User accepts output',kind='manual'))
        d=self.create(c);s=self.run_workflow(d)
        self.assertEqual(s['status'],'needs_input')
        evidence=self.repo/'acceptance.txt';evidence.write_text('User explicitly accepted the result')
        workflow.acknowledge(d,'human',evidence,'User acceptance recorded in this fixture')
        self.assertEqual(self.run_workflow(d)['status'],'completed')
        evidence.write_text('different evidence')
        self.assertEqual(self.run_workflow(d)['status'],'needs_input')

    def test_manual_ack_is_invalidated_by_subsequent_repairs(self):
        c=self.contract(keys=('first',));c['acceptance'].append(dict(id='human',description='User acceptance',kind='manual'))
        d=self.create(c);self.run_workflow(d)
        evidence=self.repo/'ack.txt';evidence.write_text('accepted')
        workflow.acknowledge(d,'human',evidence,'User said accepted')
        (self.repo/'first').unlink()
        self.assertEqual(self.run_workflow(d)['status'],'needs_input')

    def test_planner_cannot_claim_complete_or_change_acceptance(self):
        for mode in ['invalid-complete','replace-checks','unknown']:
            with self.subTest(mode=mode):
                d=self.create(self.contract(stages=False));s=self.run_workflow(d,mode)
                self.assertEqual(s['status'],'error')
                self.assertEqual(s['stages'],[])
                self.assertFalse((self.repo/'first').exists())

    def test_blocked_planner_preserves_remaining_work(self):
        d=self.create(self.contract());s=self.run_workflow(d,'blocked')
        self.assertEqual(s['status'],'blocked')
        self.assertEqual(runner.status(d)['remaining'],['second'])
        self.assertTrue((self.repo/'first').exists())

    def test_waiting_is_bounded(self):
        c=self.contract(stages=False);c['limits']['max_planning_attempts']=2
        d=self.create(c);s=self.run_workflow(d,'waiting')
        self.assertEqual(s['status'],'planning_limit')
        self.assertEqual(s['planning_attempts'],2)

    def test_total_runtime_budget_interrupts_the_child(self):
        c=self.contract(keys=('sleep','second'));c['limits']['runtime_seconds']=1
        d=self.create(c);s=self.run_workflow(d)
        self.assertEqual(s['status'],'limit')
        self.assertIsNone(s['owner_pid'])
        self.assertFalse((self.repo/'second').exists())
        child=runner.read(Path(s['active_run'])/'state.json')
        self.assertIsNone(child['active_process'])
        self.assertGreaterEqual(s['elapsed_seconds'],1)

    def test_frozen_total_contract_cannot_be_narrowed(self):
        d=self.create(self.contract());c=runner.read(d/'contract.json');c['acceptance']=c['acceptance'][:1];runner.save(d/'contract.json',c)
        result=subprocess.run([sys.executable,str(LEGACY_CLI),'run',str(d)],env=self.env,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.repo/'first').exists())

    def test_stop_propagates_to_child_and_resume_continues(self):
        c=self.contract(keys=('sleep','second'));d=self.create(c)
        p=subprocess.Popen([sys.executable,str(LEGACY_CLI),'_worker',str(d)],env=self.env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.processes.append(p)
        deadline=time.monotonic()+8
        while not (self.repo/'entered').exists() and time.monotonic()<deadline:time.sleep(.05)
        self.assertTrue((self.repo/'entered').exists())
        competing=subprocess.run([sys.executable,str(LEGACY_CLI),'start',str(d)],env=self.env,capture_output=True)
        self.assertNotEqual(competing.returncode,0)
        (d/'STOP').touch();p.wait(timeout=6)
        s=runner.read(d/'state.json');self.assertEqual(s['status'],'stopped')
        self.assertFalse((self.repo/'second').exists())
        self.fake.write_text(PLANNER.replace("Path('entered').write_text('yes');time.sleep(30)","Path('sleep').write_text('yes')"))
        subprocess.run([sys.executable,str(LEGACY_CLI),'resume',str(d)],env=self.env,check=True,capture_output=True)
        deadline=time.monotonic()+12
        while runner.read(d/'state.json')['status']!='completed' and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(runner.read(d/'state.json')['status'],'completed')

    def test_missing_total_acceptance_and_bad_coverage_rejected(self):
        for mutate in [lambda c:c.update(acceptance=[]),lambda c:c['stages'][0].update(covers=['missing']),lambda c:c.update(authorization='')]:
            c=self.contract();mutate(c)
            with self.assertRaises(ValueError):self.create(c)


class CleanupTests(unittest.TestCase):
    def test_cleanup_permission_failure_is_persisted_as_terminal_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);subprocess.run(['git','init','-q',tmp],check=True)
            p=root/'plan.json';runner.save(p,{'objective':'Cleanup failure','memory':False,'tasks':[{'id':'a','prompt':'a','checks':[['true']]}]})
            d=runner.create(root,p);r=runner.Runner(d)
            def attempt(task,memory):
                r.state['tasks']['a']['status']='running'
                r.state['active_process']={'pid':99999999,'argv':['fixture'],'evidence':'fixture'}
                r.persist()
                raise PermissionError('cleanup denied')
            with patch.object(r,'attempt',side_effect=attempt),patch.object(r,'kill',side_effect=PermissionError('cleanup denied')):
                r.execute()
            s=runner.read(d/'state.json')
            self.assertEqual(s['status'],'cleanup_blocked')
            self.assertIsNone(s['owner_pid'])
            self.assertIsNotNone(s['active_process'])
            self.assertEqual(s['tasks']['a']['status'],'pending')
            self.assertIn('run_finished',(d/'events.jsonl').read_text())


if __name__ == '__main__':unittest.main()
