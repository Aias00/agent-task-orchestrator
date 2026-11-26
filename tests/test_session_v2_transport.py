"""Offline durable transition, crash and contention qualification; no native homes.

All principals, projects, credentials, effects and clocks in this suite are
synthetic temporary fixtures. No test calls a cloud model or live session.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import agent_registry
from ato_workflow import interaction
from ato_workflow.message_bus import MessageBus, IdentityError, ConflictError
import runner
from ato_workflow import session_router
from ato_workflow.transport import DeliveryResult, TransportEvent, dispatch_one, receive_event
from ato_workflow import transport
from ato_workflow import workflow


class RecordingTransport:
    capabilities = frozenset({'transport'})

    def __init__(self, outcome='delivered', exception=None):
        self.outcome, self.exception, self.calls = outcome, exception, []

    def send(self, endpoint, envelope):
        self.calls.append(envelope)
        if self.exception:
            raise self.exception
        return DeliveryResult(self.outcome, 'native-fixture-receipt')


class DurableTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-v2-transport-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root/'project'
        self.project.mkdir()
        self.now = 100.0
        self.path = self.root/'mailbox/state.sqlite'
        self.bus = self.restart()
        self.sender = self.bus.register_endpoint('sender', kind='fixture_sender', session_id='fixture-user',
            project=self.project, source='synthetic local identity')
        self.receiver = self.bus.register_endpoint('receiver', kind='fixture_stream', session_id='fixture-session',
            project=self.project, source='synthetic exact native session/project')
        self.route = self.bus.bind_route(sender='sender', target='receiver', task_id='workflow-fixture',
            contract_hash='frozen-hash', source='synthetic routing instruction', generation=3)['id']
        self.owner = self.bus.acquire_endpoint(self.receiver, owner='fixture-host', lease_seconds=20)
        self.adapter = RecordingTransport()

    def restart(self):
        return MessageBus(self.path, clock=lambda: self.now)

    def enqueue(self, key='canonical-question', **changes):
        fields = dict(binding_id=self.route, target='receiver', kind='question', body='Choose fixture A?',
            idempotency_key=key, task_id='workflow-fixture', contract_hash='frozen-hash',
            correlation_id=key, canonical_id=key, authority_source='synthetic user chose hub',
            protocol=2, generation=3)
        fields.update(changes)
        return self.bus.enqueue(self.sender, **fields)

    def dispatch(self, adapter=None):
        return dispatch_one(self.bus, self.receiver, self.owner,
                            {'fixture_stream': adapter or self.adapter}, lease_seconds=10)

    def event(self, message, kind='accepted', receipt='ack', **changes):
        row = self.bus.message(message['id'])
        values = dict(kind=kind, message_id=row['id'], endpoint_id='receiver', session_id='fixture-session',
            receipt_id=receipt, source='synthetic native event endpoint', version=2,
            project=str(self.project), correlation_id=row['correlation_id'], generation=3,
            route_generation=row['route_generation'], endpoint_generation=row['endpoint_generation'],
            attempt_id=row['attempt_id'], turn_id='exact-turn')
        values.update(changes)
        return TransportEvent(**values)

    def receive(self, message, kind='accepted', receipt='ack', **changes):
        return receive_event(self.bus, self.receiver, self.event(message, kind, receipt, **changes))

    def crash(self, point):
        message = self.enqueue()
        # A genuine process exits after committing each requested crash window.
        code = '''import json,os,sys
from ato_workflow.message_bus import MessageBus,EndpointCredential
path,credential,owner,point,effect=sys.argv[1:]
bus=MessageBus(path,clock=lambda:100)
receiver=EndpointCredential(**json.loads(credential))
m=bus.claim(receiver,owner,lease_seconds=5)
if point!='claimed': m=bus.begin_dispatch(receiver,owner,m['id'],m['claim_token'])
if point=='sent':
    with open(effect,'w') as f:
        f.write(m['id']); f.flush(); os.fsync(f.fileno())
os._exit(47)
'''
        from dataclasses import asdict
        result = subprocess.run([sys.executable, '-c', code, str(self.path),
            json.dumps(asdict(self.receiver)), self.owner, point, str(self.root/'effect')],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 47, result.stderr)
        self.now = 121
        self.bus = self.restart()
        self.bus.recover()
        self.owner = self.bus.acquire_endpoint(self.receiver, owner='replacement', lease_seconds=20)
        return message

    def race(self, operation, count=4):
        barrier = threading.Barrier(count)
        def run(i):
            bus = self.restart()
            barrier.wait(timeout=10)
            return operation(bus, i)
        with ThreadPoolExecutor(max_workers=count) as executor:
            return list(executor.map(run, range(count)))

    def test_process_crash_before_send_can_reclaim_exact_message(self):
        message = self.crash('claimed')
        self.assertEqual(self.bus.message(message['id'])['state'], 'queued')
        self.assertEqual(self.dispatch()['id'], message['id'])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_process_crash_after_barrier_before_send_never_replays(self):
        message = self.crash('barrier')
        self.assertEqual(self.bus.message(message['id'])['state'], 'delivery_unknown')
        self.assertIsNone(self.dispatch())
        self.assertEqual(self.adapter.calls, [])

    def test_process_crash_after_effect_persists_unknown_without_replay(self):
        message = self.crash('sent')
        self.assertEqual((self.root/'effect').read_text(), message['id'])
        self.assertEqual(self.bus.message(message['id'])['busy'], 1)
        self.assertIsNone(self.dispatch())

    def test_unknown_exception_is_once_only_and_does_not_store_exception_secret(self):
        message = self.enqueue()
        adapter = RecordingTransport(exception=TimeoutError('fixture-secret-do-not-save'))
        self.assertEqual(self.dispatch(adapter)['state'], 'delivery_unknown')
        self.assertIsNone(self.dispatch(adapter))
        self.assertEqual(len(adapter.calls), 1)
        self.assertNotIn('fixture-secret-do-not-save', repr(self.bus.events()))
        self.assertEqual(self.restart().message(message['id'])['state'], 'delivery_unknown')

    def test_definite_not_sent_releases_slot_for_distinct_envelope(self):
        first = self.enqueue()
        second = self.enqueue('second')
        self.assertEqual(self.dispatch(RecordingTransport('failed'))['state'], 'failed')
        self.assertEqual(self.bus.message(first['id'])['external_possible'], 0)
        self.assertEqual(self.dispatch()['id'], second['id'])

    def test_actual_accept_response_and_turn_completion_are_distinct_transitions(self):
        first = self.enqueue()
        second = self.enqueue('second')
        self.dispatch()
        self.assertEqual(self.receive(first)['state'], 'accepted')
        self.assertEqual(self.receive(first, 'responded', 'response', text='fixture reply')['state'], 'responded')
        self.assertIsNone(self.dispatch())
        self.assertEqual(self.receive(first, 'turn_completed', 'end')['state'], 'turn_completed')
        self.assertEqual(self.dispatch()['id'], second['id'])
        self.assertNotIn('workflow_completed', repr(self.bus.events()))

    def test_repeated_ack_is_deduplicated_across_restart_and_after_end(self):
        first = self.enqueue(); self.dispatch()
        event = self.event(first)
        receive_event(self.bus, self.receiver, event)
        receive_event(self.restart(), self.receiver, event)
        self.receive(first, 'completed', 'end')
        receive_event(self.restart(), self.receiver, event)
        self.assertEqual(self.bus.message(first['id'])['state'], 'turn_completed')
        self.assertEqual(sum(r['receipt_id'] == 'ack' for r in self.bus.receipts(first['id'])), 1)

    def test_conflicting_duplicate_ack_rolls_back(self):
        first = self.enqueue(); self.dispatch(); self.receive(first)
        with self.assertRaises(ConflictError):
            self.receive(first, text='conflicting body')
        self.assertEqual(len(self.bus.receipts(first['id'])), 2)  # delivery and acceptance

    def test_wrong_endpoint_and_session_are_rejected_before_receipt(self):
        first = self.enqueue(); self.dispatch()
        for change in ({'endpoint_id': 'sender'}, {'session_id': 'another-session'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.receive(first, **change)
        self.assertEqual(self.bus.message(first['id'])['state'], 'delivered')

    def test_wrong_message_project_correlation_and_attempt_rejected(self):
        first = self.enqueue(); second = self.enqueue('other'); self.dispatch()
        event = self.event(first)
        for change in ({'message_id': second['id']}, {'project': str(self.root)},
                       {'correlation_id': 'other'}, {'attempt_id': 'forged-attempt'}):
            with self.subTest(change=change), self.assertRaises(IdentityError):
                receive_event(self.bus, self.receiver, replace(event, **change))
        self.assertEqual(self.bus.message(first['id'])['state'], 'delivered')

    def test_wrong_generation_and_route_fence_rejected(self):
        first = self.enqueue(); self.dispatch()
        for change in ({'generation': 2}, {'route_generation': 99}, {'endpoint_generation': 99}):
            with self.subTest(change=change), self.assertRaises(IdentityError):
                self.receive(first, **change)

    def test_wrong_turn_cannot_complete_or_respond(self):
        first = self.enqueue(); self.dispatch(); self.receive(first)
        for kind in ('responded', 'turn_completed'):
            with self.assertRaises(IdentityError):
                self.receive(first, kind, kind, turn_id='unrelated-turn')
        self.assertEqual(self.bus.message(first['id'])['state'], 'accepted')

    def test_legacy_receiver_api_cannot_bypass_v2_fences(self):
        first = self.enqueue(); self.dispatch()
        with self.assertRaises(IdentityError):
            self.bus.accept(self.receiver, first['id'], receipt_id='unfenced')
        with self.assertRaises(IdentityError):
            receive_event(self.bus, self.receiver, TransportEvent('accepted', first['id'], 'receiver',
                'fixture-session', 'v1', 'fixture'))

    def test_response_without_exact_acceptance_is_atomic_rejection(self):
        first = self.enqueue(); self.dispatch()
        before = self.bus.receipts(first['id'])
        with self.assertRaises(ConflictError):
            self.receive(first, 'responded', 'response', text='not yet accepted')
        self.assertEqual(self.bus.receipts(first['id']), before)

    def test_advanced_execution_cancels_old_event_and_keeps_audit(self):
        first = self.enqueue(); self.dispatch()
        event = self.event(first)
        self.bus.advance_route(self.route, generation=4, source='fixture new execution')
        receive_event(self.restart(), self.receiver, event)
        self.assertEqual(self.bus.message(first['id'])['state'], 'cancelled')
        self.assertEqual(self.bus.message(first['id'])['busy'], 1)
        self.assertTrue(next(r for r in self.bus.receipts(first['id']) if r['receipt_id'] == 'ack')['late'])
        with self.assertRaises(ConflictError):
            self.bus.advance_route(self.route, generation=3, source='late fixture')

    def test_expired_request_cannot_accept_or_free_possible_native_effect(self):
        first = self.enqueue(expires_at=101); self.dispatch()
        event = self.event(first)
        self.now = 102
        receive_event(self.bus, self.receiver, event)
        self.assertEqual(self.bus.message(first['id'])['state'], 'expired')
        self.assertEqual(self.bus.message(first['id'])['busy'], 1)

    def test_late_dispatch_receipt_after_owner_loss_stays_unknown(self):
        first = self.enqueue()
        claim = self.bus.claim(self.receiver, self.owner, lease_seconds=5)
        sent = self.bus.begin_dispatch(self.receiver, self.owner, first['id'], claim['claim_token'])
        self.now = 121
        result = self.bus.finish_dispatch(first['id'], sent['attempt_id'], state='delivered', external_id='late-native')
        self.assertEqual(result['state'], 'delivery_unknown')
        self.assertEqual(result['external_id'], 'late-native')
        self.assertEqual(result['busy'], 1)

    def test_replacement_owner_fences_old_native_acceptance(self):
        first = self.enqueue(); self.dispatch()
        event = self.event(first)
        self.now = 121
        self.owner = self.bus.acquire_endpoint(self.receiver, owner='new-host')
        result = receive_event(self.bus, self.receiver, event)
        self.assertEqual(result['state'], 'delivered')
        self.assertIsNone(self.dispatch())
        self.assertEqual(result['accepted_at'], None)
        self.assertTrue(self.bus.receipts(first['id'])[-1]['late'])

    def test_concurrent_endpoint_owners_have_exactly_one_winner(self):
        self.bus.release_endpoint(self.receiver, self.owner)
        def acquire(bus, i):
            try:
                bus.acquire_endpoint(self.receiver, owner=str(i))
                return 'won'
            except ConflictError:
                return 'busy'
        self.assertEqual(self.race(acquire).count('won'), 1)

    def test_concurrent_canonical_enqueue_has_one_durable_id(self):
        def enqueue(bus, i):
            return bus.enqueue(self.sender, binding_id=self.route, target='receiver', kind='question',
                body='same', idempotency_key='same', task_id='workflow-fixture', contract_hash='frozen-hash',
                correlation_id='canonical', protocol=2, generation=3, canonical_id='canonical',
                authority_source='synthetic user chose hub')['id']
        identifiers = self.race(enqueue)
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(sum(e['kind'] == 'enqueued' for e in self.bus.events()), 1)

    def test_concurrent_duplicate_receipts_apply_only_one_transition(self):
        first = self.enqueue(); self.dispatch()
        event = self.event(first)
        result = self.race(lambda bus, i: receive_event(bus, self.receiver, event)['state'])
        self.assertEqual(result, ['accepted']*4)
        self.assertEqual(sum(r['kind'] == 'accepted' for r in self.bus.receipts(first['id'])), 1)

    def test_execution_roles_contend_for_same_durable_resource(self):
        def acquire(bus, i):
            try:
                bus.acquire_execution('repository:fixture', project=self.project, task_id='workflow-fixture',
                    contract_hash='frozen-hash', generation=3, role=('worker', 'origin', 'session_host')[i%3],
                    owner='fixture-'+str(i), source='synthetic held repository lock')
                return 'won'
            except ConflictError:
                return 'busy'
        self.assertEqual(self.race(acquire).count('won'), 1)
        self.assertEqual(self.restart().execution_lease('repository:fixture')['state'], 'active')

    def test_old_execution_token_cannot_renew_or_release_successor(self):
        args = dict(project=self.project, task_id='workflow-fixture', contract_hash='frozen-hash',
                    generation=3, role='worker', owner='old', source='fixture', lease_seconds=5)
        token, fence = self.bus.acquire_execution('repository:fixture', **args)
        self.now = 106
        new, next_fence = self.restart().acquire_execution('repository:fixture', **dict(args, owner='new', generation=4))
        self.assertGreater(next_fence, fence)
        with self.assertRaises(ConflictError):
            self.bus.renew_execution('repository:fixture', token, fence, generation=3)
        with self.assertRaises(ConflictError):
            self.bus.release_execution('repository:fixture', token, fence, generation=3)
        self.bus.renew_execution('repository:fixture', new, next_fence, generation=4)
        self.assertEqual(self.bus.execution_lease('repository:fixture')['owner'], 'new')

    def test_route_stop_survives_reconstruction_and_does_not_clear_history(self):
        first = self.enqueue()
        self.bus.set_binding_state(self.route, 'stopped', source='synthetic user STOP')
        self.bus = self.restart()
        self.assertIsNone(self.dispatch())
        self.assertEqual(self.bus.message(first['id'])['state'], 'cancelled')
        self.assertEqual(self.bus.message(first['id'])['authority_source'], 'synthetic user chose hub')
        with self.assertRaises(ConflictError):
            self.enqueue('late')

    def test_native_queue_release_requires_registered_capability_and_fence(self):
        first = self.enqueue(); self.dispatch()
        with self.assertRaises(ConflictError):
            self.bus.queued_natively(self.receiver, first['id'])
        self.assertEqual(self.bus.message(first['id'])['busy'], 1)

    def test_v1_rows_remain_readable_and_keep_original_ids_during_migration(self):
        legacy = self.bus.enqueue(self.sender, binding_id=self.route, target='receiver', kind='old',
            body='legacy body', idempotency_key='legacy', task_id='workflow-fixture', contract_hash='frozen-hash')
        import sqlite3
        with sqlite3.connect(self.path) as con:
            con.execute('pragma user_version=1')
        upgraded = self.restart()
        self.assertEqual(upgraded.message(legacy['id'])['body'], 'legacy body')
        self.assertEqual(upgraded.message(legacy['id'])['protocol'], 1)
        dispatch_one(upgraded, self.receiver, self.owner, {'fixture_stream': self.adapter})
        self.assertEqual(upgraded.accept(self.receiver, legacy['id'], receipt_id='legacy-ack')['state'], 'accepted')

    def test_native_queue_capability_releases_submission_but_never_replays_unknown(self):
        adapter = RecordingTransport('delivery_unknown')
        adapter.kind = 'fixture_queue_'+uuid.uuid4().hex
        adapter.capabilities = frozenset({'transport', 'native_queue_serialization'})
        agent_registry.register(adapter)
        receiver = self.bus.register_endpoint('queue', kind=adapter.kind, session_id='queue-fixture',
            project=self.project, source='synthetic verified FIFO queue')
        route = self.bus.bind_route(sender='sender', target='queue', task_id='queue-task',
            contract_hash='frozen-hash', source='fixture queue authorization', generation=3)
        def enqueue(key):
            return self.bus.enqueue(self.sender, binding_id=route['id'], target='queue', kind='fixture',
                body=key, idempotency_key=key, task_id='queue-task', contract_hash='frozen-hash',
                protocol=2, generation=3, canonical_id=key, authority_source='fixture queue authorization')
        first, second = enqueue('queue-one'), enqueue('queue-two')
        owner = self.bus.acquire_endpoint(receiver, owner='queue-host')
        result = dispatch_one(self.bus, receiver, owner, {adapter.kind: adapter})
        self.assertEqual(result['busy'], 0)
        self.assertEqual(result['state'], 'delivery_unknown')
        self.assertEqual(dispatch_one(self.bus, receiver, owner, {adapter.kind: adapter})['id'], second['id'])
        self.assertIsNone(dispatch_one(self.bus, receiver, owner, {adapter.kind: adapter}))
        self.assertEqual([m['id'] for m in adapter.calls], [first['id'], second['id']])
        self.assertEqual(self.bus.message(first['id'])['accepted_at'], None)

    def test_reconciliation_requires_current_host_and_only_releases_distinct_work(self):
        first = self.enqueue(); self.enqueue('second')
        self.dispatch(RecordingTransport('delivery_unknown'))
        self.now = 121
        self.owner = self.bus.acquire_endpoint(self.receiver, owner='replacement')
        epoch = self.bus.endpoint('receiver')['generation']
        with self.assertRaises(IdentityError):
            self.bus.reconcile_correlated(self.receiver, self.owner, first['id'], source='fixture native absent',
                outcome='not_running', generation=2, endpoint_generation=epoch)
        result = self.bus.reconcile_correlated(self.receiver, self.owner, first['id'],
            source='fixture owned native process reaped and verified absent', outcome='not_running',
            generation=3, endpoint_generation=epoch)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.dispatch()['canonical_id'], 'second')
        self.assertEqual(self.bus.message(first['id'])['state'], 'failed')

    def test_delivery_receipt_after_claim_deadline_is_fenced_even_with_live_endpoint(self):
        first = self.enqueue()
        claim = self.bus.claim(self.receiver, self.owner, lease_seconds=5)
        sent = self.bus.begin_dispatch(self.receiver, self.owner, first['id'], claim['claim_token'])
        self.now = 106
        result = self.bus.finish_dispatch(first['id'], sent['attempt_id'], state='delivered')
        self.assertEqual(result['state'], 'delivery_unknown')
        self.assertEqual(result['busy'], 1)

    def test_authentication_requires_private_credential_not_environment_locator(self):
        first = self.enqueue(); self.dispatch()
        with patch.dict(os.environ, CTASK_ENDPOINT_TOKEN=self.receiver.token, CTASK_SESSION_REF='receiver'):
            with self.assertRaises(IdentityError):
                receive_event(self.bus, replace(self.receiver, token='forged'), self.event(first))
        self.assertEqual(self.bus.message(first['id'])['state'], 'delivered')

    def test_execution_generation_cannot_rewind_after_release(self):
        args = dict(project=self.project, task_id='workflow-fixture', contract_hash='frozen-hash',
                    generation=4, role='origin', owner='fixture-origin', source='fixture held lock')
        token, fence = self.bus.acquire_execution('repository:fixture', **args)
        self.bus.release_execution('repository:fixture', token, fence, generation=4)
        with self.assertRaises(ConflictError):
            self.bus.acquire_execution('repository:fixture', **dict(args, generation=3))

    def test_event_audit_keeps_exact_source_and_correlation_without_credential(self):
        first = self.enqueue(); self.dispatch(); self.receive(first)
        event = next(e for e in self.bus.events(message_id=first['id']) if e['kind'] == 'native_event')
        self.assertEqual(event['data']['source'], 'synthetic native event endpoint')
        self.assertEqual(event['data']['turn_id'], 'exact-turn')
        self.assertEqual(event['data']['generation'], 3)
        self.assertEqual(event['data']['project'], str(self.project))
        self.assertNotIn(self.receiver.token, repr(self.bus.events()))


class CanonicalMailboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-canonical-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        env = patch.dict(os.environ, dict(ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'empty-native')), clear=False)
        env.start(); self.addCleanup(env.stop)
        os.environ.pop('CTASK_SESSION_REF', None)
        os.environ.pop('CODEX_THREAD_ID', None)
        kind = 'fixture_canonical_'+uuid.uuid4().hex
        parent = self
        class Adapter:
            capabilities = frozenset({'transport', 'session_verify', 'session_actor', 'session_observe'})
            session_capabilities_v2 = {
                'session.verify': {'state': 'supported', 'evidence': 'synthetic exact identity verifier'},
                'session.transport': {'state': 'supported', 'evidence': 'synthetic effect recorder'},
                'session.observe': {'state': 'supported', 'evidence': 'synthetic bounded event fixture'},
                'actor.authenticate': {'state': 'supported', 'evidence': 'synthetic fixture challenge'},
                'session.activity': {'state': 'supported', 'evidence': 'synthetic owned lifecycle state'},
                'session.load_resume': {'state': 'supported', 'evidence': 'synthetic identity-preserving load fixture'},
            }
            def authenticate_actor(self, identity, proof, **binding):
                return 'synthetic-user' if proof.challenge == 'fixture-only-challenge' else None
            def session_activity(self, identity, *, generation):
                activity = parent.activity.get(identity.native_id, 'unknown') if isinstance(parent.activity, dict) else parent.activity
                return agent_registry.SessionActivity(identity, activity, 'synthetic owned lifecycle',
                    123.0, generation)
            def load_or_resume_session(self, identity, checkpoint, **binding):
                parent.loads.append(dict(identity=identity, binding=binding))
                return identity
            def verify_session(self, identity, project):
                return identity in ('origin-fixture', 'hub-fixture') and project == str(parent.repo)
            def is_actor(self, identity, project):
                return os.environ.get('SYNTHETIC_ACTOR') == identity
            def session_cursor(self, identity, project):
                return 0
            def observe_session(self, identity, project, correlation_id, cursor):
                return [], 0
            def send(self, endpoint, envelope):
                parent.effects.append(dict(endpoint=endpoint, envelope=envelope))
                if parent.crash_after_effect:
                    raise KeyboardInterrupt('synthetic crash after native effect')
                return DeliveryResult('delivered', 'fixture-native-receipt')
        adapter = Adapter(); adapter.kind = kind
        self.effects = []; self.loads = []; self.crash_after_effect = False; self.activity = 'idle'
        agent_registry.register(adapter)
        self.origin = session_router.register_v2(agent_registry.SessionIdentity(
            kind, 'origin-fixture', str(self.repo), 'synthetic exact identity')).reference
        self.hub = session_router.register_v2(agent_registry.SessionIdentity(
            kind, 'hub-fixture', str(self.repo), 'synthetic exact identity')).reference
        plan = self.repo/'plan.json'
        runner.save(plan, dict(objective='Fixture full goal', scope='temporary fixture', authorization='synthetic fixture only',
            memory=False, acceptance=[dict(id='artifact', description='actual file', checks=[['test', '-f', 'artifact']])]))
        self.directory = workflow.create(self.repo, plan, origin_ref=self.origin)
        with patch.dict(os.environ, CTASK_SESSION_REF=self.hub, SYNTHETIC_ACTOR='hub-fixture'):
            interaction.configure_session(self.hub, 'synthetic user selected unified hub')

    def ask(self):
        with patch.dict(os.environ, CTASK_SESSION_REF=self.origin, SYNTHETIC_ACTOR='origin-fixture'):
            return interaction.ask(self.directory, 'Choose A?', 'A')

    def test_question_and_answer_share_canonical_id_and_mailbox_authority(self):
        question = self.ask()
        with patch.dict(os.environ, CTASK_SESSION_REF=self.hub, SYNTHETIC_ACTOR='hub-fixture'):
            answered = interaction.answer(question['id'], 'A', 'synthetic fixture user response')
        bus = transport.managed_bus()
        first = bus.message(question['question_delivery']['mailbox_id'])
        second = bus.message(answered['answer_delivery']['mailbox_id'])
        self.assertEqual(first['canonical_id'], question['id'])
        self.assertEqual(first['correlation_id'], second['correlation_id'])
        self.assertEqual(second['authority_source'], 'synthetic user selected unified hub')
        self.assertEqual(len(self.effects), 2)
        with patch.dict(os.environ, CTASK_SESSION_REF=self.origin, SYNTHETIC_ACTOR='origin-fixture'):
            interaction.accept(question['id'])
        self.assertEqual(runner.read(self.directory/'state.json')['status'], 'created')

    def test_lost_json_projection_reconstructs_without_second_send(self):
        question = self.ask()
        path = interaction.question_path(question['id'])
        record = runner.read(path)
        # Simulate a crash losing the projection after the mailbox barrier.
        record.pop('question_delivery')
        with patch.dict(os.environ, CTASK_SESSION_REF=self.origin, SYNTHETIC_ACTOR='origin-fixture'):
            interaction.deliver(record, 'question_delivery', self.hub,
                self.effects[0]['envelope']['body'], path)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(record['question_delivery']['mailbox_id'], question['question_delivery']['mailbox_id'])

    def test_after_effect_crash_and_repeated_ask_do_not_send_twice(self):
        self.crash_after_effect = True
        with self.assertRaises(KeyboardInterrupt):
            self.ask()
        self.crash_after_effect = False
        question = self.ask()
        self.assertEqual(question['question_delivery']['state'], 'dispatching')
        self.assertEqual(len(self.effects), 1)

    def test_followup_projection_is_mailbox_owned_and_unknown_never_replayed(self):
        from ato_workflow import control
        with patch.dict(os.environ, CTASK_SESSION_REF=self.hub, SYNTHETIC_ACTOR='hub-fixture'):
            control.enable('synthetic fixture follow-up authorization', max_reminders=0)
        self.crash_after_effect = True
        with self.assertRaises(KeyboardInterrupt):
            control.send('followup:fixture:0', self.hub, 'synthetic follow-up', workflow=str(self.directory))
        self.crash_after_effect = False
        projected = control.send('followup:fixture:0', self.hub, 'synthetic follow-up', workflow=str(self.directory))
        self.assertEqual(projected['authority'], 'managed_mailbox')
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(transport.managed_bus().message(projected['mailbox_id'])['canonical_id'], 'followup:fixture:0')

    def test_origin_lease_excludes_worker_execution_until_recorded_release(self):
        import recovery
        state = runner.read(self.directory/'state.json')
        request = dict(id='recover-fixture', expires_at=runner.now()+30,
                       generation=recovery.generation(self.directory, state))
        lock = self.repo/'.codex/tasks/.worker.lock'
        with recovery.locked(lock, blocking=False):
            recovery.origin_lease(self.directory, state, request)
        with recovery.locked(lock, blocking=False), self.assertRaises(ConflictError):
            with recovery.execution_owner(self.directory, state):
                self.fail('worker must not run while the origin owns repair')
        with recovery.locked(lock, blocking=False):
            recovery.origin_lease(self.directory, state, request, release=True)
            with recovery.execution_owner(self.directory, state) as ownership:
                ownership.renew()
        self.assertEqual(runner.read(self.directory/'state.json')['planning_attempts'], 0)

    def test_stop_and_stale_generation_reject_reconstructed_queued_projection(self):
        question = self.ask()
        (self.directory/'STOP').touch()
        with self.assertRaises(ValueError):
            self.ask()
        self.assertEqual(len(self.effects), 1)
        (self.directory/'STOP').unlink()
        state = runner.read(self.directory/'state.json')
        state['execution_revision'] = 1
        runner.save(self.directory/'state.json', state)
        with patch.dict(os.environ, CTASK_SESSION_REF=self.hub, SYNTHETIC_ACTOR='hub-fixture'):
            with self.assertRaises(ValueError):
                interaction.answer(question['id'], 'A', 'late synthetic user response')
        self.assertEqual(len(self.effects), 1)

    def test_private_endpoint_reconstruction_is_exact_and_mode_checked(self):
        question = self.ask()
        bus = transport.managed_bus()
        message = bus.message(question['question_delivery']['mailbox_id'])
        endpoint = bus.endpoint(message['target'])
        first = transport.host_endpoint(bus, kind=endpoint['kind'], native_id=endpoint['session_id'],
            project=endpoint['project'], source=endpoint['source'])
        second = transport.host_endpoint(transport.managed_bus(), kind=endpoint['kind'],
            native_id=endpoint['session_id'], project=endpoint['project'], source=endpoint['source'])
        self.assertEqual(first, second)
        credential_file = bus.path.parent/'private'/(first.endpoint_id+'.json')
        self.assertEqual(credential_file.stat().st_mode & 0o077, 0)
        self.assertNotIn(first.token, repr(question))
        credential_file.chmod(0o644)
        with self.assertRaises(ValueError):
            transport.host_endpoint(bus, kind=endpoint['kind'], native_id=endpoint['session_id'],
                project=endpoint['project'], source=endpoint['source'])

    def host_resume(self):
        state = runner.read(self.directory/'state.json')
        proof = agent_registry.ActorProof('synthetic-local', 'synthetic-endpoint', 'fixture-only-challenge')
        actor = session_router.authenticate_actor(self.origin, proof, role='session_host', project=self.repo,
            workflow_id=str(self.directory), contract_hash=state['contract_hash'],
            generation=state.get('execution_revision', 0))
        return session_router.load_or_resume_v2(self.origin, {'fixture': 'checkpoint'}, actor=actor,
            workflow_id=str(self.directory), contract_hash=state['contract_hash'],
            generation=state.get('execution_revision', 0))

    def test_session_host_resume_rechecks_stop_before_native_effect(self):
        (self.directory/'STOP').touch()
        with self.assertRaisesRegex(ValueError, 'inactive, open'):
            self.host_resume()
        self.assertEqual(self.loads, [])

    def test_session_host_and_origin_recovery_share_exclusive_resource(self):
        import recovery
        state = runner.read(self.directory/'state.json')
        request = dict(id='recover-fixture', expires_at=runner.now()+30,
                       generation=recovery.generation(self.directory, state))
        with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
            recovery.origin_lease(self.directory, state, request)
        with self.assertRaises(ConflictError):
            self.host_resume()
        self.assertEqual(self.loads, [])
        with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
            recovery.origin_lease(self.directory, state, request, release=True)
        identity = self.host_resume()
        self.assertEqual(identity.native_id, 'origin-fixture')
        self.assertEqual(len(self.loads), 1)
        lease = transport.managed_bus().execution_lease(recovery.execution_resource(self.repo))
        self.assertEqual(lease['role'], 'session_host')
        self.assertEqual(lease['state'], 'released')
        self.assertEqual(runner.read(self.directory/'state.json')['status'], 'created')

    def test_unknown_or_active_native_activity_prevents_session_host_recovery(self):
        for state in ('unknown', 'active'):
            self.activity = state
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, 'verified idle'):
                self.host_resume()
        self.assertEqual(self.loads, [])

    def test_expired_origin_lease_does_not_authorize_worker_over_active_or_unknown_native(self):
        import recovery
        state = runner.read(self.directory/'state.json')
        bus = MessageBus(self.root/'expired-origin.sqlite', clock=lambda: 100)
        resource = recovery.execution_resource(self.repo)
        bus.acquire_execution(resource, project=self.repo, task_id=str(self.directory),
            contract_hash=state['contract_hash'], generation=recovery.generation(self.directory, state),
            role='origin', owner='synthetic-origin', source='synthetic authenticated takeover', lease_seconds=5)
        bus.clock = lambda: 106
        with patch('ato_workflow.transport.managed_bus', return_value=bus):
            for activity in ('active', 'unknown'):
                self.activity = activity
                with self.subTest(activity=activity), self.assertRaisesRegex(ValueError, 'active or unknown'):
                    with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
                        with recovery.execution_owner(self.directory, state):
                            self.fail('expired token must not authorize parallel native work')
            self.activity = 'idle'
            with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
                with recovery.execution_owner(self.directory, state):
                    self.assertEqual(bus.execution_lease(resource)['role'], 'worker')

    def test_new_workflow_cannot_use_its_idle_origin_to_steal_old_active_origin_lease(self):
        import recovery
        old_state = runner.read(self.directory/'state.json')
        successor = workflow.create(self.repo, self.repo/'plan.json', origin_ref=self.hub)
        new_state = runner.read(successor/'state.json')
        bus = MessageBus(self.root/'prior-workflow.sqlite', clock=lambda: 100)
        bus.acquire_execution(recovery.execution_resource(self.repo), project=self.repo,
            task_id=str(self.directory), contract_hash=old_state['contract_hash'],
            generation=recovery.generation(self.directory, old_state), role='origin',
            owner='prior-origin', source='synthetic authenticated origin', lease_seconds=5)
        bus.clock = lambda: 106
        self.activity = {'origin-fixture': 'active', 'hub-fixture': 'idle'}
        with patch('ato_workflow.transport.managed_bus', return_value=bus):
            with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
                with self.assertRaisesRegex(ValueError, 'active or unknown'):
                    with recovery.execution_owner(successor, new_state):
                        self.fail('the previous native origin remains the owner to reconcile')


if __name__ == '__main__':
    unittest.main()
