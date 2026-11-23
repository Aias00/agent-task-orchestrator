"""Offline, isolated behavioral checks for the reference hardening candidate."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agents
import agent_registry
import runner
import task_trace
import workflow
from acp_outcome import Outcome
from message_bus import MessageBus, ConflictError, IdentityError

ROOT = Path(__file__).resolve().parents[1]
from legacy_support import LEGACY_CLI


class WorkflowContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.root/'codex')}); env.start(); self.addCleanup(env.stop)

    def create(self, manual=False):
        criterion = dict(id='x', description='Required evidence', kind='manual') if manual else dict(id='x', description='Required evidence', checks=[['false']])
        contract = dict(objective='Complete all criteria', scope='fixture only', authorization='fixture only', memory=False, acceptance=[criterion])
        path = self.root/'input.json'; runner.save(path, contract)
        return workflow.create(self.repo, path)

    @staticmethod
    def failed_check(obj):
        obj.state['criteria']['x'] = dict(status='failed', evidence=[])
        obj.persist()

    @staticmethod
    def stage_completed(obj, retry, host_checks=False):
        obj.state['stages'][-1]['status'] = 'completed'
        obj.state['active_run'] = None
        obj.persist()
        return True

    def execute_no_progress(self, directory):
        proposal = dict(decision='stage', reason='repair', retry_after_seconds=1,
                        tasks=[dict(id='fix', prompt='repair fixture', depends_on=[], covers=['x'])])
        with patch.object(workflow.Workflow, 'check_total', self.failed_check), \
             patch.object(workflow.Workflow, 'run_active_stage', self.stage_completed), \
             patch.object(workflow.Workflow, 'propose', return_value=proposal), \
             patch.object(workflow.Workflow, 'memory_recall', return_value=''):
            workflow.Workflow(directory).execute()
        return runner.read(directory/'state.json')

    def test_successful_stage_with_unmet_total_is_incomplete(self):
        d = self.create(); state = self.execute_no_progress(d)
        self.assertEqual(state['stages'][0]['status'], 'completed')
        self.assertEqual(state['criteria']['x']['status'], 'failed')
        self.assertNotEqual(state['status'], 'completed')

    def test_identical_no_progress_continuation_is_not_spawned(self):
        d = self.create(); state = self.execute_no_progress(d)
        self.assertEqual(len(state['stages']), 1)
        self.assertEqual(state['status'], 'blocked')
        self.assertIn('no observable acceptance progress', state['error'])

    def test_continuation_dedup_survives_reconstruction(self):
        d = self.create(); first = self.execute_no_progress(d)
        second = self.execute_no_progress(d)
        self.assertEqual([s['run_dir'] for s in second['stages']], [s['run_dir'] for s in first['stages']])
        self.assertEqual(second['status'], 'blocked')

    def test_waiting_manual_acceptance_creates_no_stage(self):
        d = self.create(manual=True)
        with patch.object(workflow.Workflow, 'memory_recall', return_value=''):
            workflow.Workflow(d).execute()
        state = runner.read(d/'state.json')
        self.assertEqual(state['status'], 'needs_input')
        self.assertEqual(state['stages'], [])

    def test_stop_prevents_workflow_execution(self):
        d = self.create(); (d/'STOP').touch()
        with self.assertRaises(runner.Halt):
            workflow.Workflow(d).execute()
        self.assertEqual(runner.read(d/'state.json')['stages'], [])

    def test_state_is_reloaded_after_acquiring_owner_lock(self):
        d = self.create(manual=True); stale = workflow.Workflow(d)
        state = runner.read(d/'state.json'); state['planning_attempts'] = 7; runner.save(d/'state.json', state)
        with patch.object(workflow.Workflow, 'memory_recall', return_value=''):
            stale.execute()
        self.assertEqual(runner.read(d/'state.json')['planning_attempts'], 7)

    def test_persisted_stage_is_owned_once_after_reconstruction(self):
        import fcntl
        d = self.create(); obj = workflow.Workflow(d)
        plan = dict(objective='fixture', tasks=[dict(id='fix', prompt='fixture', checks=[['true']])])
        obj.stage(plan, ['x'])
        path = runner.read(d/'state.json')['active_run']
        lockfile = self.repo/'.codex/tasks/.worker.lock'
        with lockfile.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError): workflow.Workflow(d).execute()
        with patch.object(workflow.Workflow, 'run_active_stage', self.stage_completed), \
             patch.object(workflow.Workflow, 'check_total', self.failed_check), \
             patch.object(workflow.Workflow, 'propose', return_value=dict(decision='blocked', reason='fixture decision')), \
             patch.object(workflow.Workflow, 'memory_recall', return_value=''):
            workflow.Workflow(d).execute()
        state = runner.read(d/'state.json')
        self.assertEqual(len(state['stages']), 1)
        self.assertEqual(state['stages'][0]['run_dir'], path)
        self.assertEqual(state['stages'][0]['status'], 'completed')


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = self.root/'bus.sqlite'; self.now = 100
        self.bus = self.restart()
        self.a = self.bus.register_endpoint('a', kind='codex', session_id='s1', project=self.root, source='fixture')
        self.b = self.bus.register_endpoint('b', kind='claude', session_id='s2', project=self.root, source='fixture')
        self.route = self.bus.bind_route(sender='a', target='b', task_id='t', contract_hash='h', source='fixture')['id']
        self.owner = self.bus.acquire_endpoint(self.b, owner='one', lease_seconds=10)

    def restart(self):
        return MessageBus(self.path, clock=lambda: self.now)

    def enqueue(self, key='one'):
        return self.bus.enqueue(self.a, binding_id=self.route, target='b', kind='request', body='fixture', idempotency_key=key, task_id='t', contract_hash='h')

    def dispatch(self):
        msg = self.bus.claim(self.b, self.owner, lease_seconds=5)
        return self.bus.begin_dispatch(self.b, self.owner, msg['id'], msg['claim_token'])

    def test_duplicate_ack_survives_reconstruction(self):
        self.enqueue(); msg = self.dispatch()
        self.bus.accept(self.b, msg['id'], receipt_id='r1')
        self.bus = self.restart(); self.bus.accept(self.b, msg['id'], receipt_id='r1')
        self.assertEqual(len(self.bus.receipts(msg['id'])), 1)
        self.assertEqual(self.bus.message(msg['id'])['state'], 'accepted')

    def test_unknown_delivery_is_not_resent_after_restart(self):
        self.enqueue(); msg = self.dispatch(); self.now += 11
        self.bus = self.restart(); self.bus.recover()
        owner = self.bus.acquire_endpoint(self.b, owner='two')
        self.assertEqual(self.bus.message(msg['id'])['state'], 'delivery_unknown')
        self.assertIsNone(self.bus.claim(self.b, owner))

    def test_competing_owner_cannot_claim_operation(self):
        self.enqueue()
        with self.assertRaises(ConflictError):
            self.restart().acquire_endpoint(self.b, owner='two')
        self.assertIsNotNone(self.bus.claim(self.b, self.owner))

    def test_stale_attempt_cannot_finish_current_dispatch(self):
        self.enqueue(); msg = self.dispatch()
        with self.assertRaises(IdentityError):
            self.restart().finish_dispatch(msg['id'], 'attempt-obsolete', state='delivered')
        self.assertEqual(self.bus.message(msg['id'])['state'], 'dispatching')

    def test_cancelled_message_cannot_be_revived_by_late_receipt(self):
        self.enqueue(); msg = self.dispatch()
        self.bus.cancel(self.a, msg['id'], source='fixture stop')
        self.restart().accept(self.b, msg['id'], receipt_id='late')
        self.assertEqual(self.bus.message(msg['id'])['state'], 'cancelled')
        self.assertEqual(self.bus.receipts(msg['id'])[0]['late'], 1)

    def test_superseded_generation_receipt_cannot_mutate_new_message(self):
        self.enqueue(); old = self.dispatch()
        self.bus.set_binding_state(self.route, 'superseded', source='fixture generation changed')
        new_route = self.bus.bind_route(sender='a', target='b', task_id='t', contract_hash='new-h', source='fixture newer generation')['id']
        new = self.bus.enqueue(self.a, binding_id=new_route, target='b', kind='request', body='new', idempotency_key='new', task_id='t', contract_hash='new-h')
        self.restart().accept(self.b, old['id'], receipt_id='old-session-receipt')
        self.assertEqual(self.bus.message(old['id'])['state'], 'cancelled')
        self.assertEqual(self.bus.message(new['id'])['state'], 'queued')


class TraceAndCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)

    def add(self, kind, ident, **kw):
        return task_trace.append(self.root, kind, event_id=ident, task='task', **kw)

    def test_native_resume_is_explicitly_unsupported_for_real_adapters(self):
        for kind in ('claude', 'pi'):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError): agent_registry.get(kind, 'native_resume')
                self.assertNotIn('session_observe', agent_registry.get(kind).capabilities)

    def test_doctor_reports_capabilities_without_claiming_readiness(self):
        with patch.object(agents, 'executable', side_effect=ValueError('fixture unavailable')):
            rows = {row['kind']: row for row in agents.doctor()}
        self.assertEqual(rows['claude']['native_resume'], 'unsupported')
        self.assertEqual(rows['pi']['native_resume'], 'unsupported')
        self.assertIn('unknown', rows['pi']['readiness'])
        self.assertIn('worker', rows['claude']['capabilities'])

    def test_old_session_receipt_cannot_settle_current_attempt(self):
        outcome = Outcome('current-session', 'current-turn')
        self.assertFalse(outcome.native_receipt('old-session', 'current-turn', 'stop', trusted=True))
        self.assertFalse(outcome.transport_receipt('current-session', 'old-turn', 'end_turn'))
        self.assertEqual(outcome.verdict, 'unverified')

    def test_end_turn_without_trusted_native_success_is_unverified(self):
        outcome = Outcome('s', 't'); outcome.transport_receipt('s', 't', 'end_turn'); outcome.checks = 'passed'
        self.assertEqual(outcome.verdict, 'unverified')

    def test_tool_transitions_keep_identity_in_cli_trace(self):
        for i, state in enumerate(('pending', 'in_progress', 'completed')):
            self.add('tool', str(i), attempt=1, session='s', turn='t', tool='tool-1', state=state, source='acp')
        data = json.loads(subprocess.check_output([sys.executable, str(LEGACY_CLI), 'trace', str(self.root), '--json'], text=True))
        self.assertEqual(data['summary']['tool_call_count'], 1)
        self.assertEqual(len({event['tool_id'] for event in data['events']}), 1)

    def test_late_old_attempt_does_not_replace_current_trace_summary(self):
        self.add('attempt_started', 'start1', attempt=1)
        self.add('attempt_started', 'start2', attempt=2)
        self.add('native_unverified', 'current', attempt=2, session='new', source='native')
        self.add('native_success', 'late', attempt=1, session='old', source='native')
        summary = task_trace.query(self.root)['summary']
        self.assertEqual(summary['latest_attempt'], 2)
        self.assertEqual(summary['native'], 'native_unverified')

    def test_trace_is_redacted_bounded_and_exposes_unknown_and_drops(self):
        with patch.object(task_trace, 'MAX_EVENTS', 2):
            self.add('tool', 'one', attempt=1, tool='secret-token', state='unexpected-provider-state', target='../secret-token', workspace=self.root)
            self.add('approval', 'two', attempt=1, state='cancelled')
            self.assertFalse(self.add('progress', 'three', attempt=1))
        data = task_trace.query(self.root)
        self.assertNotIn('secret-token', json.dumps(data))
        self.assertEqual(data['summary']['dropped'], 1)
        self.assertEqual(data['events'][0]['state'], 'unknown')
        self.assertEqual(data['events'][1]['kind'], 'approval')


if __name__ == '__main__': unittest.main()
