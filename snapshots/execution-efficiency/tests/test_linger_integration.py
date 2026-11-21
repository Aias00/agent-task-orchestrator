"""Combined Workflow/Runner/capacity/accounting/CLI behavior, synthetic only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import capacity
import runner
import workflow

ROOT = Path(runner.__file__).resolve().parent
POLICY = dict(version=2, max_continuations=6, max_waits=4, max_yields=4, max_no_progress=3)
FIXTURE = '''import json,sys
from pathlib import Path
out=Path(sys.argv[sys.argv.index('-o')+1])
mode=sys.stdin.read().split('Task: ',1)[1].split('\\n',1)[0]
p=Path('.synthetic-count');n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))
Path('feature.py').write_text('step = '+str(n))
status=['continue','waiting','yielded','complete'][min(n-1,3)] if mode=='sequence' else 'complete'
if status=='complete' and mode!='false-complete':Path('artifact').write_text('verified')
out.write_text(json.dumps(dict(status=status,summary='synthetic integrated sequence',
 next_step='remaining complete scope',blocker='synthetic finite dependency' if status=='waiting' else '',retry_after_seconds=1)))
'''


class LingerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-linger-integrated-'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['/usr/bin/git','init','-q',str(self.repo)],check=True)
        self.home = self.root/'ato'; self.home.mkdir(mode=0o700)
        runner.save(self.home/'capacity.json', dict(version=1,endpoints={'fixture':dict(limit=2,reserved_interactive=1)}))
        self.fake = self.repo/'fixture-cli'; self.fake.write_text('#!'+sys.executable+'\n'+FIXTURE); self.fake.chmod(0o755)
        self.env = dict(os.environ, ATO_HOME=str(self.home), CODEX_HOME=str(self.root/'codex'),
                        CTASK_CODEX_BIN=str(self.fake), PYTHONDONTWRITEBYTECODE='1')
        for key in ('CODEX_THREAD_ID','CTASK_SESSION_REF','CTASK_SESSION_TOKEN','CTASK_ENDPOINT_TOKEN'):
            self.env.pop(key,None)
        self.env_patch = patch.dict(os.environ,self.env,clear=True); self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.processes = []; self.addCleanup(self.cleanup)
        self.q = capacity.CapacityStore(self.home/'capacity.sqlite3'); self.q.configure('fixture',2,1)

    def cleanup(self):
        for p in self.processes:
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=6)
                except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=6)

    def plan(self, mode='sequence'):
        return dict(objective='Integrated stage',memory=False,execution_policy=POLICY.copy(),
            capacity=dict(endpoint='fixture',priority='background'),
            limits=dict(runtime_seconds=35,attempt_seconds=4,check_seconds=2,max_attempts=2),
            tasks=[dict(id='work',prompt=mode,checks=[[sys.executable,'-c',
                "from pathlib import Path; assert Path('artifact').read_text()=='verified'"]])])

    def launch(self, directory):
        p = subprocess.Popen([sys.executable,'-B',str(ROOT/'runner.py'),'run',str(directory)],env=self.env,
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.processes.append(p); return p

    def summary(self, directory):
        p = subprocess.run([sys.executable,'-B',str(ROOT/'runner.py'),'task-summary',str(directory),'--format','json'],
                           env=self.env,capture_output=True,text=True,timeout=6)
        self.assertEqual(p.returncode,0,p.stderr); return json.loads(p.stdout)

    def wait_for(self, predicate):
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.03)
        self.fail('synthetic integrated behavior did not arrive')

    def test_queued_workflow_stage_continues_waits_yields_then_leaves_manual_acceptance(self):
        plan = self.plan()
        contract = dict(objective='The complete integrated objective',scope='temporary fixture only',
            authorization='synthetic local test',memory=False,execution_policy=POLICY.copy(),capacity=plan['capacity'],
            limits=plan['limits'],acceptance=[dict(id='artifact',description='Verified artifact',checks=plan['tasks'][0]['checks']),
                dict(id='human',kind='manual',description='Actual manual decision remains')],
            stages=[dict(id='integrated',covers=['artifact'],plan=plan)])
        inp = self.repo/'input.json'; runner.save(inp,contract); directory = workflow.create(self.repo,inp)
        self.q.enqueue('fixture','synthetic-holder','background','synthetic-owner',1); held = self.q.claim('synthetic-holder')
        p = self.launch(directory)
        def queued_stage():
            state = runner.read(directory/'state.json'); active = state.get('active_run')
            return active and runner.read(Path(active)/'state.json')['tasks']['work']['status']=='capacity_wait'
        self.wait_for(queued_stage)
        stage = Path(runner.read(directory/'state.json')['active_run'])
        self.assertEqual(runner.read(stage/'plan.json')['execution_policy'],POLICY)
        before = (stage/'state.json').read_bytes()
        queued = self.summary(stage)
        self.assertEqual(queued['execution_accounting']['invocations'],0)
        self.assertEqual(queued['execution_accounting']['active_seconds'],0)
        self.assertEqual(queued['overall']['objective'],'The complete integrated objective')
        self.assertEqual(before,(stage/'state.json').read_bytes())
        self.assertFalse((self.repo/'.synthetic-count').exists())
        time.sleep(.2); self.assertTrue(self.q.release('synthetic-holder',held['token']))
        p.wait(timeout=20); self.assertEqual(p.returncode,0)
        state = runner.read(directory/'state.json'); self.assertEqual(state['status'],'needs_input',state)
        row = runner.read(stage/'state.json')['tasks']['work']; ledger = row['execution_accounting']
        self.assertEqual((row['attempts'],ledger['invocations'],ledger['failures']),(4,4,0))
        self.assertEqual((ledger['continuations'],ledger['waits'],ledger['yields']),(1,1,1))
        self.assertGreaterEqual(ledger['wait_seconds'],1); self.assertGreaterEqual(ledger['yield_seconds'],.9)
        requests = [r for r in self.q.snapshot('fixture')['requests'] if r['request_id']!='synthetic-holder']
        self.assertEqual([r['generation'] for r in requests],[1,2,3,4])
        self.assertTrue(all(r['state']=='released' for r in requests))
        view = self.summary(directory); self.assertTrue(view['needs_user']); self.assertEqual(view['completion_scope'],'overall')
        self.assertIn('human',[item['id'] for item in view['remaining']])
        self.assertFalse((directory/'manual.json').exists())

    def test_false_complete_under_capacity_spends_only_exact_failure_allowance(self):
        inp = self.repo/'input.json'; runner.save(inp,self.plan('false-complete')); directory = runner.create(self.repo,inp)
        p = self.launch(directory); p.wait(timeout=12); self.assertEqual(p.returncode,0)
        row = runner.read(directory/'state.json')['tasks']['work']
        self.assertEqual((row['status'],row['attempts'],row['execution_accounting']['failures']),('failed',2,2))
        self.assertEqual(len(self.q.snapshot('fixture')['active']),0)
        summary = self.summary(directory); self.assertEqual(summary['execution_accounting']['failures'],2)
        self.assertEqual(summary['completion_scope'],'stage_only')

    def test_old_plan_ignores_missing_capacity_config_and_keeps_unknown_failure_history(self):
        (self.home/'capacity.json').unlink()
        plan = self.plan('complete'); del plan['capacity']; del plan['execution_policy']
        inp = self.repo/'input.json'; runner.save(inp,plan); directory = runner.create(self.repo,inp)
        p = self.launch(directory); p.wait(timeout=12); self.assertEqual(p.returncode,0)
        row = runner.read(directory/'state.json')['tasks']['work']
        self.assertEqual(row['status'],'completed'); self.assertNotIn('execution_accounting',row)
        self.assertNotIn('capacity_request',row)
        self.assertIsNone(self.summary(directory)['execution_accounting']['failures'])
        self.assertEqual(self.q.snapshot('fixture')['requests'],[])
