"""Capacity behavior through durable stores and actual isolated Runner processes.

No models, installed runtime, real hub/task mutations or network are used.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agents
import capacity
import runner
import workflow

POLICY = dict(version=2, max_continuations=6, max_waits=4, max_yields=4, max_no_progress=3)
SELECTION = dict(endpoint='fixture', priority='background')
FAKE = '''import json,sys,time
from pathlib import Path
prompt=sys.stdin.read()
out=Path(sys.argv[sys.argv.index('-o')+1])
if 'You are the planner' in prompt:
 Path('planner-entered').touch()
 result=dict(decision='stage',reason='synthetic next phase',retry_after_seconds=1,
   tasks=[dict(id='work',prompt='complete',depends_on=[],covers=['full'])])
else:
 mode=prompt.split('Task: ',1)[1].split('\\n',1)[0]
 Path('entered').touch()
 if mode=='sleep': time.sleep(30)
 if mode=='malformed': out.write_text('invalid'); raise SystemExit(0)
 p=Path('count'); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))
 status=mode if mode in ('waiting','yielded','continue') and n==1 else 'complete'
 if status=='complete': Path('artifact').write_text('done')
 result=dict(status=status,summary='synthetic capacity receipt',next_step='continue',blocker='',retry_after_seconds=1)
out.write_text(json.dumps(result))
'''


class CapacityQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-capacity-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root/'ato'; self.home.mkdir(mode=0o700)
        self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.repo)], check=True)
        self.fake = self.repo/'fake-cli'
        self.fake.write_text('#!'+sys.executable+'\n'+FAKE); self.fake.chmod(0o755)
        self.env = dict(os.environ, ATO_HOME=str(self.home), CODEX_HOME=str(self.root/'codex'),
                        CTASK_CODEX_BIN=str(self.fake), PYTHONDONTWRITEBYTECODE='1')
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            self.env.pop(key, None)
        self.env_patch = patch.dict(os.environ, self.env, clear=True)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.processes = []
        self.addCleanup(self.cleanup_processes)
        self.q = capacity.CapacityStore(self.home/'capacity.sqlite3')
        self.q.configure('fixture', limit=2, reserved_interactive=1)
        self.config()

    def cleanup_processes(self):
        for p in self.processes:
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait(timeout=6)

    def config(self, limit=2, reserve=1):
        runner.save(self.home/'capacity.json', dict(version=1, endpoints={
            'fixture': dict(limit=limit, reserved_interactive=reserve)}))

    def enq(self, key, priority='background', owner='owner', generation=1):
        return self.q.enqueue('fixture', key, priority, owner, generation)

    def plan(self, mode='complete', **changes):
        p = dict(objective='Full synthetic capacity objective', memory=False,
                 execution_policy=copy.deepcopy(POLICY), capacity=copy.deepcopy(SELECTION),
                 limits=dict(runtime_seconds=45, attempt_seconds=4, check_seconds=2, max_attempts=2),
                 tasks=[dict(id='work', prompt=mode, checks=[[sys.executable, '-c',
                    "from pathlib import Path; assert Path('artifact').exists()"]])])
        p.update(changes)
        return p

    def create(self, plan=None, repo=None):
        repo = repo or self.repo
        source = repo/'input.json'; runner.save(source, plan or self.plan())
        return runner.create(repo, source)

    def launch(self, directory, *args):
        p = subprocess.Popen([sys.executable, '-B', str(ROOT/'runner.py'), 'run', str(directory), *args],
                             env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(p)
        return p

    def wait_for(self, predicate, timeout=6):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.03)
        self.fail('timed out waiting for synthetic behavior')

    def row(self, d):
        return runner.read(d/'state.json')['tasks']['work']

    def finish(self, p, d):
        p.wait(timeout=15)
        self.assertEqual(p.returncode, 0)
        return runner.read(d/'state.json')

    def test_reserve_remains_available_to_interaction_across_connections(self):
        for key, priority in [('b1','background'), ('b2','background'), ('i1','interactive')]: self.enq(key, priority)
        self.assertTrue(self.q.claim('b1'))
        other = capacity.CapacityStore(self.q.path)
        self.assertIsNone(other.claim('b2'))
        self.assertTrue(other.claim('i1'))
        self.assertEqual(len(self.q.snapshot('fixture')['active']), 2)

    def test_interaction_priority_and_background_fifo_are_enforced(self):
        self.q.configure('fixture', 1, 0)
        for key, priority in [('old','background'), ('new','background'), ('human','interactive')]: self.enq(key, priority)
        self.assertIsNone(self.q.claim('old')); self.assertIsNone(self.q.claim('new'))
        grant = self.q.claim('human'); self.assertTrue(grant)
        self.assertTrue(self.q.release('human', grant['token']))
        self.assertIsNone(self.q.claim('new')); self.assertTrue(self.q.claim('old'))

    def test_no_ttl_cleanup_or_owner_death_reclaims_a_slot(self):
        self.enq('held'); grant = self.q.claim('held'); self.enq('waiting')
        later = capacity.CapacityStore(self.q.path, clock=lambda: 10**12)
        self.assertIsNone(later.claim('waiting'))
        self.assertFalse(later.release('held', grant['token'], cleanup_confirmed=False))
        self.assertIsNone(later.claim('waiting'))

    def test_trusted_reduction_never_revokes_existing_claim_or_grants_extra_slot(self):
        self.q.configure('fixture',3,0)
        for key in ('one','two','three'):
            self.enq(key); self.assertTrue(self.q.claim(key))
        self.q.configure('fixture',1,0)
        self.enq('four')
        self.assertEqual(len(self.q.snapshot('fixture')['active']),3)
        self.assertIsNone(self.q.claim('four'))
        for key in ('one','two','three'):
            g = self.q.claim(key)
            self.assertTrue(self.q.release(key,g['token']))
        self.assertTrue(self.q.claim('four'))

    def test_exact_token_owner_generation_and_cleanup_required(self):
        self.enq('held'); grant = self.q.claim('held'); self.enq('next')
        for kwargs in [dict(token='forged'), dict(token=grant['token'], owner='other'),
                       dict(token=grant['token'], generation=2), dict(token=grant['token'], generation=True),
                       dict(token=grant['token'], cleanup_confirmed=1)]:
            self.assertFalse(self.q.release('held', **kwargs))
            self.assertIsNone(self.q.claim('next'))
        self.assertTrue(self.q.release('held', grant['token'], owner='owner', generation=1))
        self.assertFalse(self.q.release('held', grant['token']))
        self.assertTrue(self.q.claim('next'))

    def test_request_identity_is_immutable_including_priority_and_endpoint(self):
        original = self.enq('one')
        self.assertEqual(self.enq('one'), original)
        for kwargs in [dict(owner='other'), dict(generation=2), dict(priority='interactive')]:
            with self.assertRaises(ValueError): self.enq('one', **kwargs)
        self.q.configure('other', 1, 0)
        with self.assertRaises(ValueError): self.q.enqueue('other','one','background','owner',1)

    def test_cancel_only_exact_pending_identity_never_active(self):
        self.enq('one')
        self.assertFalse(self.q.cancel('one', 'wrong', 1))
        self.assertFalse(self.q.cancel('one', 'owner', 2))
        self.assertTrue(self.q.cancel('one', 'owner', 1))
        self.assertIsNone(self.q.claim('one'))
        self.assertEqual(self.enq('one')['state'], 'cancelled')
        self.enq('two'); self.q.claim('two')
        self.assertFalse(self.q.cancel('two', 'owner', 1))

    def test_restart_preserves_pending_order_terminal_tombstones_and_claim_token(self):
        self.enq('one'); self.enq('two')
        grant = self.q.claim('one')
        other = capacity.CapacityStore(self.q.path)
        self.assertEqual(other.claim('one')['token'], grant['token'])
        self.assertIsNone(other.claim('two'))
        self.assertTrue(other.release('one', grant['token']))
        self.assertEqual(other.enqueue('fixture','one','background','owner',1)['state'], 'released')
        self.assertIsNone(other.claim('one')); self.assertTrue(other.claim('two'))

    def test_independent_process_atomic_claim_race_does_not_oversubscribe(self):
        self.q.configure('fixture', 1, 0)
        self.enq('one'); self.enq('two')
        code = '''import sys,json,time
from capacity import CapacityStore
q=CapacityStore(sys.argv[1]);sys.stdin.read(1)
for _ in range(30):
 g=q.claim(sys.argv[2])
 if g: break
 time.sleep(.005)
print(json.dumps(g))
'''
        children = [subprocess.Popen([sys.executable,'-B','-c',code,str(self.q.path),key], cwd=ROOT,
                    env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for key in ('one','one','two','two')]
        self.processes.extend(children)
        for p in children: p.stdin.write('x'); p.stdin.flush()
        grants = []
        for p in children:
            out, err = p.communicate(timeout=6)
            self.assertEqual(p.returncode, 0, err)
            if json.loads(out): grants.append(json.loads(out))
        self.assertTrue(grants)
        self.assertEqual({g['request_id'] for g in grants}, {'one'})
        self.assertEqual(len({g['token'] for g in grants}), 1)
        self.assertEqual(len(self.q.snapshot('fixture')['active']), 1)

    def test_snapshot_exposes_background_age_and_fifo_without_release_tokens(self):
        q = capacity.CapacityStore(self.q.path, clock=lambda:100)
        q.enqueue('fixture','old','background','owner',1)
        later = capacity.CapacityStore(self.q.path, clock=lambda:160)
        view = later.snapshot('fixture')
        self.assertEqual(view['oldest_background_wait_seconds'], 60)
        self.assertNotIn('token', view['pending'][0])
        self.assertIn('starvation possible', view['fairness'])

    def test_invalid_aliases_limits_priorities_and_generation_are_rejected(self):
        for name in ('https://example/key=x', '../escape', 'user:password', 'api-token', '', 'x/y'):
            with self.assertRaises(ValueError): self.q.configure(name, 2, 1)
        for limit, reserve in [(True,0), (0,0), (2,2), (2,-1), (2,False), (1.5,0)]:
            with self.assertRaises(ValueError): self.q.configure('bad', limit, reserve)
        for kwargs in [dict(priority='urgent'), dict(generation=True), dict(generation=-1), dict(owner='')]:
            with self.assertRaises(ValueError): self.enq('bad', **kwargs)

    def test_plan_cannot_set_global_limit_and_workflow_cannot_override_selection(self):
        for selection in (None, {}, SELECTION | dict(limit=100), SELECTION | dict(priority='high')):
            with self.assertRaises(ValueError): runner.validate(self.plan(capacity=selection))
        contract = self.contract()
        contract['stages'][0]['plan']['capacity'] = SELECTION | dict(priority='interactive')
        with self.assertRaises(ValueError): workflow.validate(contract)
        contract = self.contract(); del contract['capacity']
        contract['stages'][0]['plan']['capacity'] = SELECTION
        with self.assertRaises(ValueError): workflow.validate(contract)

    def test_trusted_config_rejects_symlink_writeable_unknown_and_duplicate_fields(self):
        path = self.home/'capacity.json'
        valid = path.read_text()
        for data in [dict(version=True,endpoints={}), dict(version=1,endpoints={'fixture':dict(limit=4,reserved_interactive=0,extra=1)}),
                     dict(version=2,endpoints={}), dict(version=1,endpoints={},extra=1)]:
            runner.save(path,data)
            with self.assertRaises(ValueError): capacity.configured_store(SELECTION)
        path.write_text('{"version":1,"version":1,"endpoints":{}}')
        with self.assertRaises(ValueError): capacity.configured_store(SELECTION)
        path.write_text(valid); path.chmod(0o666)
        with self.assertRaises(ValueError): capacity.configured_store(SELECTION)
        path.unlink(); target = self.root/'outside.json'; target.write_text(valid); path.symlink_to(target)
        with self.assertRaises(ValueError): capacity.configured_store(SELECTION)

    def test_actual_runner_waits_before_model_invocation_failure_or_active_charge(self):
        self.enq('holder'); held = self.q.claim('holder')
        d = self.create(); p = self.launch(d)
        self.wait_for(lambda: self.row(d)['status']=='capacity_wait')
        time.sleep(.4)
        row = self.row(d); self.assertFalse((self.repo/'entered').exists())
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['execution_accounting']['invocations'], 0)
        self.assertEqual(row['execution_accounting']['failures'], 0)
        self.assertEqual(row['execution_accounting']['active_seconds'], 0)
        self.assertEqual(len(self.q.snapshot('fixture')['pending']), 1)
        self.q.release('holder', held['token'])
        state = self.finish(p,d); self.assertEqual(state['status'],'completed',state)
        self.assertEqual(self.row(d)['attempts'],1)
        self.assertGreaterEqual(self.row(d)['execution_accounting']['wait_seconds'],.4)
        self.assertEqual(self.row(d)['capacity_request']['state'],'released')

    def test_actual_stop_cancels_queued_request_without_starting_or_spending_failure(self):
        self.enq('holder'); self.q.claim('holder')
        d = self.create(); p = self.launch(d)
        self.wait_for(lambda: self.row(d)['status']=='capacity_wait')
        (d/'STOP').touch()
        state = self.finish(p,d)
        self.assertEqual(state['status'],'stopped')
        self.assertEqual(self.row(d)['capacity_request']['state'],'cancelled')
        self.assertEqual(self.row(d)['execution_accounting']['invocations'],0)
        self.assertFalse((self.repo/'entered').exists())
        self.assertEqual(len(self.q.snapshot('fixture')['pending']),0)
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)
        before = (d/'state.json').read_bytes(); stopped = self.launch(d); stopped.wait(timeout=5)
        self.assertNotEqual(stopped.returncode,0); self.assertEqual(before,(d/'state.json').read_bytes())

    def test_missing_capacity_config_fails_closed_with_no_invocation(self):
        (self.home/'capacity.json').unlink()
        d = self.create(); state = self.finish(self.launch(d),d)
        self.assertEqual(state['status'],'capacity_blocked',state)
        self.assertIn('capacity.json',state['error'])
        self.assertEqual(self.row(d)['attempts'],0)
        self.assertFalse((self.repo/'entered').exists())

    def test_stop_cli_cancels_dead_owner_pending_queue_without_reclaiming_holder(self):
        self.enq('holder'); self.q.claim('holder')
        d = self.create(); p = self.launch(d)
        self.wait_for(lambda: self.row(d)['status']=='capacity_wait')
        request = self.row(d)['capacity_request']['request_id']
        p.kill(); p.wait(timeout=5)
        before = (d/'state.json').read_bytes()
        stopped = subprocess.run([sys.executable,'-B',str(ROOT/'runner.py'),'stop',str(d)],
                                 env=self.env,capture_output=True,text=True,timeout=5)
        self.assertEqual(stopped.returncode,0,stopped.stderr)
        self.assertTrue((d/'STOP').exists())
        self.assertEqual(before,(d/'state.json').read_bytes())
        view = self.q.snapshot('fixture')
        self.assertEqual(len(view['active']),1); self.assertEqual(len(view['pending']),0)
        self.assertEqual(next(r['state'] for r in view['requests'] if r['request_id']==request),'cancelled')
        self.assertFalse((self.repo/'entered').exists())

    def test_stop_cli_cancels_live_queue_and_retains_exact_cancelled_receipt(self):
        self.enq('holder'); self.q.claim('holder')
        d = self.create(); p = self.launch(d)
        self.wait_for(lambda: self.row(d)['status']=='capacity_wait')
        stopped = subprocess.run([sys.executable,'-B',str(ROOT/'runner.py'),'stop',str(d)],
                                 env=self.env,capture_output=True,text=True,timeout=5)
        self.assertEqual(stopped.returncode,0,stopped.stderr)
        self.assertEqual(self.finish(p,d)['status'],'stopped')
        self.assertEqual(self.row(d)['capacity_request']['state'],'cancelled')
        self.assertFalse((self.repo/'entered').exists())

    def test_unconfirmed_actual_runner_cleanup_keeps_claim_and_unknown_barrier(self):
        d = self.create(self.plan('yielded'))
        code = '''import sys
from unittest.mock import patch
import runner
r=runner.Runner(sys.argv[1]);r.state['tasks']['work']['verified_checks']=[False];r.persist()
def failed():raise PermissionError('synthetic cleanup unavailable')
with patch.object(r,'kill',side_effect=failed):r.execute()
# Restore the real cleanup method solely to reap our own synthetic child; the
# recorded receipt stays unknown and is deliberately not released or replayed.
r.kill()
'''
        p = subprocess.run([sys.executable,'-B','-c',code,str(d)],cwd=ROOT,env=self.env,capture_output=True,text=True,timeout=8)
        self.assertEqual(p.returncode,0,p.stderr)
        state = runner.read(d/'state.json'); row = self.row(d)
        self.assertEqual(state['status'],'cleanup_blocked')
        self.assertTrue(row['delivery_unknown'])
        self.assertEqual(row['capacity_request']['state'],'active')
        self.assertEqual(row['execution_accounting']['yields'],0)
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)

    def test_unconfigured_endpoint_fails_closed_and_legacy_without_opt_in_runs(self):
        d = self.create(self.plan(capacity=SELECTION | dict(endpoint='absent')))
        self.assertEqual(self.finish(self.launch(d),d)['status'],'capacity_blocked')
        (self.home/'capacity.json').unlink()
        plan = self.plan(); del plan['capacity']; del plan['execution_policy']
        legacy = self.create(plan); state = self.finish(self.launch(legacy),legacy)
        self.assertEqual(state['status'],'completed',state)
        self.assertNotIn('capacity_request',self.row(legacy))

    def test_real_runner_cross_repository_queue_and_interactive_reserve(self):
        first = self.create(self.plan('sleep'))
        p1 = self.launch(first)
        self.wait_for(lambda: (self.repo/'entered').exists())
        other = self.root/'other'; other.mkdir(); subprocess.run(['/usr/bin/git','init','-q',str(other)],check=True)
        bg = self.create(repo=other); p2 = self.launch(bg)
        self.wait_for(lambda: self.row(bg)['status']=='capacity_wait')
        third = self.root/'third'; third.mkdir(); subprocess.run(['/usr/bin/git','init','-q',str(third)],check=True)
        interactive = self.create(self.plan(capacity=SELECTION | dict(priority='interactive')),repo=third)
        self.assertEqual(self.finish(self.launch(interactive),interactive)['status'],'completed')
        self.assertEqual(self.row(bg)['attempts'],0)
        (first/'STOP').touch(); self.assertEqual(self.finish(p1,first)['status'],'stopped')
        self.assertEqual(self.finish(p2,bg)['status'],'completed')
        self.assertEqual(len(self.q.snapshot('fixture')['active']),0)

    def test_wait_and_yield_release_capacity_and_reacquire_new_generation(self):
        for mode in ('waiting','yielded','continue'):
            with self.subTest(mode=mode):
                for name in ('artifact','count'): (self.repo/name).unlink(missing_ok=True)
                d = self.create(self.plan(mode)); state = self.finish(self.launch(d),d)
                self.assertEqual(state['status'],'completed',state)
                own = [r for r in self.q.snapshot('fixture')['requests'] if r['owner']==self.row(d)['capacity_request']['owner']]
                self.assertEqual([r['generation'] for r in own],[1,2])
                self.assertTrue(all(r['state']=='released' for r in own))
                self.assertEqual(self.row(d)['execution_accounting']['failures'],0)

    def test_crash_after_claim_never_replays_or_reclaims_without_cleanup(self):
        d = self.create()
        code = '''import os,sys
import runner
r=runner.Runner(sys.argv[1]);r.capacity_admit(r.state['tasks']['work'],'work',1)
os._exit(0)
'''
        result = subprocess.run([sys.executable,'-B','-c',code,str(d)],cwd=ROOT,env=self.env,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.row(d)['capacity_request']['state'],'active')
        state = self.finish(self.launch(d,'--retry-failed'),d)
        self.assertEqual(state['status'],'blocked',state)
        self.assertTrue(self.row(d)['delivery_unknown'])
        self.assertFalse((self.repo/'entered').exists())
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)

    def test_pending_runner_restart_reuses_fifo_request_without_double_enqueue(self):
        self.enq('holder'); held = self.q.claim('holder')
        d = self.create(); p = self.launch(d)
        self.wait_for(lambda: self.row(d)['status']=='capacity_wait')
        request = self.row(d)['capacity_request']['request_id']
        p.kill(); p.wait(timeout=5)
        # This synthetic runner never started a command; pending state remains safe.
        # Capacity is not repository ownership: retain its independent lease
        # fence and wait for that existing owner's expiry before foreground run.
        import transport
        import recovery
        lease = transport.managed_bus().execution_lease(recovery.execution_resource(self.repo))
        while time.time() <= lease['lease_until']+.05:
            time.sleep(.1)
        resumed = self.launch(d)
        self.wait_for(lambda: runner.read(d/'state.json').get('owner_pid')==resumed.pid)
        self.assertEqual(self.row(d)['capacity_request']['request_id'],request)
        self.assertEqual(len(self.q.snapshot('fixture')['pending']),1)
        self.q.release('holder',held['token'])
        self.assertEqual(self.finish(resumed,d)['status'],'completed')
        self.assertEqual(self.row(d)['attempts'],1)

    def test_crash_between_sqlite_claim_and_json_receipt_blocks_runner_restart(self):
        d = self.create()
        code = '''import os,sys
import runner
r=runner.Runner(sys.argv[1]);row=r.state['tasks']['work'];persist=r.persist
def crash_before_active_json():
 if row.get('capacity_request',{}).get('state')=='active':os._exit(0)
 persist()
r.persist=crash_before_active_json
r.capacity_admit(row,'work',1)
'''
        p = subprocess.run([sys.executable,'-B','-c',code,str(d)],cwd=ROOT,env=self.env,
                           capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(self.row(d)['capacity_request']['state'],'pending')
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)
        state = self.finish(self.launch(d,'--retry-failed'),d)
        self.assertEqual(state['status'],'blocked',state)
        self.assertTrue(self.row(d)['delivery_unknown'])
        self.assertEqual(self.row(d)['attempts'],0)
        self.assertFalse((self.repo/'entered').exists())
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)

    def test_unknown_acp_result_retains_slot_and_cannot_retry(self):
        d = self.create(self.plan('malformed',agent=dict(kind='acp_pi')))
        def argv(plan, directory, label, schema, output, **kwargs):
            return [sys.executable,str(self.fake),'-o',str(output)]
        code = '''import sys
from unittest.mock import patch
import runner,agents
def command(plan,directory,label,schema,output,**kwargs):return [sys.executable,sys.argv[2],'-o',str(output)]
with patch.object(agents,'command',side_effect=command):runner.Runner(sys.argv[1]).execute(retry=True)
'''
        for _ in range(2):
            p = subprocess.run([sys.executable,'-B','-c',code,str(d),str(self.fake)],cwd=ROOT,env=self.env,capture_output=True,text=True,timeout=8)
            self.assertEqual(p.returncode,0,p.stderr)
        row = self.row(d)
        self.assertTrue(row['delivery_unknown']); self.assertEqual(row['attempts'],1)
        self.assertEqual(row['capacity_request']['state'],'active')
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)

    def test_runner_release_refusal_blocks_acceptance_despite_passing_artifact(self):
        d = self.create()
        code = '''import sys
from unittest.mock import patch
import capacity,runner
with patch.object(capacity.CapacityStore,'release',return_value=False):runner.Runner(sys.argv[1]).execute()
'''
        p = subprocess.run([sys.executable,'-B','-c',code,str(d)],cwd=ROOT,env=self.env,
                           capture_output=True,text=True,timeout=8)
        self.assertEqual(p.returncode,0,p.stderr)
        state = runner.read(d/'state.json'); row = self.row(d)
        self.assertTrue((self.repo/'artifact').exists())
        self.assertEqual(state['status'],'capacity_unknown',state)
        self.assertEqual(row['status'],'blocked')
        self.assertTrue(row['delivery_unknown'])
        self.assertEqual(row['execution_accounting']['failures'],0)
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)
        self.assertEqual(self.finish(self.launch(d,'--retry-failed'),d)['status'],'blocked')
        self.assertEqual(self.row(d)['attempts'],1)

    def test_task_summary_queue_is_read_only_and_does_not_create_missing_store(self):
        d = self.create()
        (self.home/'capacity.sqlite3').unlink()
        before = (d/'state.json').read_bytes()
        view = runner.status(d)['task_view']
        self.assertEqual(view['capacity']['knowledge'],'unknown')
        self.assertFalse((self.home/'capacity.sqlite3').exists())
        self.assertEqual(before,(d/'state.json').read_bytes())

    def test_task_summary_cli_reads_capacity_age_without_writes_or_tokens(self):
        d = self.create(); self.enq('queued')
        before = {p: p.read_bytes() for p in [d/'state.json',self.q.path,self.home/'capacity.json']}
        result = subprocess.run([sys.executable,'-B',str(ROOT/'runner.py'),'task-summary',str(d),'--format','json'],
                                env=self.env,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        view = json.loads(result.stdout)
        self.assertEqual(view['capacity']['pending'][0]['request_id'],'queued')
        self.assertGreaterEqual(view['capacity']['oldest_background_wait_seconds'],0)
        self.assertNotIn('token',view['capacity']['pending'][0])
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def contract(self, generated=False):
        plan = self.plan(); plan.pop('capacity')
        return dict(objective='Full workflow capacity objective',scope='temporary isolated fixture',authorization='synthetic',
                    memory=False,execution_policy=copy.deepcopy(POLICY),capacity=copy.deepcopy(SELECTION),
                    limits=dict(runtime_seconds=45,attempt_seconds=4,check_seconds=2,max_attempts=2,
                                max_stages=3,max_planning_attempts=3,max_wait_seconds=1),
                    acceptance=[dict(id='full',description='artifact exists',checks=plan['tasks'][0]['checks'])],
                    stages=[] if generated else [dict(id='first',covers=['full'],plan=plan)])

    def create_workflow(self, generated=False):
        source = self.repo/'contract.json'; runner.save(source,self.contract(generated))
        return workflow.create(self.repo,source)

    def test_actual_workflow_stage_inherits_exact_selection_and_queues(self):
        self.enq('holder'); held = self.q.claim('holder')
        d = self.create_workflow(); p = self.launch(d)
        self.wait_for(lambda: bool(runner.read(d/'state.json').get('active_run')))
        child = Path(runner.read(d/'state.json')['active_run'])
        self.wait_for(lambda: self.row(child)['status']=='capacity_wait')
        self.assertEqual(runner.read(child/'plan.json')['capacity'],SELECTION)
        self.assertEqual(self.row(child)['attempts'],0)
        self.q.release('holder',held['token'])
        state = self.finish(p,d)
        self.assertEqual(state['status'],'completed',state)
        self.assertEqual(state['execution_accounting']['failures'],0)

    def test_workflow_stop_cli_cancels_exact_child_pending_request(self):
        self.enq('holder'); self.q.claim('holder')
        d = self.create_workflow(); p = self.launch(d)
        self.wait_for(lambda: bool(runner.read(d/'state.json').get('active_run')))
        child = Path(runner.read(d/'state.json')['active_run'])
        self.wait_for(lambda: self.row(child)['status']=='capacity_wait')
        p.kill(); p.wait(timeout=5)
        stopped = subprocess.run([sys.executable,'-B',str(ROOT/'runner.py'),'stop',str(d)],
                                 env=self.env,capture_output=True,text=True,timeout=5)
        self.assertEqual(stopped.returncode,0,stopped.stderr)
        self.assertTrue((d/'STOP').exists()); self.assertFalse((child/'STOP').exists())
        self.assertEqual(len(self.q.snapshot('fixture')['pending']),0)
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)
        self.assertFalse((self.repo/'entered').exists())

    def test_actual_workflow_planner_queues_before_planning_attempt_and_releases(self):
        self.enq('holder'); held = self.q.claim('holder')
        d = self.create_workflow(generated=True); p = self.launch(d)
        self.wait_for(lambda: runner.read(d/'state.json')['status']=='capacity_wait')
        state = runner.read(d/'state.json')
        self.assertEqual(state['planning_attempts'],0)
        self.assertFalse((self.repo/'planner-entered').exists())
        self.q.release('holder',held['token'])
        state = self.finish(p,d)
        self.assertEqual(state['status'],'completed',state)
        self.assertEqual(state['planning_attempts'],1)
        self.assertEqual(state['planning_execution']['capacity_request']['state'],'released')
        self.assertEqual(state['execution_accounting']['invocations'],2)
        self.assertEqual(len(self.q.snapshot('fixture')['active']),0)

    def test_workflow_unknown_planner_claim_cannot_be_promoted_by_passing_checks(self):
        for v2 in (False, True):
            with self.subTest(v2=v2):
                contract = self.contract(generated=True)
                if not v2: contract.pop('execution_policy')
                source = self.repo/'contract.json'; runner.save(source,contract)
                d = workflow.create(self.repo,source)
                code = '''import os,sys
import workflow
w=workflow.Workflow(sys.argv[1])
row=w.state.setdefault('planning_execution' if 'execution_policy' in w.plan else 'capacity_planning',dict(status='pending'))
persist=w.persist
def crash_before_active_json():
 if row.get('capacity_request',{}).get('state')=='active':os._exit(0)
 persist()
w.persist=crash_before_active_json
w.capacity_admit(row,'planner',1)
'''
                p = subprocess.run([sys.executable,'-B','-c',code,str(d)],cwd=ROOT,env=self.env,
                                   capture_output=True,text=True,timeout=5)
                self.assertEqual(p.returncode,0,p.stderr)
                # Independent work may satisfy the final artifact, but cannot
                # supply this interrupted planner's delivery/cleanup receipt.
                (self.repo/'artifact').write_text('independent result')
                state = self.finish(self.launch(d),d)
                row = state['planning_execution' if v2 else 'capacity_planning']
                self.assertEqual(state['status'],'capacity_unknown',state)
                self.assertTrue(row['delivery_unknown'])
                self.assertEqual(state['planning_attempts'],0)
                self.assertEqual(state['criteria']['full']['status'],'pending')
                self.assertFalse((self.repo/'planner-entered').exists())
                self.assertEqual(len(self.q.snapshot('fixture')['active']),1)
                self.assertEqual(runner.status(d)['task_view']['effective_status'],'delivery_unknown')
                request = row['capacity_request']['request_id']
                grant = self.q.claim(request)
                # Fixture deliberately crashed before launching any process.
                self.assertTrue(self.q.release(request,grant['token']))

    def test_workflow_planner_release_refusal_does_not_launch_proposed_stage(self):
        d = self.create_workflow(generated=True)
        code = '''import sys
from unittest.mock import patch
import capacity,workflow
with patch.object(capacity.CapacityStore,'release',return_value=False):workflow.Workflow(sys.argv[1]).execute()
'''
        p = subprocess.run([sys.executable,'-B','-c',code,str(d)],cwd=ROOT,env=self.env,
                           capture_output=True,text=True,timeout=8)
        self.assertEqual(p.returncode,0,p.stderr)
        state = runner.read(d/'state.json')
        self.assertEqual(state['status'],'capacity_unknown',state)
        self.assertIsNone(state['active_run'])
        self.assertEqual(state['stages'],[])
        self.assertTrue(state['planning_execution']['delivery_unknown'])
        self.assertEqual(state['planning_attempts'],1)
        self.assertEqual(len(self.q.snapshot('fixture')['active']),1)
        self.assertTrue((self.repo/'planner-entered').exists())
        self.assertFalse((self.repo/'entered').exists())


if __name__ == '__main__':
    unittest.main()
