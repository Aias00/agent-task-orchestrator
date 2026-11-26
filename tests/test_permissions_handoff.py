import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from ato_adapters import agents
from ato_workflow import control
from ato_workflow import permissions
import runner
from ato_workflow import workflow
import test_interaction as interaction_fixtures
from test_interaction import ORIGIN, HUB
import test_workflow as workflow_fixtures
import test_delegation as delegation_fixtures
from ato_adapters import appserver
from ato_workflow import delegation
from legacy_support import LEGACY_CLI


FAKE = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
if 'sandbox' in sys.argv:
 print(json.dumps({'passed':os.environ.get('FAIL_PROBE')!='1'}))
 sys.exit(1 if os.environ.get('FAIL_PROBE')=='1' else 0)
prompt=sys.stdin.read()
status=os.environ.get('OUTCOME','ready_for_checks')
p=Path('worker-calls');p.write_text(str(int(p.read_text())+1) if p.exists() else '1')
if status!='needs_host':Path('first').write_text('yes')
out=Path(sys.argv[sys.argv.index('-o')+1])
out.write_text(json.dumps(dict(status=status,summary='Implementation ready',
 next_step='Origin must install the already authorized local fixture',blocker='',retry_after_seconds=1)))
'''


class PermissionAndHandoffTests(unittest.TestCase):
    setUp = workflow_fixtures.WorkflowTests.setUp
    tearDown = workflow_fixtures.WorkflowTests.tearDown
    criterion = workflow_fixtures.WorkflowTests.criterion
    contract = workflow_fixtures.WorkflowTests.contract
    create = workflow_fixtures.WorkflowTests.create

    def fixture(self):
        self.fake.write_text(FAKE)

    def execute(self, directory, outcome='ready_for_checks', **env):
        result = subprocess.run([sys.executable, str(LEGACY_CLI), 'run', str(directory)],
            env=dict(self.env, OUTCOME=outcome, **env), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return runner.read(directory/'state.json')

    def test_legacy_policy_and_readonly_planner_do_not_gain_worker_grants(self):
        legacy = agents.command({}, self.repo, 'a', self.repo/'s', self.repo/'o')
        self.assertNotIn('sandbox_workspace_write.network_access=true', legacy)
        plan = {'permissions': {'source': 'Actual fixture authorization', 'network_access': True,
                                'writable_roots': [str(self.repo)]}}
        argv = agents.command(plan, self.repo, 'a', self.repo/'s', self.repo/'o')
        self.assertIn('sandbox_workspace_write.network_access=true', argv)
        self.assertIn('sandbox_workspace_write.writable_roots='+json.dumps([str(self.repo.resolve())]), argv)
        readonly = agents.command(plan, self.repo, 'a', self.repo/'s', self.repo/'o', sandbox='read-only')
        self.assertEqual(readonly[readonly.index('--sandbox')+1], 'read-only')
        self.assertFalse(any('sandbox_workspace_write.' in s for s in readonly))

    def test_full_access_is_explicit_and_not_available_to_external_adapters(self):
        with self.assertRaises(ValueError): permissions.validate({'sandbox':'danger-full-access'})
        plan={'sandbox':'danger-full-access','permissions':{'source':'Actual bounded user authorization'}}
        argv=agents.command(plan,self.repo,'a',self.repo/'s',self.repo/'o')
        self.assertEqual(argv[argv.index('--sandbox')+1],'danger-full-access')
        for kind in ['claude','pi']:
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                permissions.validate(dict(plan,agent={'kind':kind}))

    def test_invalid_or_misleading_grants_are_rejected(self):
        for plan in [
            {'permissions':{'network_access':True}},
            {'permissions':{'source':'x','network_access':'true'}},
            {'permissions':{'source':'x','writable_roots':['relative']}},
            {'permissions':{'source':'x','writable_roots':[str(self.repo/'missing')]}},
            {'sandbox':'read-only','permissions':{'source':'x','network_access':True}},
            {'sandbox':'danger-full-access','permissions':{'source':'x','network_access':False}},
            {'permissions':{'source':'x','requirements':['unknown']}},
            {'host_handoff':{'source':'x'}},
        ]:
            with self.subTest(plan=plan),self.assertRaises(ValueError):permissions.validate(plan)

    def test_contract_propagates_permissions_and_rejects_preset_override(self):
        c=self.contract(keys=('first',))
        c['permissions']={'source':'Fixture user allows local listener','network_access':True}
        d=self.create(c);w=workflow.Workflow(d)
        w.stage(c['stages'][0]['plan'],['first'])
        child=runner.read(Path(w.state['active_run'])/'plan.json')
        self.assertEqual(child['permissions'],c['permissions'])
        bad=copy.deepcopy(c);bad['stages'][0]['plan']['permissions']={'source':'escalation','network_access':False}
        with self.assertRaises(ValueError):workflow.validate(bad)

    def test_failed_capability_probe_spends_no_model_attempt(self):
        self.fixture();c=self.contract(keys=('first',))
        c['permissions']={'source':'Fixture','network_access':True,'requirements':['loopback']}
        d=self.create(c);s=self.execute(d,FAIL_PROBE='1')
        self.assertEqual(s['status'],'permission_blocked')
        child=runner.read(Path(s['active_run'])/'state.json')
        self.assertEqual(child['tasks']['first']['attempts'],0)
        self.assertFalse((self.repo/'worker-calls').exists())
        self.assertFalse(child['permission_check']['passed'])

    def test_ready_for_checks_runs_real_host_acceptance(self):
        self.fixture();d=self.create(self.contract(keys=('first',)))
        s=self.execute(d)
        self.assertEqual(s['status'],'completed')
        self.assertEqual((self.repo/'worker-calls').read_text(),'1')
        self.assertTrue(s['criteria']['first']['evidence'])

    def test_ready_for_checks_cannot_bypass_failing_acceptance(self):
        self.fixture();c=self.contract(keys=('first',))
        c['acceptance'][0]['checks']=[[sys.executable,'-c','raise RuntimeError("real failure")']]
        c['stages'][0]['plan']['tasks'][0]['checks']=c['acceptance'][0]['checks']
        d=self.create(c);self.assertNotEqual(self.execute(d)['status'],'completed')

    def test_host_handoff_does_not_relaunch_worker_or_infer_manual_approval(self):
        self.fixture();c=self.contract(keys=('first',))
        c['host_handoff']={'source':'Original local fixture authorization','instructions':'Origin installs fixture only'}
        c['acceptance'].append({'id':'human','description':'Actual final user decision','kind':'manual'})
        d=self.create(c);s=self.execute(d,'needs_host')
        self.assertEqual(s['status'],'needs_host')
        self.assertEqual((self.repo/'worker-calls').read_text(),'1')
        before=s['elapsed_seconds']; oldhash=s['contract_hash']
        # Retry cannot silently turn a normal handoff into another repair attempt.
        self.assertEqual(self.execute(d,'needs_host')['status'],'needs_host')
        self.assertEqual((self.repo/'worker-calls').read_text(),'1')
        # Failed host checks stay unmet and do not invoke a model.
        w=workflow.Workflow(d);w.execute(host_checks=True,source='Origin attempted fixture installation')
        self.assertEqual(runner.read(d/'state.json')['status'],'needs_host')
        (self.repo/'first').write_text('installed by fixture host')
        w=workflow.Workflow(d);w.execute(host_checks=True,source='Origin actually installed fixture')
        s=runner.read(d/'state.json')
        self.assertEqual(s['status'],'needs_input')
        self.assertEqual(s['criteria']['human']['status'],'needs_input')
        self.assertEqual(s['contract_hash'],oldhash)
        self.assertGreater(s['elapsed_seconds'],before)
        self.assertEqual((self.repo/'worker-calls').read_text(),'1')

    def test_host_checks_refuse_stop_and_unauthorized_handoff(self):
        self.fixture();d=self.create(self.contract(keys=('first',)))
        self.assertEqual(self.execute(d,'needs_host')['status'],'blocked')
        with self.assertRaises(ValueError):workflow.Workflow(d).execute(host_checks=True,source='No grant')
        (d/'STOP').touch()
        with self.assertRaises(runner.Halt):workflow.Workflow(d).execute(host_checks=True,source='Stopped')

    def test_host_checks_charge_the_existing_budget_without_reset(self):
        self.fixture();c=self.contract(keys=('first',))
        c['host_handoff']={'source':'Fixture','instructions':'Only local host checks'}
        d=self.create(c);self.execute(d,'needs_host')
        s=runner.read(d/'state.json');s['elapsed_seconds']=c['limits']['runtime_seconds']
        runner.save(d/'state.json',s)
        (self.repo/'first').write_text('host finished')
        workflow.Workflow(d).execute(host_checks=True,source='Actual host evidence')
        s=runner.read(d/'state.json')
        self.assertEqual(s['status'],'limit')
        self.assertGreaterEqual(s['elapsed_seconds'],c['limits']['runtime_seconds'])
        self.assertEqual((self.repo/'worker-calls').read_text(),'1')


class HandoffRoutingTests(unittest.TestCase):
    setUp=interaction_fixtures.InteractionTests.setUp
    tearDown=interaction_fixtures.InteractionTests.tearDown
    transport=interaction_fixtures.InteractionTests.transport

    def prepare(self):
        c=runner.read(self.repo/'input.json')
        c['host_handoff']={'source':'Fixture user requested continuous delivery','instructions':'Only host fixture checks'}
        runner.save(self.repo/'handoff-input.json',c)
        self.d=workflow.create(self.repo,self.repo/'handoff-input.json',ORIGIN)
        s=runner.read(self.d/'state.json')
        s.update(status='needs_host',host_handoff={'run_dir':'fixture','tasks':{'file':{'next_step':'Install fixture'}}})
        runner.save(self.d/'state.json',s)
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):control.enable('Actual fixture hub follow-up request')
        control.register(self.d)

    def test_normal_handoff_routes_once_even_without_repair_supervisor(self):
        self.prepare()
        with patch('ato_workflow.control.subprocess.run',side_effect=self.transport) as send:
            control.tick();control.tick()
        origin=[c for c in send.call_args_list if '--thread' in c.args[0] and c.args[0][c.args[0].index('--thread')+1]==ORIGIN]
        self.assertEqual(len(origin),1)
        self.assertIn('[ctask-host-handoff',origin[0].args[0][-1])
        self.assertFalse((self.d/'recovery.json').exists())
        self.assertEqual(runner.read(self.d/'state.json')['status'],'needs_host')
        self.assertEqual(len([a for a in control.rows() if a['kind']=='host_handoff']),1)

    def test_unknown_origin_delivery_is_not_resent_and_stop_never_routes(self):
        self.prepare()
        with patch('ato_workflow.control.subprocess.run',side_effect=subprocess.TimeoutExpired('queue',25)) as send:
            control.tick();control.tick()
        origin=[c for c in send.call_args_list if '--thread' in c.args[0] and c.args[0][c.args[0].index('--thread')+1]==ORIGIN]
        self.assertEqual(len(origin),1)
        action=next(a for a in control.rows() if a['kind']=='host_handoff')
        self.assertIn(action['origin_delivery']['state'],['delivery_unknown','dispatching'])
        (self.d/'STOP').touch()
        with patch('ato_workflow.control.subprocess.run') as send:control.tick();send.assert_not_called()

    def test_stop_before_first_observation_prevents_any_handoff(self):
        self.prepare();(self.d/'STOP').touch()
        with patch('ato_workflow.control.subprocess.run') as send:control.tick();send.assert_not_called()
        self.assertFalse(any(a['kind']=='host_handoff' for a in control.rows()))


class DelegatedPermissionTests(unittest.TestCase):
    setUp=delegation_fixtures.DelegationTests.setUp
    tearDown=delegation_fixtures.DelegationTests.tearDown

    def test_new_coordinator_receives_declared_network_and_extra_root(self):
        extra=self.root/'cache';extra.mkdir()
        self.contract['permissions']={'source':'Fixture user granted local tests','network_access':True,
                                      'writable_roots':[str(extra)]}
        runner.save(self.plan,self.contract)
        original=appserver.Client.request;seen=[]
        def capture(client,method,params=None):
            if method=='thread/start':seen.append(params)
            return original(client,method,params)
        with patch('ato_adapters.appserver.Client.request',new=capture), patch('ato_workflow.delegation.start_bridge'):
            result=delegation.delegate(self.repo,self.plan,'Fixture','Actual fixture user requested delegation',timeout=30)
        self.assertIn(result['state'],['queued','awaiting_acceptance'])
        self.assertTrue(seen[0]['config']['sandbox_workspace_write.network_access'])
        self.assertIn(str(extra.resolve()),seen[0]['config']['sandbox_workspace_write.writable_roots'])
