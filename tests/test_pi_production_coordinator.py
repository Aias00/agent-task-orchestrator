"""O7/O8/O10: real guard/IPC and owned processes; only synthetic ACP/model data."""
import asyncio
import hashlib
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_registry as registry
from ato_common.ato_core import digest
from ato_workflow import session_router
from ato_pi.pi_production_profile import ProductionPiProfile
from ato_pi.pi_session_coordinator import PiCoordinator,PiSessionAdapter
from ato_pi.pi_session_profile import Launch
from ato_pi.pi_toolchain import locate
from test_pi_production_profile import configuration,reviewed

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'tests/fixtures/pi_coordinator_fixture.py'


class Owner:
    def __init__(self,path):
        self.dir=path;self._accounted_at=time.time();self.calls=0;self.blocked=False
    def guard(self):
        self.calls+=1
        if self.blocked:raise ValueError('synthetic existing owner budget rejection')


class ProductionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):cls.node=locate('node')

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.project=self.root/'project';self.project.mkdir()
        self.env=patch.dict(os.environ,dict(HOME=str(self.root/'home'),PATH='/usr/bin:/bin',
            ATO_HOME=str(self.root/'ato'),CODEX_HOME=str(self.root/'native'),FIXTURE_KEY='synthetic-never-log'),clear=True)
        self.env.start();self.addCleanup(self.env.stop)
        self.workflow=self.project/'.codex/tasks/workflows/production-fixture';self.workflow.mkdir(parents=True)
        self.owner=Owner(self.workflow)
        adapters=patch.dict(registry._adapters)
        adapters.start();self.addCleanup(adapters.stop)
        self.adapter=PiSessionAdapter(kind='pi_acp_production');registry.register(self.adapter)
        self.host=None

    async def asyncTearDown(self):
        if self.host:await self.host.close()
        # Apply the same sentinel check to cancel, EOF, STOP and failure paths.
        for p in self.root.rglob('*'):
            if p.is_file() and not p.is_symlink():
                self.assertNotIn(b'synthetic-never-log',p.read_bytes(),str(p))

    async def start(self, *, orphan=False):
        c=configuration();profile=ProductionPiProfile(c,reviewed=reviewed(),permit=lambda *_:True)
        def launch(p,runtime,project,sandbox):
            fixture=FIXTURE
            if orphan:
                fixture=runtime/'orphan-fixture.py'
                child="import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('child-ready').touch();time.sleep(30)"
                fixture.write_text('import subprocess,sys,runpy,time\nfrom pathlib import Path\n'
                    +'p=subprocess.Popen([sys.executable,"-c",'+repr(child)+'])\n'
                    +'Path(sys.argv[1],"grandchild.pid").write_text(str(p.pid))\n'
                    +'\nfor _ in range(100):\n if Path("child-ready").exists():break\n time.sleep(.01)\n'
                    +'runpy.run_path('+repr(str(FIXTURE))+',run_name="__main__")\n')
            return Launch((sys.executable,str(fixture),str(runtime)),dict(HOME=str(runtime),PATH='/usr/bin:/bin'),
                'synthetic production lifecycle fixture; no real Pi/provider')
        with patch.object(ProductionPiProfile,'launch',launch):
            self.host=PiCoordinator(profile,self.project,kind='pi_acp_production',production_owner=self.owner,timeout=3)
            identity=await self.host.start()
        self.adapter.attach(self.host);self.reference=session_router.register_v2(identity)
        self.contract=dict(objective='synthetic full goal')
        self.state=dict(repo=str(self.project),contract_hash=digest(self.contract),status='blocked',
            owner_pid=None,active_process=None,active_run=None,execution_revision=0)
        (self.workflow/'contract.json').write_text(json.dumps(self.contract))
        (self.workflow/'state.json').write_text(json.dumps(self.state))
        (self.workflow/'origin.json').write_text(json.dumps(dict(origin_session_ref=self.reference.reference)))
        self.binding=self.host.bind_workflow(self.workflow)
        self.endpoint=dict(kind=self.adapter.kind,session_id=identity.native_id,project=str(self.project))

    async def dispatch(self,body='cancel-me',identifier='one'):
        message=dict(id=identifier,canonical_id=identifier,correlation_id=identifier,task_id=str(self.workflow),
            contract_hash=digest(self.contract),generation='[null, 0]',protocol=2,body=body,expires_at=None)
        result=await self.host.dispatch(self.endpoint,message)
        deadline=asyncio.get_running_loop().time()+2
        while not (self.host.runtime/'tool-authority.json').exists() and not self.host.closed.is_set():
            if asyncio.get_running_loop().time()>deadline:raise AssertionError('authority was not prepared')
            await asyncio.sleep(.01)
        return result

    async def guard(self, *, profile=None, tool='read'):
        code="""import guard from './pi-coordinator-guard.mjs';
let callback;guard({on(name,fn){if(name==='tool_call')callback=fn;}});
const result=await callback({toolName:process.env.FIXTURE_TOOL,input:{path:'safe.txt'}});
console.log(JSON.stringify({blocked:result?.block===true}));
"""
        from ato_pi.pi_session_coordinator import control_socket
        from ato_pi.pi_toolchain import production_sandbox
        env=dict(HOME=str(self.root),PATH='/usr/bin:/bin',CTASK_PI_RUNTIME=str(self.host.runtime),
            CTASK_AGENT_REPO=str(self.project),CTASK_AGENT_SANDBOX='read-only',FIXTURE_TOOL=tool,
            ATO_PI_PROFILE_HASH=profile or self.host.profile.fingerprint,
            ATO_PI_CONTROL_SOCKET=str(control_socket(self.host.runtime)))
        policy=production_sandbox(self.host.runtime,self.project,self.node,sources=ROOT,proxy_port=12345,
            control_socket=control_socket(self.host.runtime),workflow=self.workflow)
        p=await asyncio.create_subprocess_exec('/usr/bin/sandbox-exec','-p',policy,str(self.node),'--input-type=module','-e',code,cwd=ROOT,env=env,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(p.communicate(),4)
        self.assertEqual(p.returncode,0,err.decode());self.assertNotIn(b'synthetic-never-log',out+err)
        return json.loads(out)['blocked']

    async def test_every_tool_real_node_guard_calls_owner_and_rejects_shell_and_wrong_profile(self):
        await self.start();(self.project/'safe.txt').write_text('safe')
        self.assertEqual((await self.dispatch()).state,'delivered')
        before=self.owner.calls;self.assertFalse(await self.guard());self.assertGreater(self.owner.calls,before)
        self.assertFalse(await self.guard())
        self.assertTrue(await self.guard(tool='bash'))
        self.assertTrue(await self.guard(profile='stale-profile'))
        self.assertEqual(self.host.production_fence.snapshot()['requests'],0)
        await self.host.cancel()

    async def test_stop_during_slow_request_closes_owned_group_and_does_not_complete(self):
        await self.start();await self.dispatch()
        (self.workflow/'STOP').touch()
        await asyncio.wait_for(self.host.closed.wait(),3)
        self.assertIsNotNone(self.host.proc.returncode)
        self.assertNotEqual(self.host.state['turns']['one']['state'],'turn_completed')
        self.assertFalse(self.host._production_members())

    async def test_budget_rejection_and_hard_deadline_are_checked_without_tool_calls(self):
        await self.start();await self.dispatch();self.owner.blocked=True
        await asyncio.wait_for(self.host.closed.wait(),3)
        self.assertTrue(self.host.production_fence.revoked)
        self.assertFalse(self.host._production_members())

    async def test_monotonic_hard_deadline_stops_even_with_advisory_owner(self):
        await self.start();await self.dispatch()
        self.host.production_fence.deadline=time.monotonic()-1
        await asyncio.wait_for(self.host.closed.wait(),3)
        self.assertFalse(self.host._production_members())
        self.assertTrue(self.host.production_fence.revoked)

    async def test_generation_change_closes_host_and_stale_guard_never_performs_tool(self):
        await self.start();await self.dispatch();self.state['execution_revision']=1
        (self.workflow/'state.json').write_text(json.dumps(self.state))
        self.assertTrue(await self.guard())
        await asyncio.wait_for(self.host.closed.wait(),3)

    async def test_raw_origin_change_revokes_before_tool_even_without_generation_change(self):
        await self.start();await self.dispatch();(self.workflow/'origin.json').write_text('{}')
        self.assertTrue(await self.guard())
        await asyncio.wait_for(self.host.closed.wait(),3)
        self.assertTrue(self.host.production_fence.revoked)

    async def test_child_exit_wakes_host_and_reaps_term_ignoring_grandchild(self):
        await self.start(orphan=True)
        await self.dispatch('disconnect')
        await asyncio.wait_for(self.host.closed.wait(),4)
        self.assertFalse(self.host._production_members())
        self.assertEqual(self.host.state['lifecycle'],'stopped')
        self.assertNotEqual(self.host.state['turns']['one']['state'],'turn_completed')

    async def test_cancel_pipe_backpressure_is_bounded_and_still_cleans_owned_group(self):
        await self.start();await self.dispatch()
        async def stalled(_):await asyncio.Future()
        started=time.monotonic()
        with patch.object(self.host,'_write',stalled),self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.host.cancel(),2)
        self.assertLess(time.monotonic()-started,1.5)
        self.assertTrue(self.host.closed.is_set())
        self.assertFalse(self.host._production_members())
        self.assertNotEqual(self.host.state['turns']['one']['state'],'turn_completed')

    async def test_group_exit_between_membership_sample_and_signal_requires_empty_recheck(self):
        await self.start();await self.dispatch()
        members=self.host._production_members;killpg=os.killpg;cohort={};calls=0
        def sampled():
            nonlocal calls
            calls+=1
            if calls==1:cohort.update(members());return dict(cohort)
            if calls==2:return dict(cohort)  # Simulate a stale sample after TERM.
            return members()
        def signal_group(pid,sig):
            if sig==signal.SIGKILL:raise ProcessLookupError('synthetic already-exited owned group')
            return killpg(pid,sig)
        with patch.object(self.host,'_production_members',sampled),patch.object(os,'killpg',signal_group):
            await self.host.close()
        self.assertGreaterEqual(calls,3)
        self.assertFalse(members())
        self.assertEqual(self.host.state['lifecycle'],'stopped')

    async def test_native_completion_is_rechecked_and_secrets_are_not_persisted(self):
        await self.start();await self.dispatch('fixture hello');await self.host.drain()
        self.assertEqual(self.host.state['turns']['one']['state'],'turn_completed')
        for p in self.root.rglob('*'):
            if p.is_file() and not p.is_symlink():
                self.assertNotIn(b'synthetic-never-log',p.read_bytes(),str(p))
        self.assertEqual(self.adapter.session_capabilities_v2['session.load_resume']['state'],'unsupported')
        with self.assertRaises(ValueError):
            await self.adapter.reconstruct_session(None,None,None,actor=None,workflow_id='',contract_hash='',generation=0)

    async def test_profile_or_owner_missing_never_adopts_local_or_old_tasks(self):
        p=ProductionPiProfile(configuration(),reviewed=reviewed(),permit=lambda *_:True)
        with self.assertRaises(ValueError):PiCoordinator(p,self.project)
        with self.assertRaises(ValueError):PiCoordinator(p,self.project,kind='pi_acp_production')
        with self.assertRaises(ValueError):PiCoordinator(p,self.project,kind='pi_acp_production',
            production_owner=self.owner,bindings=dict(workflow_id='old-task'))

    async def test_owner_directory_must_be_one_exact_absolute_workflow_before_launch(self):
        for directory in (Path('relative'),self.project,self.workflow.parent,self.root/'other-workflow'):
            p=ProductionPiProfile(configuration(),reviewed=reviewed(),permit=lambda *_:True)
            with self.subTest(directory=directory),self.assertRaisesRegex(ValueError,'exact project workflow'):
                PiCoordinator(p,self.project,kind='pi_acp_production',production_owner=Owner(directory))

    async def test_production_provisioning_rejects_prepared_local_before_native_creation(self):
        from ato_pi.pi_session_profile import LocalPiProfile
        from ato_pi.pi_session_coordinator import provision_path
        from ato_workflow.transport import _private_save
        models=dict(providers={'fixture':dict(baseUrl='http://127.0.0.1:19321',apiKey='ato-local-dummy',
            api='openai-completions',models=[{'id':'scripted'}])})
        local=PiCoordinator(LocalPiProfile(models,'fixture/scripted'),self.project)
        self.addAsyncCleanup(local.close)
        _private_save(provision_path(str(self.project)),dict(host_id=local.host_id))
        with self.assertRaisesRegex(ValueError,'may not adopt'):
            self.adapter.create_session(self.project,dict(sandbox='read-only'),'fixture','id',lambda **_:None)
        self.assertIsNone(local.proc)

    async def test_configuration_file_in_workspace_fails_before_launch(self):
        p=self.project/'production.json';p.write_text(json.dumps(configuration()));p.chmod(0o600)
        profile=ProductionPiProfile.from_file(p,reviewed=reviewed(),permit=lambda *_:True)
        with self.assertRaisesRegex(ValueError,'configuration enters'):
            profile.launch(self.root/'unused-runtime',self.project,'read-only')
