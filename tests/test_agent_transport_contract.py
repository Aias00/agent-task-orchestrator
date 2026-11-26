"""Real SQLite/process boundaries with a deterministic clock (no live sessions)."""
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ato_workflow.message_bus import MessageBus, IdentityError, ConflictError, MailboxError
from ato_workflow.transport import DeliveryResult, TransportEvent, dispatch_one, receive_event


def concurrent_enqueue(path, credential, binding, barrier, output):
    try:
        bus = MessageBus(path, clock=lambda: 100)
        barrier.wait(timeout=15)
        msg = bus.enqueue(credential, binding_id=binding, target='origin', kind='request', body='one',
                          idempotency_key='race', task_id='task', contract_hash='hash')
        output.put(('ok', msg['id']))
    except Exception as exc:
        output.put(('error', type(exc).__name__))


def concurrent_claim(path, credential, owner, barrier, output):
    try:
        bus = MessageBus(path, clock=lambda: 100)
        barrier.wait(timeout=15)
        msg = bus.claim(credential, owner)
        output.put(('ok', msg['id'] if msg else None))
    except Exception as exc:
        output.put(('error', type(exc).__name__))


def concurrent_owner(path, credential, barrier, output):
    try:
        bus = MessageBus(path, clock=lambda: 100)
        barrier.wait(timeout=15)
        owner = bus.acquire_endpoint(credential, owner=str(os.getpid()))
        output.put(('owned', owner))
    except ConflictError:
        output.put(('busy', None))
    except Exception as exc:
        output.put(('error', type(exc).__name__))


def concurrent_reply(path, credential, message_id, barrier, output):
    try:
        bus = MessageBus(path, clock=lambda: 100)
        barrier.wait(timeout=15)
        result = bus.reply(credential, message_id, receipt_id='same-native-reply', body='one native result')
        output.put(('ok', result['id']))
    except Exception as exc:
        output.put(('error', type(exc).__name__))


def crash_at(path, credential, owner, where, effects):
    bus = MessageBus(path, clock=lambda: 100)
    msg = bus.claim(credential, owner, lease_seconds=5)
    if where == 'claimed':
        os._exit(42)
    msg = bus.begin_dispatch(credential, owner, msg['id'], msg['claim_token'])
    if where == 'sent':
        with open(effects, 'a') as handle:
            handle.write(msg['id']+'\n')
            handle.flush()
            os.fsync(handle.fileno())
    os._exit(42)


def crash_transaction(path):
    con = sqlite3.connect(path)
    con.execute('begin immediate')
    con.execute("update messages set state='corrupt',busy=0")
    os._exit(42)


class FixtureTransport:
    def __init__(self, result=None, failure=None):
        self.calls = []
        self.result = result or DeliveryResult('delivered', 'fixture-receipt')
        self.failure = failure

    def send(self, endpoint, envelope):
        self.calls.append((endpoint, envelope))
        if self.failure:
            raise self.failure
        return self.result


class MessageBusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ctask-bus-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'mailbox/state.sqlite'
        self.now = 100
        self.bus = self.restart()
        self.hub = self.bus.register_endpoint('hub', kind='codex', session_id='fixture-hub',
                                             project=self.root, source='verified fixture hub')
        self.origin = self.bus.register_endpoint('origin', kind='claude', session_id='fixture-origin',
                                                project=self.root, source='managed local fixture')
        self.route = self.bus.bind_route(sender='hub', target='origin', task_id='task',
                                         contract_hash='hash', source='fixture routing instruction')['id']
        self.owner = self.bus.acquire_endpoint(self.origin, owner='fixture', lease_seconds=30)

    def restart(self):
        return MessageBus(self.path, clock=lambda: self.now)

    def enqueue(self, key='first', **changes):
        values = dict(binding_id=self.route, target='origin', kind='request', body='hello',
                      idempotency_key=key, task_id='task', contract_hash='hash')
        values.update(changes)
        return self.bus.enqueue(self.hub, **values)

    def dispatch(self, adapter=None):
        return dispatch_one(self.bus, self.origin, self.owner, {'claude': adapter or FixtureTransport()})

    def run_processes(self, target, arguments, count=4):
        # The frozen gate is a top-level script without a __main__ guard;
        # spawn would recursively execute the entire acceptance suite.
        ctx = multiprocessing.get_context('fork')
        barrier, output = ctx.Barrier(count), ctx.Queue()
        jobs = [ctx.Process(target=target, args=(*arguments, barrier, output)) for _ in range(count)]
        for job in jobs:
            job.start()
        try:
            results = [output.get(timeout=20) for _ in jobs]
            for job in jobs:
                job.join(20)
                self.assertEqual(job.exitcode, 0)
            self.assertFalse(any(row[0] == 'error' for row in results), results)
            return results
        finally:
            for job in jobs:
                if job.is_alive():
                    job.kill()
                    job.join()
            output.close()

    def crash(self, target, *args):
        job = multiprocessing.get_context('fork').Process(target=target, args=args)
        job.start()
        job.join(20)
        if job.is_alive():
            job.kill()
            job.join()
        self.assertEqual(job.exitcode, 42)

    def test_two_turn_round_trip_restart_and_distinct_acknowledgements(self):
        first = self.enqueue()
        adapter = FixtureTransport()
        delivered = self.dispatch(adapter)
        self.assertEqual(delivered['state'], 'delivered')
        self.assertIsNone(delivered['accepted_at'])
        self.assertNotIn('attempt_id', adapter.calls[0][1])
        self.assertNotIn('claim_token', adapter.calls[0][1])
        self.bus = self.restart()
        accepted = self.bus.accept(self.origin, first['id'], receipt_id='ack-1')
        self.assertEqual(accepted['state'], 'accepted')
        self.assertEqual(accepted['accepted_at'], 100)
        reply = self.bus.reply(self.origin, first['id'], receipt_id='reply-1', body='first reply')
        self.assertEqual(reply['reply_to'], first['id'])
        self.assertEqual(reply['correlation_id'], first['correlation_id'])
        self.assertEqual(self.bus.message(first['id'])['state'], 'replied')
        hub_owner = self.bus.acquire_endpoint(self.hub, owner='hub host')
        dispatch_one(self.bus, self.hub, hub_owner, {'codex': FixtureTransport()})
        self.bus.accept(self.hub, reply['id'], receipt_id='reply-ack')
        self.bus.complete(self.hub, reply['id'], receipt_id='reply-finished')
        second = self.enqueue('second', body='follow-up with first reply')
        self.dispatch()
        self.bus.accept(self.origin, second['id'], receipt_id='ack-2')
        result = self.bus.reply(self.origin, second['id'], receipt_id='reply-2', body='second reply')
        self.assertNotEqual(result['correlation_id'], reply['correlation_id'])
        self.assertEqual(result['reply_to'], second['id'])
        restarted = self.restart()
        self.assertEqual(restarted.message(reply['id'])['body'], 'first reply')
        self.assertEqual(restarted.message(result['id'])['body'], 'second reply')

    def test_idempotency_payload_conflict_and_receipt_replay(self):
        first = self.enqueue()
        self.assertEqual(self.enqueue()['id'], first['id'])
        with self.assertRaises(ConflictError):
            self.enqueue(body='changed')
        self.dispatch()
        self.bus.accept(self.origin, first['id'], receipt_id='ack')
        self.bus.accept(self.origin, first['id'], receipt_id='ack')
        reply = self.bus.reply(self.origin, first['id'], receipt_id='reply', body='reply')
        self.assertEqual(self.bus.reply(self.origin, first['id'], receipt_id='reply', body='reply')['id'], reply['id'])
        with self.assertRaises(ConflictError):
            self.bus.reply(self.origin, first['id'], receipt_id='reply', body='changed')
        self.assertEqual(sum(e['kind'] == 'accepted' for e in self.bus.events(message_id=first['id'])), 1)

    def test_multi_process_enqueue_deduplicates_and_claim_serializes_fifo(self):
        rows = self.run_processes(concurrent_enqueue, (str(self.path), self.hub, self.route))
        self.assertEqual(len({r[1] for r in rows}), 1)
        second = self.enqueue('next')
        rows = self.run_processes(concurrent_claim, (str(self.path), self.origin, self.owner))
        self.assertEqual(sum(r[1] is not None for r in rows), 1)
        self.assertEqual(self.bus.message(second['id'])['state'], 'queued')
        self.assertEqual(self.bus.message(second['id'])['busy'], 0)

    def test_multi_process_exclusive_endpoint_owner_and_fencing(self):
        self.bus.release_endpoint(self.origin, self.owner)
        rows = self.run_processes(concurrent_owner, (str(self.path), self.origin))
        self.assertEqual(sum(r[0] == 'owned' for r in rows), 1)
        self.assertEqual(sum(r[0] == 'busy' for r in rows), 3)
        self.now = 131
        newer = self.bus.acquire_endpoint(self.origin, owner='replacement')
        with self.assertRaises(ConflictError):
            self.bus.renew_endpoint(self.origin, self.owner)
        self.bus.renew_endpoint(self.origin, newer, lease_seconds=60)
        self.assertEqual(self.bus.endpoint('origin')['owner_until'], 191)

    def test_multi_process_reply_is_one_atomic_durable_response(self):
        first = self.enqueue()
        self.dispatch()
        self.bus.accept(self.origin, first['id'], receipt_id='accepted')
        rows = self.run_processes(concurrent_reply, (str(self.path), self.origin, first['id']))
        self.assertEqual(len({row[1] for row in rows}), 1)
        self.assertEqual(self.bus.message(first['id'])['reply_id'], rows[0][1])
        self.assertEqual(self.bus.message(rows[0][1])['body'], 'one native result')
        receipts = self.bus.receipts(first['id'])
        self.assertEqual(sum(r['kind'] == 'reply' for r in receipts), 1)

    def test_crash_before_send_recovers_claim_and_fences_old_token(self):
        first = self.enqueue()
        self.crash(crash_at, str(self.path), self.origin, self.owner, 'claimed', str(self.root/'effects'))
        old = self.bus.message(first['id'])
        self.assertEqual(old['state'], 'claimed')
        self.now = 106
        self.bus = self.restart()
        self.assertEqual(self.bus.recover(), 1)
        claimed = self.bus.claim(self.origin, self.owner)
        self.assertEqual(claimed['id'], first['id'])
        self.assertNotEqual(claimed['claim_token'], old['claim_token'])
        with self.assertRaises(ConflictError):
            self.bus.begin_dispatch(self.origin, self.owner, first['id'], old['claim_token'])
        self.bus.begin_dispatch(self.origin, self.owner, first['id'], claimed['claim_token'])

    def test_crash_at_barrier_or_after_effect_never_replays(self):
        for index, point in enumerate(('dispatching', 'sent')):
            with self.subTest(point=point):
                first = self.enqueue('crash-'+point)
                effects = self.root/('effects-'+point)
                self.crash(crash_at, str(self.path), self.origin, self.owner, point, str(effects))
                self.now += 6
                self.bus = self.restart()
                self.bus.recover()
                row = self.bus.message(first['id'])
                self.assertEqual(row['state'], 'delivery_unknown')
                self.assertEqual(row['busy'], 1)
                adapter = FixtureTransport()
                self.assertIsNone(self.dispatch(adapter))
                self.assertEqual(adapter.calls, [])
                if point == 'sent':
                    self.assertEqual(effects.read_text().splitlines(), [first['id']])
                self.bus.reconcile(self.origin, first['id'], source='fixture process exited; native turn absent',
                                   outcome='not_running')
                self.assertEqual(self.bus.message(first['id'])['state'], 'failed')

    def test_fifo_busy_until_receiver_finishes_not_delivery_or_acceptance(self):
        first, second = self.enqueue(), self.enqueue('second')
        self.dispatch()
        self.assertIsNone(self.bus.claim(self.origin, self.owner))
        self.bus.accept(self.origin, first['id'], receipt_id='ack')
        self.assertIsNone(self.bus.claim(self.origin, self.owner))
        self.bus.complete(self.origin, first['id'], receipt_id='done')
        self.assertEqual(self.bus.claim(self.origin, self.owner)['id'], second['id'])

    def test_expiry_unsent_and_late_acceptance_reply_cannot_revive(self):
        queued = self.enqueue('expires-unsent', expires_at=101)
        self.now = 102
        self.bus.recover()
        self.assertEqual(self.bus.message(queued['id'])['state'], 'expired')
        self.assertEqual(self.bus.message(queued['id'])['busy'], 0)
        sent = self.enqueue('expires-sent', expires_at=104)
        self.dispatch()
        self.now = 105
        self.bus.recover()
        self.assertEqual(self.bus.message(sent['id'])['busy'], 1)
        self.assertEqual(self.bus.accept(self.origin, sent['id'], receipt_id='late')['state'], 'expired')
        self.assertIsNone(self.bus.reply(self.origin, sent['id'], receipt_id='late-reply', body='late text'))
        self.assertEqual(self.bus.message(sent['id'])['state'], 'expired')
        events = self.bus.events(message_id=sent['id'])
        self.assertEqual(sum(e['kind'] == 'late_receipt' for e in events), 2)
        receipt = next(r for r in self.restart().receipts(sent['id']) if r['kind'] == 'reply')
        self.assertEqual(receipt['late'], 1)
        self.assertEqual(receipt['body'], 'late text')
        self.assertIsNone(self.bus.claim(self.origin, self.owner))

    def test_cancel_before_and_during_send_preserves_history_and_slot(self):
        first = self.enqueue()
        self.bus.cancel(self.hub, first['id'], source='fixture stop')
        self.assertEqual(self.bus.message(first['id'])['busy'], 0)
        self.assertEqual(self.bus.message(first['id'])['body'], 'hello')
        second = self.enqueue('second')
        claim = self.bus.claim(self.origin, self.owner)
        dispatched = self.bus.begin_dispatch(self.origin, self.owner, second['id'], claim['claim_token'])
        self.bus.cancel(self.hub, second['id'], source='user paused fixture')
        late = self.bus.finish_dispatch(second['id'], dispatched['attempt_id'], state='delivered', external_id='late')
        self.assertEqual(late['state'], 'cancelled')
        self.assertEqual(late['busy'], 1)
        self.bus.reconcile(self.origin, second['id'], source='verified native turn stopped', outcome='finished')
        self.assertEqual(self.bus.message(second['id'])['busy'], 0)
        self.assertEqual(self.bus.message(second['id'])['state'], 'cancelled')

    def test_contract_revocation_and_paused_endpoint_do_not_reopen_old_work(self):
        first = self.enqueue()
        self.dispatch()
        self.bus.set_binding_state(self.route, 'superseded', source='fixture contract replaced')
        self.assertEqual(self.bus.message(first['id'])['state'], 'cancelled')
        self.assertIsNone(self.bus.reply(self.origin, first['id'], receipt_id='stale', body='ignored'))
        with self.assertRaises(ConflictError):
            self.enqueue('another')
        with self.assertRaises(ConflictError):
            self.bus.bind_route(sender='hub', target='origin', task_id='task', contract_hash='hash',
                                source='fixture routing instruction')
        route2 = self.bus.bind_route(sender='hub', target='origin', task_id='task', contract_hash='new-hash', source='new contract')
        second = self.enqueue('new', binding_id=route2['id'], contract_hash='new-hash')
        self.bus.set_endpoint_state(self.origin, 'paused', source='actual pause')
        self.bus.set_endpoint_state(self.origin, 'active', source='actual resume')
        self.assertEqual(self.bus.message(second['id'])['state'], 'cancelled')
        self.bus.set_endpoint_state(self.origin, 'closed', source='fixture finished')
        with self.assertRaises(ConflictError):
            self.bus.set_endpoint_state(self.origin, 'active', source='invalid revival')

    def test_sender_target_session_project_and_task_identity_rejected(self):
        first = self.enqueue()
        for credential in (replace(self.hub, token='wrong'), replace(self.hub, session_id='other'),
                           replace(self.hub, project='/other')):
            with self.assertRaises(IdentityError):
                self.bus.cancel(credential, first['id'], source='forgery')
        for changes in ({'target': 'hub'}, {'contract_hash': 'changed'}, {'task_id': 'another'}):
            with self.assertRaises(IdentityError):
                self.enqueue('wrong', **changes)
        with self.assertRaises(IdentityError):
            self.bus.accept(self.hub, first['id'], receipt_id='wrong-receiver')
        with self.assertRaises(ConflictError):
            self.bus.register_endpoint('other', kind='claude', session_id='fixture-origin', project=self.root,
                                       source='duplicate session')
        other_project = self.root/'other-project'
        other_project.mkdir()
        other = self.bus.register_endpoint('other', kind='pi', session_id='different', project=other_project,
                                           source='managed other project')
        with self.assertRaises(IdentityError):
            self.bus.bind_route(sender='hub', target=other.endpoint_id, task_id='task', contract_hash='hash', source='wrong implicit route')
        route = self.bus.bind_route(sender='hub', target=other.endpoint_id, task_id='task', contract_hash='hash',
                                    source='explicit hub crossing', cross_project=True)
        self.assertEqual(route['target'], other.endpoint_id)

    def test_transport_timeout_unknown_and_definite_not_sent_release(self):
        first = self.enqueue()
        adapter = FixtureTransport(failure=TimeoutError('fictional-key-must-not-be-recorded'))
        self.assertEqual(self.dispatch(adapter)['state'], 'delivery_unknown')
        self.assertIsNone(self.dispatch(adapter))
        self.assertEqual(len(adapter.calls), 1)
        self.assertNotIn('fictional-key-must-not-be-recorded', repr(self.bus.events()))
        self.bus.reconcile(self.origin, first['id'], source='fixture no running native turn', outcome='not_running')
        second = self.enqueue('second')
        self.assertEqual(self.dispatch(FixtureTransport(DeliveryResult('failed')))['state'], 'failed')
        self.assertEqual(self.bus.message(second['id'])['busy'], 0)
        third = self.enqueue('third')
        self.assertEqual(self.dispatch()['id'], third['id'])

    def test_unknown_delivery_not_resent(self):
        import agent_registry
        from ato_workflow.transport import dispatch_registered

        class UnknownAdapter:
            kind = 'fixture_unknown_transport'
            capabilities = frozenset({'transport'})
            calls = 0

            def send(self, endpoint, message):
                self.calls += 1
                raise TimeoutError('ambiguous fixture delivery')

        adapter = UnknownAdapter()
        agent_registry.register(adapter)
        receiver = self.bus.register_endpoint('fixture-target', kind=adapter.kind,
            session_id='opaque-native-id', project=self.root, source='verified fixture')
        route = self.bus.bind_route(sender='hub', target=receiver.endpoint_id,
            task_id='task', contract_hash='hash', source='fixture')['id']
        message = self.bus.enqueue(self.hub, binding_id=route, target=receiver.endpoint_id,
            kind='request', body='hello', idempotency_key='unknown', task_id='task',
            contract_hash='hash')
        owner = self.bus.acquire_endpoint(receiver, owner='fixture')
        self.assertEqual(dispatch_registered(self.bus, receiver, owner)['state'], 'delivery_unknown')
        self.bus.recover()
        self.assertIsNone(dispatch_registered(self.bus, receiver, owner))
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(self.bus.message(message['id'])['state'], 'delivery_unknown')

    def test_registry_refusal_before_claim(self):
        from ato_adapters import agents
        from ato_workflow.transport import dispatch_registered
        message = self.enqueue()
        with self.assertRaisesRegex(ValueError, 'does not support transport'):
            dispatch_registered(self.bus, self.origin, self.owner)
        self.assertEqual(self.bus.message(message['id'])['state'], 'queued')

    def test_crash_transaction_rolls_back_body_and_state_atomically(self):
        first = self.enqueue()
        before = self.bus.events()
        self.crash(crash_transaction, str(self.path))
        restarted = self.restart()
        self.assertEqual(restarted.message(first['id'])['state'], 'queued')
        self.assertEqual(restarted.message(first['id'])['body'], 'hello')
        self.assertEqual(restarted.events(), before)
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute('pragma integrity_check').fetchone()[0], 'ok')
            self.assertEqual(con.execute('pragma foreign_key_check').fetchall(), [])

    def test_event_contract_checks_native_session_and_records_accept_reply(self):
        first = self.enqueue()
        self.dispatch()
        accepted = TransportEvent('accepted', first['id'], 'origin', 'fixture-origin', 'ack', 'fixture receipt')
        with self.assertRaises(ValueError):
            receive_event(self.bus, self.origin, replace(accepted, session_id='unrelated'))
        receive_event(self.bus, self.origin, accepted)
        reply = receive_event(self.bus, self.origin, replace(accepted, kind='reply', receipt_id='reply', text='normalized response'))
        self.assertEqual(reply['body'], 'normalized response')
        self.assertEqual(reply['reply_to'], first['id'])
        self.assertEqual(self.bus.message(first['id'])['state'], 'replied')

    def test_acceptance_before_transport_receipt_does_not_regress_state(self):
        first = self.enqueue()
        claimed = self.bus.claim(self.origin, self.owner)
        sent = self.bus.begin_dispatch(self.origin, self.owner, first['id'], claimed['claim_token'])
        self.bus.accept(self.origin, first['id'], receipt_id='native-fast-ack')
        result = self.bus.finish_dispatch(first['id'], sent['attempt_id'], state='delivered', external_id='native-receipt')
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(result['external_id'], 'native-receipt')
        self.assertEqual(result['route_source'], 'fixture routing instruction')
        repeated = self.bus.finish_dispatch(first['id'], sent['attempt_id'], state='delivered', external_id='native-receipt')
        self.assertEqual(repeated['state'], 'accepted')
        with self.assertRaises(ConflictError):
            self.bus.finish_dispatch(first['id'], sent['attempt_id'], state='failed')
        with self.assertRaises(IdentityError):
            self.bus.finish_dispatch(first['id'], 'wrong-attempt', state='delivered')
        self.assertEqual(sum(r['kind'] == 'delivery' for r in self.bus.receipts(first['id'])), 1)

    def test_release_owner_recovers_unsent_but_quarantines_dispatch(self):
        first = self.enqueue()
        old = self.bus.claim(self.origin, self.owner)
        self.bus.release_endpoint(self.origin, self.owner)
        self.assertEqual(self.bus.message(first['id'])['state'], 'queued')
        self.owner = self.bus.acquire_endpoint(self.origin, owner='replacement')
        with self.assertRaises(ConflictError):
            self.bus.begin_dispatch(self.origin, self.owner, first['id'], old['claim_token'])
        new = self.bus.claim(self.origin, self.owner)
        self.bus.begin_dispatch(self.origin, self.owner, first['id'], new['claim_token'])
        self.bus.release_endpoint(self.origin, self.owner)
        self.assertEqual(self.bus.message(first['id'])['state'], 'delivery_unknown')
        self.owner = self.bus.acquire_endpoint(self.origin, owner='third-owner')
        self.assertIsNone(self.bus.claim(self.origin, self.owner))

    def test_credentials_are_not_bodies_or_events_and_unknown_schema_rejected(self):
        first = self.enqueue()
        self.assertNotIn(self.origin.token, repr(self.origin))
        self.assertNotIn('token_hash', self.bus.endpoint('origin'))
        with sqlite3.connect(self.path) as con:
            dump = '\n'.join(con.iterdump())
            self.assertNotIn(self.hub.token, dump)
            self.assertNotIn(self.origin.token, dump)
            self.assertEqual(con.execute('select body from bodies where message_id=?', (first['id'],)).fetchone()[0], 'hello')
            con.execute('pragma user_version=99')
        with self.assertRaises(MailboxError):
            self.restart()

    def test_malformed_input_and_missing_transport_cannot_start_delivery(self):
        for value in (True, 0, -1, float('nan')):
            with self.assertRaises(MailboxError):
                self.bus.claim(self.origin, self.owner, lease_seconds=value)
        for changes in ({'expires_at': float('inf')}, {'body': {}}, {'expires_at': 99}):
            with self.assertRaises(MailboxError):
                self.enqueue(**changes)
        first = self.enqueue()
        with self.assertRaises(ValueError):
            dispatch_one(self.bus, self.origin, self.owner, {})
        self.assertEqual(self.bus.message(first['id'])['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
