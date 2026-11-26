"""User closure and once-only unhandled failure ingress, isolated from real queues."""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_interaction as fixtures
from test_interaction import ORIGIN,HUB
import runner
from ato_workflow import workflow
from ato_workflow import workflow_closure
from ato_workflow import control
from ato_workflow import failure_attention
import hub_queue_gate
from ato_workflow import interaction
from ato_workflow import notifications
import recovery

class TaskDispositionTests(unittest.TestCase):
    setUp=fixtures.InteractionTests.setUp
    tearDown=fixtures.InteractionTests.tearDown

    def configure(self):
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            control.enable('User requested control',60,0)
            notifications.configure('user_input_only','Keep normal updates silent')
            failure_attention.configure('User explicitly requested notices for failed tasks with no handler')
        control.register(self.d)

    def row(self):
        with control.database() as con:
            return json.loads(con.execute('select body from workflows where path=?',(str(self.d),)).fetchone()[0])

    def observe(self):
        with patch('ato_workflow.control.send',return_value=dict(state='queued')):
            control.workflow_observation(self.row())
        return [r for r in control.rows(True) if r['kind']=='failure_attention']

    def evidence(self):
        p=self.repo/'user-decision.json'
        runner.save(p,dict(decision='accept_current_delivery_and_close',workflows=[str(self.d)],
                           user_text='Mark these four tasks complete',hub_thread=HUB))
        return p

    def close(self):
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            return workflow_closure.close(self.d,self.evidence(),'Actual user message in hub')

    def test_user_acceptance_preserves_state_contract_stop_and_residuals(self):
        (self.d/'STOP').touch()
        before={n:(self.d/n).read_bytes() for n in ['state.json','contract.json','origin.json']}
        self.close()
        x=runner.status(self.d)
        self.assertEqual(x['status'],'blocked')
        self.assertEqual(x['delivery']['effective_status'],'completed')
        self.assertEqual(x['delivery']['completion_basis'],'user_acceptance')
        self.assertFalse(x['delivery']['automated_acceptance_complete'])
        self.assertEqual(x['delivery']['unresolved_criteria'],['file'])
        self.assertTrue((self.d/'STOP').exists())
        self.assertEqual(before,{n:(self.d/n).read_bytes() for n in before})

    def test_user_close_is_idempotent(self):
        a=self.close(); b=self.close(); self.assertEqual(a,b)

    def test_closure_requires_actual_bound_actor(self):
        p=self.evidence()
        with patch.dict(os.environ,CODEX_THREAD_ID='unrelated'):
            with self.assertRaises(ValueError):workflow_closure.close(self.d,p,'user source')

    def test_closure_rejects_wrong_workflow_mapping(self):
        p=self.evidence();x=runner.read(p);x['workflows']=[];runner.save(p,x)
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            with self.assertRaises(ValueError):workflow_closure.close(self.d,p,'user source')

    def test_active_executor_cannot_be_closed(self):
        s=runner.read(self.d/'state.json');s['owner_pid']=os.getpid();runner.save(self.d/'state.json',s)
        with self.assertRaisesRegex(ValueError,'active'):self.close()

    def test_dead_process_metadata_is_retained_without_claiming_live_execution(self):
        s=runner.read(self.d/'state.json');s['active_process']=dict(pid=99999999,argv=['historical'])
        runner.save(self.d/'state.json',s)
        with patch('ato_workflow.workflow_closure.recovery.alive',return_value=False), patch('ato_workflow.workflow_closure.os.killpg',side_effect=ProcessLookupError):
            self.close()
        self.assertEqual(runner.read(self.d/'state.json')['active_process']['pid'],99999999)

    def test_orphan_group_blocks_closure_even_after_leader_exits(self):
        s=runner.read(self.d/'state.json');s['active_process']=dict(pid=99999999,argv=['historical'])
        runner.save(self.d/'state.json',s)
        with patch('ato_workflow.workflow_closure.recovery.alive',return_value=False),patch('ato_workflow.workflow_closure.os.killpg',return_value=None):
            with self.assertRaisesRegex(ValueError,'active'):self.close()

    def test_closed_task_cannot_resume_or_receive_late_question(self):
        self.close()
        with self.assertRaisesRegex(ValueError,'closed'):runner.ensure_resumable(self.d)
        with self.assertRaisesRegex(ValueError,'closed'):interaction.binding(self.d)

    def test_changed_evidence_revokes_completion_projection(self):
        self.close();self.evidence().write_text('{}')
        x=runner.status(self.d)['delivery']
        self.assertEqual(x['validation'],'invalid');self.assertEqual(x['effective_status'],'blocked')

    def test_changed_state_revokes_completion_projection(self):
        self.close();s=runner.read(self.d/'state.json');s['error']='changed';runner.save(self.d/'state.json',s)
        self.assertEqual(runner.status(self.d)['delivery']['validation'],'invalid')

    def test_new_workflow_auto_registers_without_enabling_recovery(self):
        self.configure()
        d=workflow.create(self.repo,self.repo/'input.json',ORIGIN)
        with control.database() as con:
            self.assertIsNotNone(con.execute('select body from workflows where path=?',(str(d),)).fetchone())
        self.assertFalse((d/'recovery.json').exists())

    def test_current_runtime_origin_is_used_not_latest_session(self):
        self.configure();d=workflow.create(self.repo,self.repo/'input.json')
        o=runner.read(d/'origin.json');self.assertEqual(o['origin_thread_id'],ORIGIN)
        self.assertIn('current runtime',o['evidence'])

    def test_missing_runtime_origin_is_visible_not_guessed(self):
        self.configure()
        with patch.dict(os.environ,CODEX_THREAD_ID=''):
            d=workflow.create(self.repo,self.repo/'input.json')
        self.assertIsNone(runner.read(d/'origin.json')['origin_thread_id'])
        self.assertEqual(runner.read(d/'observation-registration.json')['status'],'failed')

    def test_unhandled_failure_is_a_specific_once_only_notice(self):
        self.configure();a=self.observe()[0]
        self.assertTrue(notifications.disposition(a)['notify'])
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):control.record(a['id'],state='presented',note='Actual status message')
        a=next(x for x in control.rows(True) if x['id']==a['id'])
        self.assertFalse(notifications.disposition(a)['notify'])
        self.assertEqual(len(self.observe()),1)

    def test_normal_status_stays_silent(self):
        self.configure()
        for kind in ['workflow','host_handoff','operational','watchdog','workflow_result']:
            self.assertFalse(notifications.disposition(dict(kind=kind,workflow=str(self.d)))['notify'])

    def test_no_notice_for_stop(self):
        self.configure();(self.d/'STOP').touch();self.assertEqual(self.observe(),[])

    def test_no_notice_for_manual_or_host_wait(self):
        self.configure()
        for status in ['needs_input','needs_host','waiting']:
            s=runner.read(self.d/'state.json');s['status']=status;runner.save(self.d/'state.json',s)
            self.assertEqual(self.observe(),[])

    def test_unknown_recovery_delivery_is_not_a_missing_handler_claim(self):
        self.configure();runner.save(self.d/'recovery.json',dict(requests=[dict(state='delivery_unknown')]))
        self.assertEqual(self.observe(),[])

    def test_active_owner_suppresses_failure(self):
        self.configure();s=runner.read(self.d/'state.json');s['owner_pid']=os.getpid();runner.save(self.d/'state.json',s)
        self.assertEqual(self.observe(),[])

    def test_old_registry_rows_do_not_flood_new_policy(self):
        self.configure();row=self.row();row['registered_at']=failure_attention.policy()['configured_at']-1
        failure_attention.observe(row,self.d,runner.read(self.d/'state.json'),runner.read(self.d/'contract.json'))
        self.assertFalse(any(a['kind']=='failure_attention' for a in control.rows(True)))

    def test_gate_requires_canonical_claim_and_exact_payload(self):
        self.configure();a=self.observe()[0]
        text='[ctask-followup id='+a['id']+' reminder=0]\nActual bounded failure notice'
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            self.assertFalse(hub_queue_gate.pending_failure_notice(text,HUB))
            with control.database() as con,con:
                con.execute('insert into outbox values(?,?)',('followup:'+a['id']+':0',json.dumps(dict(state='dispatching',target=HUB,message_sha256=hashlib.sha256(text.encode()).hexdigest()))))
            self.assertTrue(hub_queue_gate.pending_failure_notice(text,HUB))
            self.assertFalse(hub_queue_gate.pending_failure_notice(text+' forged',HUB))
            self.assertFalse(hub_queue_gate.pending_failure_notice(text.replace('reminder=0','reminder=1'),HUB))

    def test_reconstructed_controller_does_not_resend(self):
        self.configure()
        with patch('ato_workflow.control.send',return_value=dict(state='queued',message_id='fixture')) as send:
            control.tick();control.tick()
            notices=[c for c in send.call_args_list if c.args[0].startswith('followup:failure-')]
        self.assertEqual(len(notices),1)

    def test_user_closure_closes_failure_and_host_followups_truthfully(self):
        self.configure();a=self.observe()[0];self.close()
        with patch('ato_workflow.control.send',return_value=dict(state='queued')):control.tick()
        a=next(r for r in control.rows(True) if r['id']==a['id'])
        self.assertEqual(a['state'],'completed')
        self.assertEqual(a['delivery']['completion_basis'],'user_acceptance')
        self.assertEqual(runner.read(self.d/'state.json')['status'],'blocked')

    def test_true_automated_completion_still_produces_result(self):
        self.configure();s=runner.read(self.d/'state.json');s['status']='completed';runner.save(self.d/'state.json',s)
        control.workflow_observation(self.row())
        self.assertTrue(any(a['kind']=='workflow_result' for a in control.rows(True)))
