import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runner
from legacy_support import LEGACY_CLI


FAKE = '''#!/usr/bin/env python3
import sys,json,time
from pathlib import Path
prompt=sys.stdin.read()
out=Path(sys.argv[sys.argv.index('-o')+1])
task=prompt.split('Task: ',1)[1].split('\\n',1)[0]
status='complete'
if task=='repair':
 p=Path('attempts'); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))
 if n>=2: Path('fixed').write_text('yes')
elif task=='block': status='blocked'
elif task=='sleep':
 Path('entered').write_text('yes'); time.sleep(30)
elif task=='wait':
 p=Path('waited')
 if not p.exists(): p.write_text('yes'); status='waiting'
 else: Path('wait-done').write_text('yes')
elif task=='upstream': Path('upstream').write_text('valid')
elif task=='invalidate-once':
 if not Path('invalidated').exists(): Path('upstream').write_text('broken'); Path('invalidated').write_text('yes')
 Path('integrated').write_text('yes')
else: Path(task).write_text('yes')
out.write_text(json.dumps(dict(status=status,summary=task,next_step='continue',blocker='requires input' if status=='blocked' else '',retry_after_seconds=1)))
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', self.tmp.name], check=True)
        self.fake = self.repo / 'fake-codex'
        self.fake.write_text(FAKE); self.fake.chmod(0o755)
        self.env = dict(os.environ, CTASK_CODEX_BIN=str(self.fake))
        self.processes = []

    def tearDown(self):
        for p in self.processes:
            if p.poll() is None:
                p.terminate(); p.wait(timeout=5)
        self.tmp.cleanup()

    def task(self, id, prompt=None, deps=None, expression=None):
        return {'id': id, 'prompt': prompt or id, 'depends_on': deps or [],
                'checks': [[sys.executable, '-c', expression or f'from pathlib import Path; assert Path({id!r}).exists()']]}

    def create(self, tasks, **limits):
        p = self.repo / 'input-plan.json'
        p.write_text(json.dumps({'objective': 'Test scheduler', 'memory': False, 'tasks': tasks,
                                 'limits': dict(runtime_seconds=40, attempt_seconds=10, check_seconds=3, max_attempts=3) | limits}))
        return runner.create(self.repo, p)

    def foreground(self, d, retry=False):
        command = [sys.executable, str(LEGACY_CLI), 'run', str(d)]
        if retry: command.append('--retry-failed')
        subprocess.run(command, env=self.env, check=True, timeout=45, capture_output=True)
        return runner.read(d/'state.json')

    def background(self, d):
        p = subprocess.Popen([sys.executable, str(LEGACY_CLI), '_worker', str(d)], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(p)
        return p

    def wait_for(self, predicate, timeout=10):
        deadline=time.time()+timeout
        while time.time()<deadline:
            if predicate(): return
            time.sleep(.05)
        self.fail('condition timed out')

    def test_repair_then_dependency(self):
        d=self.create([self.task('fixed','repair'),self.task('downstream',deps=['fixed'])])
        s=self.foreground(d)
        self.assertEqual(s['status'],'completed')
        self.assertEqual(s['tasks']['fixed']['attempts'],2)
        events=[json.loads(l) for l in (d/'events.jsonl').read_text().splitlines()]
        results=[e for e in events if e['event']=='task_result']
        self.assertEqual([(e['task'],e['status']) for e in results],[('fixed','pending'),('fixed','completed'),('downstream','completed')])

    def test_blocked_branch_does_not_stop_independent_work(self):
        d=self.create([self.task('a','block'), self.task('b',deps=['a']),self.task('independent')])
        s=self.foreground(d)
        self.assertEqual(s['tasks']['a']['status'],'blocked')
        self.assertEqual(s['tasks']['b']['status'],'dependency_blocked')
        self.assertEqual(s['tasks']['independent']['status'],'completed')

    def test_cycle_missing_dependency_and_missing_checks_rejected(self):
        for tasks in [[self.task('a',deps=['b']),self.task('b',deps=['a'])],[self.task('a',deps=['missing'])],[dict(id='a',prompt='a')]]:
            with self.assertRaises(ValueError): self.create(tasks)

    def test_dependency_field_typo_cannot_silently_remove_ordering(self):
        t=self.task('a'); t['dependsOn']=['missing']
        with self.assertRaises(ValueError): self.create([t])

    def test_claimed_completion_cannot_bypass_acceptance(self):
        d=self.create([self.task('a',expression='raise RuntimeError("acceptance failed")')])
        s=self.foreground(d)
        self.assertEqual(s['status'],'blocked')
        self.assertEqual(s['tasks']['a']['attempts'],3)
        self.assertEqual(s['tasks']['a']['status'],'failed')

    def test_resume_rechecks_previously_completed_work(self):
        d=self.create([self.task('a'),self.task('b',deps=['a'])])
        self.foreground(d)
        (self.repo/'a').unlink()
        s=self.foreground(d)
        self.assertEqual(s['status'],'completed')
        self.assertTrue((self.repo/'a').exists())
        self.assertEqual(s['tasks']['b']['attempts'],1)

    def test_final_integration_failure_is_repaired_automatically(self):
        d=self.create([self.task('upstream',expression='from pathlib import Path; assert Path("upstream").read_text()=="valid"'),self.task('integrated','invalidate-once',deps=['upstream'])])
        s=self.foreground(d)
        self.assertEqual(s['status'],'completed')
        self.assertEqual(s['tasks']['upstream']['attempts'],2)
        self.assertEqual(s['tasks']['integrated']['attempts'],2)

    def test_waiting_task_yields_to_independent_work(self):
        d=self.create([self.task('wait-done','wait'),self.task('independent')])
        s=self.foreground(d)
        self.assertEqual(s['status'],'completed')
        events=[json.loads(l) for l in (d/'events.jsonl').read_text().splitlines()]
        results=[e['task'] for e in events if e['event']=='task_result']
        self.assertEqual(results,['wait-done','independent','wait-done'])

    def test_stop_terminates_child_and_preserves_state(self):
        d=self.create([self.task('sleep','sleep'),self.task('later')])
        p=self.background(d)
        self.wait_for(lambda:(self.repo/'entered').exists())
        child=runner.read(d/'state.json')['active_process']['pid']
        (d/'STOP').touch()
        p.wait(timeout=6)
        s=runner.read(d/'state.json')
        self.assertEqual(s['status'],'stopped')
        self.assertFalse((self.repo/'later').exists())
        with self.assertRaises(ProcessLookupError): os.kill(child,0)

    def test_repo_lock_prevents_concurrent_workers(self):
        d=self.create([self.task('sleep','sleep')])
        p=self.background(d)
        self.wait_for(lambda:(self.repo/'entered').exists())
        second=subprocess.run([sys.executable,str(LEGACY_CLI),'start',str(d)],env=self.env,capture_output=True)
        self.assertNotEqual(second.returncode,0)
        (d/'STOP').touch();p.wait(timeout=6)

    def test_runtime_limit_stops_active_worker(self):
        d=self.create([self.task('sleep','sleep')],runtime_seconds=1)
        s=self.foreground(d)
        self.assertEqual(s['status'],'limit')
        self.assertIsNone(s['active_process'])

    def test_attempt_timeout_is_bounded(self):
        d=self.create([self.task('sleep','sleep')],attempt_seconds=1,max_attempts=1)
        s=self.foreground(d)
        self.assertEqual(s['status'],'blocked')
        self.assertEqual(s['tasks']['sleep']['attempts'],1)

    def test_detached_start_and_resume_after_stop(self):
        d=self.create([self.task('sleep','sleep')])
        self.background(d)
        self.wait_for(lambda:(self.repo/'entered').exists())
        (d/'STOP').touch()
        self.wait_for(lambda:runner.read(d/'state.json')['status']=='stopped')
        self.fake.write_text(FAKE.replace('time.sleep(30)', 'Path("sleep").write_text("yes")'))
        launch=subprocess.run([sys.executable,str(LEGACY_CLI),'resume',str(d)],env=self.env,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(launch.stdout)['status'],'launching')
        self.wait_for(lambda:runner.read(d/'state.json')['status']=='completed')

    def test_frozen_plan_change_is_rejected(self):
        d=self.create([self.task('a')]);plan=runner.read(d/'plan.json');plan['objective']='changed';runner.save(d/'plan.json',plan)
        r=subprocess.run([sys.executable,str(LEGACY_CLI),'run',str(d)],env=self.env,capture_output=True)
        self.assertNotEqual(r.returncode,0)
        self.assertFalse((self.repo/'a').exists())


if __name__=='__main__': unittest.main()
