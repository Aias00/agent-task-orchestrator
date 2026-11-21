"""Runner timeout boundaries using a code-owned, model-free subprocess adapter.

These tests do not replace sandbox-exec or invoke installed native CLIs. The
historical Pi host error remains unknown until its frozen host test can run.
"""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agents
import execution_accounting as accounting
import runner
import task_trace

FIXTURE = r'''
import json,os,sys,time,subprocess,signal
from pathlib import Path
out=Path(sys.argv[1]);mode=Path('mode').read_text()
Path('leader.pid').write_text(str(os.getpid()))
if mode=='sleep':
 child=subprocess.Popen([sys.executable,'-c',"import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)"])
 Path('child.pid').write_text(str(child.pid))
 # Reap the descendant on TERM, retaining the exact process group boundary.
 def stop(*args):
  child.kill();child.wait(timeout=2);sys.exit(143)
 signal.signal(signal.SIGTERM,stop)
 time.sleep(20)
else:
 Path('artifact').write_text('done')
 out.write_text(json.dumps(dict(status='complete',summary='synthetic',next_step='',blocker='',retry_after_seconds=1)))
'''
POLICY = dict(version=2,max_continuations=3,max_waits=2,max_yields=2,max_no_progress=2)

class BoundaryAdapter:
    kind='timeout_evidence_fixture'
    capabilities=frozenset({'worker','planner','progress'})
    def command(self, request):
        return [sys.executable,str(Path(request.directory).parents[2]/'boundary.py'),str(request.output)]

if BoundaryAdapter.kind not in agents.registry.kinds():
    agents.registry.register(BoundaryAdapter())


class TimeoutFailureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ato-timeout-evidence-');self.addCleanup(self.tmp.cleanup)
        self.repo=Path(self.tmp.name).resolve()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        (self.repo/'boundary.py').write_text(FIXTURE);(self.repo/'mode').write_text('sleep')
        env=dict(os.environ,ATO_HOME=str(self.repo/'ato'),CODEX_HOME=str(self.repo/'native'))
        for k in ('CODEX_THREAD_ID','CTASK_SESSION_REF','CTASK_SESSION_TOKEN','CTASK_ENDPOINT_TOKEN'):
            env.pop(k,None)
        p=patch.dict(os.environ,env,clear=True);p.start();self.addCleanup(p.stop)
        old={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
        self.addCleanup(lambda:[signal.signal(s,h) for s,h in old.items()])

    def create(self,v2=False):
        self.task=dict(id='work',prompt='Synthetic timeout fixture only',checks=[[sys.executable,'-c',
            "from pathlib import Path;assert Path('artifact').read_text()=='done'"]])
        self.plan=dict(objective='Bounded timeout and explicit legacy resume',memory=False,
            agent=dict(kind=BoundaryAdapter.kind),tasks=[self.task],
            limits=dict(runtime_seconds=15,attempt_seconds=1,check_seconds=2,max_attempts=1))
        if v2:self.plan['execution_policy']=copy.deepcopy(POLICY)
        p=self.repo/'input.json';runner.save(p,self.plan);self.d=runner.create(self.repo,p)
        return runner.Runner(self.d)

    def events(self):return [json.loads(l) for l in (self.d/'events.jsonl').read_text().splitlines()]
    def state(self):return runner.read(self.d/'state.json')
    def assert_clean(self):
        s=self.state();self.assertIsNone(s['active_process']);self.assertIsNone(s['owner_pid'])
        for name in ('leader.pid','child.pid'):
            pid=int((self.repo/name).read_text())
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        with self.assertRaises(ProcessLookupError):os.killpg(int((self.repo/'leader.pid').read_text()),0)

    def test_legacy_timeout_is_blocked_spends_one_attempt_and_reaps_exact_group(self):
        self.create().execute();s=self.state();self.assertEqual(s['status'],'blocked',s)
        row=s['tasks']['work'];self.assertEqual((row['attempts'],row['attempt_sequence']),(1,1))
        self.assertIn('exit 124',row['feedback']);self.assertEqual(len([e for e in self.events() if e['event']=='command_timeout']),1)
        self.assertFalse(any(e['event']=='run_error' for e in self.events()));self.assert_clean()

    def test_v2_timeout_failure_charged_once_and_retry_preserves_history(self):
        self.create(True).execute();s=self.state();row=s['tasks']['work'];value=accounting.ledger(row)
        self.assertEqual(s['status'],'blocked',s);self.assertEqual((value['invocations'],value['failures']),(1,1))
        self.assertEqual(value['history'][0]['outcome'],'timeout');self.assert_clean()
        original=copy.deepcopy(value['history']);(self.repo/'mode').write_text('complete')
        runner.Runner(self.d).execute(retry=True)
        value=accounting.ledger(self.state()['tasks']['work'])
        self.assertEqual(value['history'],original);self.assertEqual((value['invocations'],value['failures']),(1,1))
        self.assertEqual(self.state()['status'],'blocked')

    def test_explicit_legacy_resume_new_checkpoint_keeps_sequence_elapsed_and_trace(self):
        self.create().execute();first=self.state();before=(self.d/'plan.json').read_bytes()
        inputs={p.name:p.read_bytes() for p in (self.d/'checkpoints').glob('*.input.txt')}
        (self.repo/'mode').write_text('complete');runner.Runner(self.d).execute(retry=True)
        s=self.state();self.assertEqual(s['status'],'completed',s)
        self.assertEqual(s['tasks']['work']['attempt_sequence'],2);self.assertGreater(s['elapsed_seconds'],first['elapsed_seconds'])
        self.assertEqual((self.d/'plan.json').read_bytes(),before)
        self.assertFalse((self.d/'runtime-selection.json').exists())
        for name,value in inputs.items():self.assertEqual((self.d/'checkpoints'/name).read_bytes(),value)
        self.assertEqual(len(list((self.d/'checkpoints').glob('work-attempt-*.input.txt'))),2)
        starts=[e['attempt'] for e in task_trace.query(self.d,'work')['events'] if e['kind']=='attempt_started']
        self.assertEqual(starts,[1,2])

    def test_stop_still_fences_invocation(self):
        r=self.create();(self.d/'STOP').touch()
        with self.assertRaisesRegex(ValueError,'STOP'):r.execute()
        self.assertEqual(self.state()['tasks']['work']['attempts'],0);self.assertFalse((self.repo/'leader.pid').exists())

    def test_unexpected_error_remains_error_with_bounded_body_free_locations(self):
        r=self.create();(self.repo/'mode').write_text('complete')
        def fail(*args,**kwargs):
            private_local='DO_NOT_COPY_LOCALS'
            raise TypeError('synthetic decoding defect')
        with patch.object(agents.registry.ExecutionResult,'from_mapping',side_effect=fail):r.execute()
        self.assertEqual(self.state()['status'],'error')
        error=next(e for e in self.events() if e['event']=='run_error')
        self.assertEqual(error['error_type'],'TypeError');self.assertGreater(len(error['locations']),0)
        self.assertLessEqual(len(error['locations']),8)
        self.assertTrue(all(set(v)=={'file','function','line'} for v in error['locations']))
        self.assertNotIn('DO_NOT_COPY_LOCALS',json.dumps(error))
        self.assertTrue(all('/' not in v['file'] for v in error['locations']))
        self.assertIsNone(self.state()['active_process'])

    def test_unconfirmed_cleanup_stays_blocked_and_cannot_be_reported_success(self):
        r=self.create(True);original=r.kill
        def unconfirmed():
            original()
            if (r.state.get('active_process') or {}).get('phase')=='agent':
                raise OSError('synthetic exact cleanup unconfirmed')
        with patch.object(r,'kill',side_effect=unconfirmed):r.execute()
        s=self.state();self.assertEqual(s['status'],'cleanup_blocked',s)
        self.assertIsNotNone(s['active_process']);self.assertTrue(s['tasks']['work']['delivery_unknown'])
        self.assertEqual(accounting.ledger(s['tasks']['work'])['invocations'],1)
        self.assertEqual(accounting.ledger(s['tasks']['work'])['failures'],1)
        history=copy.deepcopy(accounting.ledger(s['tasks']['work'])['history'])
        (self.repo/'mode').write_text('complete');runner.Runner(self.d).execute(retry=True)
        row=self.state()['tasks']['work']
        self.assertEqual(self.state()['status'],'blocked');self.assertTrue(row['delivery_unknown'])
        self.assertEqual(accounting.ledger(row)['history'],history)
