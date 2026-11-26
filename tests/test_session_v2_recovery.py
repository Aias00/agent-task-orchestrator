"""Recovery behavior in bounded temporary repos; no native CLI, cloud or scheduler.

The adapter is an in-process lifecycle fixture, not a qualified vendor adapter.
"""
from dataclasses import replace
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import agent_registry
from ato_adapters import agents
from ato_workflow import budget
from ato_workflow import control
from ato_workflow import failure_attention
from ato_workflow import host_recheck
from ato_workflow import interaction
from ato_workflow.message_bus import ConflictError, IdentityError, MessageBus
from ato_workflow import notifications
import recovery
import runner
from ato_workflow import session_router
from ato_workflow import transport
from ato_workflow import workflow
from ato_review.hub_loop import GuardedWorkflow


class RecoveryAdapter:
    capabilities = frozenset({'transport', 'session_verify', 'session_actor'})

    def __init__(self, fixture):
        self.f = fixture
        self.kind = 'fixture_recovery_' + uuid.uuid4().hex
        self.session_capabilities_v2 = {k: {'state': 'supported', 'evidence': 'isolated lifecycle fixture'}
            for k in ('session.verify', 'session.transport', 'session.observe', 'session.activity',
                      'actor.authenticate', 'session.load_resume')}

    def verify_session(self, identity, project):
        return identity in ('origin-fixture', 'hub-fixture') and project == str(self.f.repo)

    def session_cursor_v2(self, identity):
        return {'offset': len(self.f.events)}

    def observe_session_v2(self, identity, correlation, generation, cursor):
        self.f.observations += 1
        return self.f.events[cursor['offset']:], {'offset': len(self.f.events)}

    def session_activity(self, identity, *, generation):
        return agent_registry.SessionActivity(identity, self.f.activity, 'synthetic owned lifecycle',
            time.time(), generation, self.f.active_turn)

    def authenticate_actor(self, identity, proof, **binding):
        return 'fixture-local-host' if proof.challenge == 'synthetic-proof' else None

    def is_actor(self, identity, project):
        return self.f.actor == identity

    def load_or_resume_session(self, identity, checkpoint, **binding):
        self.f.loads.append(binding)
        return identity

    def send(self, endpoint, message):
        self.f.effects.append((endpoint, message))
        return transport.DeliveryResult(self.f.delivery_result, 'fixture-receipt')


class SessionV2RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-recovery-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        env = patch.dict(os.environ, dict(ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'empty-native'),
            HOME=str(self.root/'empty-home')), clear=False)
        env.start(); self.addCleanup(env.stop)
        for key in ('CTASK_SESSION_REF', 'CODEX_THREAD_ID', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            os.environ.pop(key, None)
        self.events = []; self.effects = []; self.loads = []; self.observations = 0
        self.delivery_result = 'delivered'; self.activity = 'idle'; self.active_turn = ''
        self.actor = 'origin-fixture'
        self.adapter = RecoveryAdapter(self); agent_registry.register(self.adapter)
        self.addCleanup(lambda: agent_registry._adapters.pop(self.adapter.kind, None))
        self.identity = agent_registry.SessionIdentity(self.adapter.kind, 'origin-fixture', str(self.repo), 'synthetic fixture')
        self.origin = session_router.register_v2(self.identity).reference
        self.hub = session_router.register_v2(replace(self.identity, native_id='hub-fixture')).reference
        plan = self.repo/'contract.json'
        runner.save(plan, dict(objective='Complete full fixture goal', scope='Temporary repo only',
            authorization='Synthetic fixture only', memory=False,
            host_handoff=dict(source='synthetic host authorization', instructions='frozen local check only'),
            acceptance=[dict(id='artifact', description='Independent file check',
                checks=[[sys.executable, '-c', 'from pathlib import Path; assert Path("artifact").exists()']]),
                dict(id='human', description='Actual user decision', kind='manual')]))
        self.d = workflow.create(self.repo, plan, origin_ref=self.origin)
        self.set_state(status='blocked', error='original failure', planning_attempts=2, elapsed_seconds=19)
        recovery.enable(self.d, 'Synthetic preexisting bounded recovery grant', response_seconds=60)

    def set_state(self, **changes):
        s = runner.read(self.d/'state.json'); s.update(changes); runner.save(self.d/'state.json', s)
        return s

    def ledger(self):
        return runner.read(self.d/'recovery.json')

    def queued(self):
        self.assertTrue(recovery.tick(self.d))
        return self.ledger()['requests'][-1]

    def event(self, request, kind, **changes):
        values = dict(event_id=uuid.uuid4().hex, session=self.identity, correlation_id=request['id'],
            generation=request['generation'][-1], kind=kind, source='synthetic correlated native event',
            observed_at=time.time(), turn_id='turn-fixture', message_id=request['delivery']['mailbox_id'], text='fixture reply')
        values.update(changes)
        return agent_registry.SessionEvent(**values)

    def claim(self, request):
        recovery.takeover(self.d, request['id'], 'working', 'Inspect synthetic failure')

    def host_resume(self):
        s = runner.read(self.d/'state.json')
        actor = session_router.authenticate_actor(self.origin,
            agent_registry.ActorProof('fixture', 'local:fixture', 'synthetic-proof'), role='session_host',
            project=self.repo, workflow_id=str(self.d), contract_hash=s['contract_hash'],
            generation=s.get('execution_revision', 0))
        return session_router.load_or_resume_v2(self.origin, {}, actor=actor, workflow_id=str(self.d),
            contract_hash=s['contract_hash'], generation=s.get('execution_revision', 0))

    def policy(self):
        self.actor = 'hub-fixture'
        with patch.dict(os.environ, CTASK_SESSION_REF=self.hub):
            interaction.configure_session(self.hub, 'Synthetic user-selected hub')
            control.enable('Synthetic controller grant', 60, 0)
            notifications.configure('user_input_only', 'Synthetic quiet policy')
            failure_attention.configure('Synthetic once-only unhandled failure notice')
        self.actor = 'origin-fixture'
        control.register(self.d)
        with control.database() as con:
            return json.loads(con.execute('select body from workflows where path=?', (str(self.d),)).fetchone()[0])

    def test_stop_blocks_delivery_takeover_and_session_host_without_state_rewrite(self):
        q = self.queued(); before = (self.d/'state.json').read_bytes()
        (self.d/'STOP').touch()
        self.assertFalse(recovery.tick(self.d))
        with self.assertRaisesRegex(ValueError, 'stopped'): self.claim(q)
        with self.assertRaises(ValueError): self.host_resume()
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(before, (self.d/'state.json').read_bytes())
        self.assertTrue((self.d/'STOP').exists())

    def test_expired_request_refuses_takeover_and_remains_unhandled(self):
        q = self.queued(); ledger = self.ledger(); ledger['requests'][-1]['expires_at'] = 0
        runner.save(self.d/'recovery.json', ledger)
        with self.assertRaisesRegex(ValueError, 'expired'): self.claim(q)
        self.assertEqual(recovery.handler_status(self.d, runner.read(self.d/'state.json'), self.origin), 'unhandled')
        self.assertFalse(recovery.tick(self.d)); self.assertEqual(self.ledger()['status'], 'stalled')
        self.assertEqual(len(self.effects), 1)

    def test_environment_reference_alone_does_not_authorize_takeover(self):
        q = self.queued(); self.actor = None
        with patch.dict(os.environ, CTASK_SESSION_REF=self.origin, CTASK_SESSION_TOKEN='invented'):
            with self.assertRaises(ValueError): self.claim(q)
        self.assertEqual(self.ledger()['requests'][-1]['state'], 'queued')

    def test_correlated_accept_reply_end_use_mailbox_without_workflow_acceptance(self):
        q = self.queued()
        self.events = [self.event(q, k) for k in ('message_received', 'response_completed', 'turn_completed')]
        self.assertTrue(recovery.tick(self.d))
        after = self.ledger()['requests'][-1]
        self.assertEqual(after['mailbox_state'], 'turn_completed')
        self.assertEqual(after['state'], 'queued')
        self.assertIn('turn_completed_at', after)
        self.assertEqual(runner.read(self.d/'state.json')['criteria']['artifact']['status'], 'pending')
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')
        self.assertEqual(len(self.effects), 1)

    def test_tool_events_do_not_complete_transport_or_accept_workflow(self):
        q = self.queued()
        self.events = [self.event(q, 'tool_started'), self.event(q, 'tool_completed')]
        recovery.tick(self.d)
        self.assertEqual(self.ledger()['requests'][-1]['mailbox_state'], 'delivered')
        self.assertNotIn('turn_completed_at', self.ledger()['requests'][-1])
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_canonical_unknown_cannot_be_overridden_by_queued_projection(self):
        self.delivery_result = 'delivery_unknown'
        recovery.tick(self.d)
        ledger = self.ledger(); ledger['requests'][-1]['state'] = 'queued'
        runner.save(self.d/'recovery.json', ledger)
        q = ledger['requests'][-1]
        with self.assertRaisesRegex(ValueError, 'canonical_delivery_unavailable'): self.claim(q)
        self.assertEqual(transport.managed_bus().message(q['delivery']['mailbox_id'])['state'], 'delivery_unknown')

    def test_session_host_exit_with_native_activity_keeps_execution_quarantined(self):
        original = self.adapter.load_or_resume_session
        def load(*args, **kwargs):
            result = original(*args, **kwargs)
            self.activity = 'active'; self.active_turn = 'restored-turn'
            return result
        with patch.object(self.adapter, 'load_or_resume_session', load): self.host_resume()
        s = runner.read(self.d/'state.json')
        self.assertEqual(recovery.current_execution_owner(s)['role'], 'session_host')
        with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
            with self.assertRaises(ConflictError):
                with recovery.execution_owner(self.d, s): self.fail('native session still active')

    def test_recovery_actor_from_another_session_cannot_resume_bound_origin(self):
        s = runner.read(self.d/'state.json')
        actor = session_router.authenticate_actor(self.hub,
            agent_registry.ActorProof('fixture', 'local:fixture', 'synthetic-proof'), role='session_host',
            project=self.repo, workflow_id=str(self.d), contract_hash=s['contract_hash'], generation=0)
        with self.assertRaisesRegex(ValueError, 'exact target session'):
            session_router.load_or_resume_v2(self.origin, {}, actor=actor, workflow_id=str(self.d),
                contract_hash=s['contract_hash'], generation=0)
        self.assertEqual(self.loads, [])

    def test_unhandled_failure_is_once_only_even_under_all_notifications_policy(self):
        row = self.policy(); recovery.disable(self.d)
        self.actor = 'hub-fixture'
        with patch.dict(os.environ, CTASK_SESSION_REF=self.hub):
            notifications.configure('all', 'Synthetic all notifications policy')
        failure_attention.observe(row, self.d, runner.read(self.d/'state.json'), runner.read(self.d/'contract.json'))
        action = next(a for a in control.rows(True) if a['kind'] == 'failure_attention')
        self.assertTrue(notifications.disposition(action)['notify'])
        action['notice'] = {'state': 'delivery_unknown'}
        self.assertFalse(notifications.disposition(action)['notify'])

    def test_unpresented_failure_refreshes_accounting_evidence_under_same_id(self):
        row = self.policy(); recovery.disable(self.d)
        failure_attention.observe(row, self.d, runner.read(self.d/'state.json'), runner.read(self.d/'contract.json'))
        first = next(a for a in control.rows(True) if a['kind'] == 'failure_attention')
        self.set_state(elapsed_seconds=21, execution_revision=1)
        failure_attention.observe(row, self.d, runner.read(self.d/'state.json'), runner.read(self.d/'contract.json'))
        actions = [a for a in control.rows(True) if a['kind'] == 'failure_attention']
        self.assertEqual([a['id'] for a in actions], [first['id']])
        self.assertTrue(failure_attention.current(actions[0]))
        self.assertEqual(actions[0]['created_at'], first['created_at'])

    def test_neutral_reply_observation_uses_canonical_v2_receipts(self):
        q = self.queued()
        self.events = [self.event(q, k) for k in ('message_received', 'response_completed', 'turn_completed')]
        tracker = {'cursor': q['event_cursor']}
        reply = session_router.observe_reply(self.origin, tracker, q['id'], runner.now, delivery=q['delivery'])
        self.assertEqual(reply['text'], 'fixture reply')
        self.assertEqual(reply['turn'], 'turn-fixture')
        self.assertIsNone(session_router.observe_reply(self.origin, tracker, q['id'], runner.now, delivery=q['delivery']))
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_duplicate_observations_reconstruct_same_canonical_receipts(self):
        q = self.queued(); event = self.event(q, 'message_received'); self.events = [event]
        recovery.tick(self.d)
        bus = transport.managed_bus(); before = bus.receipts(q['delivery']['mailbox_id'])
        ledger = self.ledger(); ledger['requests'][-1]['event_cursor'] = {'offset': 0}
        runner.save(self.d/'recovery.json', ledger); recovery.tick(self.d)
        self.assertEqual(transport.managed_bus().receipts(q['delivery']['mailbox_id']), before)

    def test_forged_message_session_generation_and_correlation_leave_cursor_unchanged(self):
        q = self.queued(); before = (self.d/'recovery.json').read_bytes()
        for change in ({'message_id': 'wrong'}, {'session': replace(self.identity, native_id='hub-fixture')},
                {'generation': 99}, {'correlation_id': 'wrong'}):
            self.events = [self.event(q, 'message_received', **change)]
            with self.subTest(change=change), self.assertRaises(ValueError): recovery.tick(self.d)
            self.assertEqual(before, (self.d/'recovery.json').read_bytes())
        self.assertEqual(transport.managed_bus().message(q['delivery']['mailbox_id'])['state'], 'delivered')

    def test_unrelated_turn_cannot_complete_an_accepted_message(self):
        q = self.queued(); self.events = [self.event(q, 'message_received')]; recovery.tick(self.d)
        self.events.append(self.event(q, 'turn_completed', turn_id='other-turn'))
        with self.assertRaises(IdentityError): recovery.tick(self.d)
        self.assertEqual(transport.managed_bus().message(q['delivery']['mailbox_id'])['state'], 'accepted')

    def test_generation_change_refuses_stale_claim_and_does_not_observe_old_events(self):
        q = self.queued(); self.events = [self.event(q, 'turn_completed')]
        self.set_state(execution_revision=1)
        with self.assertRaisesRegex(ValueError, 'generation_changed'): self.claim(q)
        self.activity = 'active'; self.active_turn = 'unrelated-new-turn'
        recovery.tick(self.d)
        self.assertEqual(self.observations, 0)
        self.assertEqual(self.ledger()['requests'][0]['state'], 'superseded')
        self.assertNotIn('resumed_at', self.ledger()['requests'][0])

    def test_unrelated_reactivated_origin_prevents_claim(self):
        q = self.queued(); self.activity = 'active'; self.active_turn = 'unrelated-turn'
        with self.assertRaisesRegex(ValueError, 'origin_other_turn_active'): self.claim(q)
        self.assertEqual(self.ledger()['requests'][-1]['state'], 'queued')

    def test_matching_origin_turn_can_claim_and_excludes_worker_and_session_host(self):
        q = self.queued(); self.events = [self.event(q, 'message_received')]; recovery.tick(self.d)
        self.activity = 'active'; self.active_turn = 'turn-fixture'; self.claim(q)
        s = runner.read(self.d/'state.json')
        with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
            with self.assertRaises(ConflictError):
                with recovery.execution_owner(self.d, s): self.fail('two owners')
        with self.assertRaises(ValueError): self.host_resume()
        self.assertEqual(self.loads, [])
        self.assertEqual(recovery.handler_status(self.d, s, self.origin), 'execution_owned')

    def host_entry(self, kind):
        if kind == 'budget':
            return budget.host_run(self.d, [sys.executable, '-c', 'pass'], 'Isolated host verification')
        return GuardedWorkflow(self.d, lambda: None).verify_only()

    def acquire_host_lease(self, bus, role):
        state = runner.read(self.d/'state.json')
        return bus.acquire_execution(recovery.execution_resource(self.repo), project=str(self.repo),
            task_id=str(self.d), contract_hash=state['contract_hash'], generation=recovery.generation(self.d, state),
            role=role, owner='synthetic-existing-owner', source='Isolated lease fixture')

    def test_host_entries_refuse_active_origin_and_session_host_leases_without_mutation(self):
        bus = transport.managed_bus(); resource = recovery.execution_resource(self.repo)
        for kind in ('budget', 'hub'):
            for role in ('origin', 'session_host'):
                with self.subTest(entry=kind, role=role):
                    token, fence = self.acquire_host_lease(bus, role)
                    before = (self.d/'state.json').read_bytes(); lease = bus.execution_lease(resource)
                    with patch.object(runner.Runner, 'command', side_effect=AssertionError('no command allowed')):
                        with self.assertRaises(ConflictError): self.host_entry(kind)
                    self.assertEqual((self.d/'state.json').read_bytes(), before)
                    self.assertEqual(bus.execution_lease(resource), lease)
                    bus.release_execution(resource, token, fence, generation=json.loads(lease['generation']))

    def test_host_entries_refuse_expired_unknown_native_activity_without_mutation(self):
        for kind in ('budget', 'hub'):
            for role in ('origin', 'session_host'):
                with self.subTest(entry=kind, role=role):
                    moment = [100]
                    bus = MessageBus(self.root/(kind+'-'+role+'.sqlite'), clock=lambda: moment[0])
                    self.acquire_host_lease(bus, role); moment[0] = 131; self.activity = 'unknown'
                    before = (self.d/'state.json').read_bytes()
                    with patch('ato_workflow.transport.managed_bus', return_value=bus), patch.object(
                            runner.Runner, 'command', side_effect=AssertionError('no command allowed')):
                        with self.assertRaisesRegex(ValueError, 'unknown native activity'): self.host_entry(kind)
                    self.assertEqual((self.d/'state.json').read_bytes(), before)
                    self.assertEqual(bus.execution_lease(recovery.execution_resource(self.repo))['state'], 'active')

    def test_host_entries_reconcile_expired_idle_lease_and_fence_late_release(self):
        (self.repo/'artifact').write_text('synthetic artifact')
        for kind in ('budget', 'hub'):
            with self.subTest(entry=kind):
                moment = [100]; bus = MessageBus(self.root/(kind+'-idle.sqlite'), clock=lambda: moment[0])
                token, fence = self.acquire_host_lease(bus, 'session_host')
                resource = recovery.execution_resource(self.repo); prior = bus.execution_lease(resource)
                moment[0] = 131; self.activity = 'idle'
                with patch('ato_workflow.transport.managed_bus', return_value=bus): self.host_entry(kind)
                current = bus.execution_lease(resource)
                self.assertEqual(current['state'], 'released'); self.assertGreater(current['fence'], fence)
                with self.assertRaises(ConflictError):
                    bus.release_execution(resource, token, fence, generation=json.loads(prior['generation']))
                state = runner.read(self.d/'state.json')
                self.assertEqual(json.loads(current['generation']), recovery.generation(self.d, state))
                self.assertNotEqual(state['status'], 'completed')
                self.assertEqual(list((self.d/'acknowledgements').iterdir()), [])

    def test_host_entries_publish_renew_and_release_their_exact_execution_generation(self):
        bus = transport.managed_bus(); resource = recovery.execution_resource(self.repo)
        for kind in ('budget', 'hub'):
            with self.subTest(entry=kind):
                observed = []
                def command(owner, *args, **kwargs):
                    lease = bus.execution_lease(resource); state = runner.read(self.d/'state.json')
                    self.assertEqual(lease['state'], 'active')
                    self.assertEqual(json.loads(lease['generation']), recovery.generation(self.d, state))
                    self.assertEqual(lease['role'], 'origin' if kind == 'budget' else 'worker')
                    # Advance only the injected lease clock; guard must renew it
                    # before the original 30-second admission window expires.
                    expires = lease['lease_until']; bus.clock = lambda: expires-1
                    owner._last_heartbeat = 0
                    owner.guard()
                    self.assertGreater(bus.execution_lease(resource)['lease_until'], expires)
                    observed.append(lease['fence'])
                    raise runner.Halt('stopped')
                with patch('ato_workflow.transport.managed_bus', return_value=bus), patch.object(runner.Runner, 'command', command):
                    if kind == 'budget':
                        with self.assertRaises(runner.Halt): self.host_entry(kind)
                    else: self.host_entry(kind)
                self.assertEqual(len(observed), 1)
                self.assertEqual(bus.execution_lease(resource)['state'], 'released')
                self.assertIsNone(runner.read(self.d/'state.json')['owner_pid'])
                bus.clock = time.time
                self.set_state(status='blocked')

    def test_origin_disposition_releases_lease_without_resetting_failure_or_grants(self):
        q = self.queued(); self.claim(q); before = runner.read(self.d/'state.json'); grant = self.ledger()
        recovery.takeover(self.d, q['id'], 'blocked', 'Needs a real business decision')
        self.assertEqual(runner.read(self.d/'state.json'), before)
        for key in ('deadline', 'max_requests', 'same_blocker_limit', 'source'):
            self.assertEqual(self.ledger()[key], grant[key])
        self.assertIsNone(recovery.current_execution_owner(before))

    def test_unknown_activity_never_authorizes_new_recovery_or_session_resume(self):
        self.activity = 'unknown'
        self.assertTrue(recovery.tick(self.d)); self.assertEqual(self.effects, [])
        self.assertEqual(self.ledger()['status'], 'origin_unknown')
        with self.assertRaisesRegex(ValueError, 'verified idle'): self.host_resume()
        self.assertEqual(recovery.handler_status(self.d, runner.read(self.d/'state.json'), self.origin), 'activity_unknown')

    def test_unknown_delivery_never_replays_and_is_not_a_claimable_request(self):
        self.delivery_result = 'delivery_unknown'
        self.assertFalse(recovery.tick(self.d)); q = self.ledger()['requests'][-1]
        for _ in range(3): self.assertFalse(recovery.tick(self.d))
        with self.assertRaisesRegex(ValueError, 'already_handled'): self.claim(q)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(recovery.handler_status(self.d, runner.read(self.d/'state.json'), self.origin), 'delivery_unknown')

    def test_expired_endpoint_owner_receipt_is_late_and_cannot_claim_acceptance(self):
        q = self.queued(); bus = transport.managed_bus(); message = bus.message(q['delivery']['mailbox_id'])
        bus.clock = lambda: time.time()+31
        self.events = [self.event(q, 'message_received')]
        with patch('ato_workflow.transport.managed_bus', return_value=bus): recovery.tick(self.d)
        self.assertNotIn('observed_turn', self.ledger()['requests'][-1])
        self.assertTrue(next(r for r in bus.receipts(message['id']) if r['receipt_id'] == self.events[0].event_id)['late'])

    def test_expired_origin_lease_requires_idle_and_fences_late_release(self):
        s = runner.read(self.d/'state.json'); moment = [100]
        bus = MessageBus(self.root/'lease.sqlite', clock=lambda: moment[0])
        resource = recovery.execution_resource(self.repo); gen = recovery.generation(self.d, s)
        old, fence = bus.acquire_execution(resource, project=self.repo, task_id=str(self.d),
            contract_hash=s['contract_hash'], generation=gen, role='origin', owner='prior', source='fixture', lease_seconds=5)
        moment[0] = 106
        with patch('ato_workflow.transport.managed_bus', return_value=bus), recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
            self.activity = 'active'
            with self.assertRaisesRegex(ValueError, 'active or unknown'):
                with recovery.execution_owner(self.d, s): self.fail('active native origin')
            self.activity = 'idle'
            with recovery.execution_owner(self.d, s) as owned:
                with self.assertRaises(ConflictError): bus.release_execution(resource, old, fence, generation=gen)
                owned.renew()
        self.assertEqual(bus.execution_lease(resource)['state'], 'released')

    def test_execution_generation_advance_fences_old_owner_and_preserves_attempts(self):
        s = runner.read(self.d/'state.json'); gen = recovery.generation(self.d, s)
        with recovery.locked(self.repo/'.codex/tasks/.worker.lock'), recovery.execution_owner(self.d, s) as owned:
            old_fence = owned.fence; next_gen = [123, 1]; owned.advance(next_gen)
            with self.assertRaises(ConflictError):
                owned.bus.renew_execution(owned.resource, owned.token, old_fence, generation=gen)
            owned.renew()
            self.assertEqual(json.loads(owned.bus.execution_lease(owned.resource)['generation']), next_gen)
        self.assertEqual(runner.read(self.d/'state.json')['planning_attempts'], 2)

    def test_repository_lock_excludes_session_host_even_with_idle_activity(self):
        with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
            with self.assertRaises(BlockingIOError): self.host_resume()
        self.assertEqual(self.loads, [])

    def test_worker_lease_excludes_origin_claim_even_when_flock_is_free(self):
        q = self.queued(); s = runner.read(self.d/'state.json')
        with recovery.execution_owner(self.d, s):
            with self.assertRaisesRegex(ValueError, 'execution_lease_active'): self.claim(q)
        self.assertEqual(self.ledger()['requests'][-1]['state'], 'queued')

    def test_contract_or_user_closure_refuses_recovery_without_dispatch(self):
        (self.d/'user-closure.json').write_text('{}')
        self.assertFalse(recovery.tick(self.d)); self.assertEqual(self.ledger()['status'], 'user_closed')
        (self.d/'user-closure.json').unlink()
        self.set_state(contract_hash='wrong')
        self.assertFalse(recovery.tick(self.d)); self.assertEqual(self.ledger()['status'], 'binding_changed')
        self.assertEqual(self.effects, [])

    def test_once_only_failure_notice_survives_reobservation_and_presentation(self):
        row = self.policy(); recovery.disable(self.d)
        for _ in range(2):
            failure_attention.observe(row, self.d, runner.read(self.d/'state.json'), runner.read(self.d/'contract.json'))
        actions = [a for a in control.rows(True) if a['kind'] == 'failure_attention']
        self.assertEqual(len(actions), 1); self.assertTrue(notifications.disposition(actions[0])['notify'])
        self.actor = 'hub-fixture'
        with patch.dict(os.environ, CTASK_SESSION_REF=self.hub):
            control.record(actions[0]['id'], state='presented', note='Synthetic actual presentation')
        action = next(a for a in control.rows(True) if a['id'] == actions[0]['id'])
        self.assertFalse(notifications.disposition(action)['notify'])
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_expired_pending_request_no_longer_hides_unhandled_failure(self):
        row = self.policy(); q = self.queued()
        s = runner.read(self.d/'state.json')
        self.assertEqual(recovery.handler_status(self.d, s, self.origin), 'recovery_pending')
        ledger = self.ledger(); ledger['requests'][-1]['expires_at'] = 0; runner.save(self.d/'recovery.json', ledger)
        failure_attention.observe(row, self.d, s, runner.read(self.d/'contract.json'))
        self.assertEqual(len([a for a in control.rows(True) if a['kind'] == 'failure_attention']), 1)

    def test_active_or_unknown_origin_suppresses_false_unhandled_failure(self):
        row = self.policy(); recovery.disable(self.d)
        for activity in ('active', 'unknown'):
            self.activity = activity
            failure_attention.observe(row, self.d, runner.read(self.d/'state.json'), runner.read(self.d/'contract.json'))
        self.assertEqual([a for a in control.rows(True) if a['kind'] == 'failure_attention'], [])

    def test_quiet_controller_records_ordinary_status_without_send_or_presentation(self):
        self.policy(); recovery.disable(self.d)
        self.activity = 'unknown'  # Absence of evidence is not an unhandled failure.
        before = len(self.effects)
        for _ in range(2): control.tick()
        self.assertEqual(len(self.effects), before)
        actions = control.rows(True)
        self.assertTrue(actions)
        self.assertTrue(all(not a.get('presented_at') for a in actions))
        self.assertEqual(runner.read(self.d/'state.json')['criteria']['human']['status'], 'pending')

    def test_terminal_recovery_alert_is_attempted_once_per_request(self):
        q = self.queued(); ledger = self.ledger(); ledger['requests'][-1]['expires_at'] = 0
        runner.save(self.d/'recovery.json', ledger)
        with patch('ato_workflow.interaction.supervision_alert', return_value={'routed': False, 'reason': 'fixture quiet'}) as alert:
            for _ in range(3): recovery.tick(self.d)
        self.assertEqual(alert.call_count, 1)

    def stage(self):
        w = workflow.Workflow(self.d)
        w.stage(dict(objective='Frozen local fixture checks', memory=False,
            tasks=[dict(id='artifact', prompt='fixture', checks=[[sys.executable, '-c', 'assert True']])]), ['artifact'])
        child = Path(w.state['active_run']); s = runner.read(child/'state.json')
        s.update(status='blocked'); s['tasks']['artifact'].update(status='failed', attempts=2,
            attempt_sequence=2, feedback='original failure', evidence=['original-fixture.log'])
        runner.save(child/'state.json', s); self.set_state(status='blocked')
        return child

    def test_host_recheck_joins_lease_and_preserves_original_failure_attempts(self):
        child = self.stage(); before = runner.read(child/'state.json')['tasks']['artifact']
        def check(child_runner, task, label):
            lease = recovery.current_execution_owner(runner.read(self.d/'state.json'))
            self.assertEqual(lease['role'], 'origin')
            self.assertEqual(json.loads(lease['generation']), recovery.generation(self.d, runner.read(self.d/'state.json')))
            child_runner.guard()
            return False, []
        with patch.object(runner.Runner, 'checks', check):
            result = host_recheck.recheck(self.d, 'artifact', 'Synthetic bounded host recheck')
        self.assertFalse(result['passed'])
        after = runner.read(child/'state.json')
        self.assertEqual(after['tasks']['artifact'], before)
        self.assertEqual(after['host_rechecks'][0]['previous'], before)
        self.assertEqual(runner.read(self.d/'state.json')['criteria']['human']['status'], 'pending')
        self.assertIsNone(recovery.current_execution_owner(runner.read(self.d/'state.json')))

    def test_host_recheck_cannot_compete_with_origin_repair_lease(self):
        self.stage(); q = self.queued(); self.claim(q)
        before = (self.d/'state.json').read_bytes()
        with patch.object(runner.Runner, 'checks', side_effect=AssertionError('must not run')):
            with self.assertRaises(ConflictError): host_recheck.recheck(self.d, 'artifact', 'Synthetic host check')
        self.assertEqual(before, (self.d/'state.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
