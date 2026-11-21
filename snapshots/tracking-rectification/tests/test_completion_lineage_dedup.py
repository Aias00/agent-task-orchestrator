"""Real controller ticks with private homes and synthetic operation receipts.

Only the native transport boundary is stubbed; canonical action/outbox claims,
trusted completion validation, binding, lineage and approval projections run.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_interaction as fixtures
import approved_delivery
import completion_delivery as identity
import completion_notifications as notices
import control
import interaction
import notifications
import recovery
import runner
import workflow
import workflow_lineage

ROOT = Path(__file__).resolve().parents[1]
ORIGIN, HUB = fixtures.ORIGIN, fixtures.HUB


class CompletionLineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ato-completion-dedup-')
        self.addCleanup(self.temp.cleanup)
        self.outer = patch.dict(os.environ, ATO_HOME=self.temp.name+'/ato')
        self.outer.start(); self.addCleanup(self.outer.stop)
        for name in ('ATO_ACTOR_FILE', 'ATO_SESSION_REF', 'CTASK_SESSION_REF'):
            os.environ.pop(name, None)
        self.f = fixtures.InteractionTests('runTest'); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.home = self.f.home.resolve()
        os.environ['CODEX_HOME'] = str(self.f.home)
        with sqlite3.connect(self.f.home/'state_5.sqlite') as con:
            for actor in (ORIGIN, HUB):
                con.execute('update threads set rollout_path=? where id=?',
                            (str(self.f.home/(actor+'.jsonl')), actor))
        self.repo, self.old = self.f.repo.resolve(), self.f.d.resolve()
        for actor in (ORIGIN, HUB):
            path = self.f.home/(actor+'.jsonl')
            path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=actor, cwd=str(self.repo))))+'\n')
        self.calls, self.fail_transport = [], False
        def native(home, target, text, **kwargs):
            self.assertEqual(Path(home), self.f.home)
            self.assertEqual(target, HUB)
            self.calls.append(text)
            if self.fail_transport:
                raise subprocess.TimeoutExpired('synthetic native queue', 1)
            return subprocess.CompletedProcess(['fixture'], 0, 'Synthetic queued receipt', ''), ORIGIN
        self.transport = patch('codex_session_adapter._queue', side_effect=native)
        self.transport.start(); self.addCleanup(self.transport.stop)
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            notifications.configure('user_input_only', 'Synthetic user completion once', completion='once')
            control.enable('Synthetic controller authority', 60, 0)
        self.finish(self.old)

    def finish(self, d, at=None):
        log = d/'evidence'/'verified.log'
        log.write_text('Synthetic immutable full verification payload\n')
        state = runner.read(d/'state.json')
        state.update(status='completed', owner_pid=None, active_process=None,
                     criteria={key: dict(status='passed', evidence=[str(log)]) for key in state['criteria']})
        runner.save(d/'state.json', state)
        with (d/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event='workflow_finished', status='completed',
                                         time=runner.now() if at is None else at))+'\n')
        control.register(d)

    def successor(self, *, extra=False, authorization=None, manual=False):
        contract = runner.read(self.old/'contract.json')
        contract['objective'] = 'Synthetic compatible final wording'
        if authorization:
            contract['authorization'] = authorization
        if extra:
            contract['acceptance'].append(dict(id='extra', description='Distinct further work', checks=[['true']]))
        if manual:
            contract['acceptance'][0] = dict(id='file', kind='manual', description='Actual fixture acknowledgement')
        plan = self.repo/'successor-plan.json'; runner.save(plan, contract)
        d = workflow.create(self.repo, plan, ORIGIN)
        self.finish(d)
        return d

    def lineage(self, a, b):
        proof = self.repo/(a.name+'-lineage.json')
        ac = runner.read(a/'contract.json'); bc = runner.read(b/'contract.json')
        mapping = {c['id']: c['id'] for c in ac['acceptance']}
        data = dict(predecessor_workflow=str(a), successor_workflow=str(b),
            predecessor_contract_sha256=workflow_lineage.sha(a/'contract.json'),
            successor_contract_sha256=workflow_lineage.sha(b/'contract.json'),
            permission_expanded=False, origin_thread=ORIGIN, hub_thread=HUB,
            acceptance_mapping=mapping, source='Synthetic explicit compatible delivery lineage')
        runner.save(proof, data)
        workflow_lineage.link(a, b, proof, 'Synthetic verified lineage registration')
        return proof

    def operation(self, a, b):
        receipt = self.repo/(a.name+'-operation.json')
        runner.save(receipt, identity.operation_template(a, b, 'synthetic-operation-'+a.name))
        for d in (a, b):
            state = runner.read(d/'state.json')
            for criterion in state['criteria'].values():
                criterion['evidence'].append(str(receipt))
            runner.save(d/'state.json', state)
        proof = self.repo/(a.name+'-same-delivery.json')
        runner.save(proof, identity.inspect(a, b, receipt)['expected_evidence'])
        before = self.immutable(a, b)
        identity.register(a, b, receipt, proof, 'Synthetic exact operation receipt and original authority')
        self.assertEqual(self.immutable(a, b), before)
        return receipt, proof

    def tick(self):
        before = len(self.calls)
        control.tick()
        return len(self.calls)-before

    def result(self, d):
        return next(a for a in control.rows(True) if a['kind'] == 'workflow_result' and a['workflow'] == str(d))

    def immutable(self, *directories):
        paths = [p for d in directories for p in d.rglob('*') if p.is_file()]
        paths += [p for p in self.f.home.iterdir() if p.is_file()]
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

    def cli(self, *args, ok=True, actor=ORIGIN):
        proc = subprocess.run([sys.executable, '-B', str(ROOT/'runner.py'), 'completion-delivery', *map(str, args)],
                              env=dict(os.environ, CODEX_THREAD_ID=actor), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode == 0, ok, proc.stdout+proc.stderr)
        return json.loads(proc.stdout) if ok else proc

    def test_verified_successor_reuses_old_notice_without_fake_presentation(self):
        self.assertEqual(self.tick(), 1)
        old = self.result(self.old)
        new = self.successor(); self.lineage(self.old, new)
        before = self.immutable(self.old, new)
        self.assertEqual(self.tick(), 0); self.assertEqual(self.tick(), 0)
        result = self.result(new)
        self.assertEqual(result['completion_delivery']['duplicate_of']['action_id'], old['id'])
        self.assertNotIn('notice', result); self.assertNotIn('presented_at', result)
        self.assertEqual(self.result(self.old)['notice'], old['notice'])
        self.assertEqual(self.immutable(self.old, new), before)

    def test_unknown_dispatch_does_not_replay_through_successor_id(self):
        self.fail_transport = True; self.assertEqual(self.tick(), 1)
        self.assertEqual(self.result(self.old)['notice']['state'], 'delivery_unknown')
        new = self.successor(); self.lineage(self.old, new)
        self.fail_transport = False
        self.assertEqual(self.tick(), 0)
        self.assertIn('duplicate_of', self.result(new)['completion_delivery'])

    def test_only_successor_emits_when_lineage_registered_before_any_tick(self):
        new = self.successor(); self.lineage(self.old, new)
        self.assertEqual(self.tick(), 1); self.assertEqual(self.tick(), 0)
        self.assertIn(self.result(new)['id'], self.calls[0])

    def test_multihop_verified_lineage_honors_first_unknown_claim(self):
        self.fail_transport = True; self.tick()
        second = self.successor(); third = self.successor()
        self.lineage(self.old, second); self.lineage(second, third)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(identity.identity(third)['members']), 3)

    def test_two_true_deliveries_with_equal_repo_title_commands_and_payload_stay_independent(self):
        second = self.successor()
        # Equal acceptance bytes, command, repository and authorization are
        # intentionally insufficient without the explicit verified relation.
        self.assertEqual(self.tick(), 2); self.assertEqual(self.tick(), 0)
        self.assertNotIn('duplicate_of', self.result(second)['completion_delivery'])

    def test_extra_acceptance_is_distinct_work_even_in_compatible_lineage(self):
        self.assertEqual(self.tick(), 1)
        new = self.successor(extra=True); self.lineage(self.old, new)
        self.assertEqual(self.tick(), 1)
        self.assertNotIn('duplicate_of', self.result(new)['completion_delivery'])

    def test_tampered_lineage_blocks_new_notification_without_erasing_outbox(self):
        self.tick(); new = self.successor(); proof = self.lineage(self.old, new)
        old = self.result(self.old)['notice']
        proof.write_text('{}')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.result(new)['completion_delivery']['validation'], 'invalid')
        self.assertEqual(self.result(self.old)['notice'], old)

    def test_ambiguous_lineage_assignment_fails_closed(self):
        new = self.successor(); self.lineage(self.old, new)
        other = self.successor()
        runner.save(other/'workflow-lineage.json', dict(successor=str(new)))
        self.assertEqual(self.tick(), 0)
        self.assertEqual(identity.identity(new)['validation'], 'invalid')

    def test_generic_platform_shared_operation_receipt_deduplicates_exact_delivery(self):
        self.tick(); new = self.successor()
        self.operation(self.old, new)
        before = self.immutable(self.old, new)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(identity.identity(new)['relations'][0]['kind'], 'receipt')
        self.assertEqual(self.immutable(self.old, new), before)
        self.assertNotIn('presented_at', self.result(new))

    def test_shared_receipt_before_first_tick_sends_exactly_once(self):
        new = self.successor(); self.operation(self.old, new)
        self.assertEqual(self.tick(), 1); self.assertEqual(self.tick(), 0)
        self.assertEqual(sum('notice' in a for a in control.rows(True) if a['kind'] == 'workflow_result'), 1)

    def test_two_explicit_physical_operations_with_equal_verification_each_send_once(self):
        a, b, c = self.successor(), self.successor(), self.successor()
        self.operation(self.old, a)
        self.operation(b, c)
        self.assertNotEqual(identity.identity(a)['identity'], identity.identity(c)['identity'])
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.tick(), 0)

    def test_receipt_dedup_unknown_claim_and_restart_without_action_notice(self):
        self.tick(); new = self.successor(); self.operation(self.old, new)
        original = self.result(self.old)
        original.pop('notice')
        with control.database() as con, con:
            control.put(con, original)
            key = 'followup:'+original['id']+':0'
            row = json.loads(con.execute('select body from outbox where key=?', (key,)).fetchone()[0])
            row['state'] = 'dispatching'  # Crash after durable claim, before receipt.
            row['authority'] = 'legacy_submission'  # Imported pre-mailbox claim.
            con.execute('update outbox set body=? where key=?', (json.dumps(row), key))
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.result(new)['completion_delivery']['duplicate_of']['delivery_state'], 'dispatching')

    def test_legacy_presented_result_suppresses_without_copying_presented_timestamp(self):
        self.tick(); original = self.result(self.old)
        original['presented_at'] = runner.now(); original['state'] = 'resolved'
        with control.database() as con, con: control.put(con, original)
        new = self.successor(); self.operation(self.old, new)
        self.assertEqual(self.tick(), 0); self.assertNotIn('presented_at', self.result(new))

    def test_historical_physical_delivery_is_not_backfilled_with_fresh_successor(self):
        with (self.old/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event='workflow_finished', status='completed', time=runner.now()-1000))+'\n')
        new = self.successor(); self.operation(self.old, new)
        self.assertEqual(self.tick(), 0)
        self.assertNotIn('notice', self.result(new))

    def test_receipt_tamper_blocks_both_notices(self):
        new = self.successor(); receipt, _ = self.operation(self.old, new)
        data = runner.read(receipt); data['operation_id'] += '-different'; runner.save(receipt, data)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(identity.identity(new)['validation'], 'invalid')

    def test_completion_evidence_drift_blocks_new_notice(self):
        new = self.successor(); self.operation(self.old, new)
        (new/'evidence'/'verified.log').write_text('Synthetic different deployment verification')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(identity.identity(new)['validation'], 'invalid')

    def test_wrong_authority_cannot_share_receipt_even_with_equal_payload(self):
        new = self.successor(authorization='Synthetic independently expanded production authority')
        with self.assertRaisesRegex(ValueError, 'authority'):
            self.operation(self.old, new)
        self.assertEqual(self.tick(), 2)

    def test_different_payload_or_partial_acceptance_cannot_share_receipt(self):
        new = self.successor(extra=True)
        with self.assertRaisesRegex(ValueError, 'complete acceptance'):
            self.operation(self.old, new)
        new = self.successor()
        (new/'evidence'/'verified.log').write_text('Another physical operation')
        with self.assertRaisesRegex(ValueError, 'verification payload'):
            self.operation(self.old, new)

    def test_receipt_must_be_shared_in_both_actual_completed_results(self):
        new = self.successor()
        receipt = self.repo/'not-shared.json'
        runner.save(receipt, identity.operation_template(self.old, new, 'synthetic-unshared'))
        with self.assertRaisesRegex(ValueError, 'both completed'):
            identity.inspect(self.old, new, receipt)

    def test_symlink_proof_or_receipt_and_conflicting_assignment_rejected(self):
        new = self.successor(); receipt, proof = self.operation(self.old, new)
        alias = self.repo/'receipt-alias'; alias.symlink_to(receipt)
        with self.assertRaises(ValueError): identity.inspect(self.old, new, alias)
        alias.unlink(); alias.symlink_to(proof)
        with self.assertRaises(ValueError): identity.register(self.old, new, receipt, alias, 'Alias rejected')
        other = self.successor()
        with self.assertRaises(ValueError): self.operation(self.old, other)

    def test_stop_generation_and_manual_ack_are_never_bypassed(self):
        new = self.successor(); receipt, _ = self.operation(self.old, new)
        (new/'STOP').touch()
        self.assertEqual(self.tick(), 0)
        (new/'STOP').unlink()
        s = runner.read(new/'state.json'); s['execution_revision'] = s.get('execution_revision', 0)+1; runner.save(new/'state.json', s)
        self.assertEqual(self.tick(), 0)
        manual = self.successor(manual=True)
        with self.assertRaises((ValueError, OSError)):
            identity.operation_template(manual, new, 'synthetic-no-ack')

    def test_project_mute_and_zero_reminders_survive_receipt_registration(self):
        new = self.successor(); self.operation(self.old, new)
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            notifications.configure_project(self.repo, True, 'Synthetic Drover priority mute')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(control.settings()['max_reminders'], 0)
        self.assertTrue(all('notice' not in a for a in control.rows(True) if a['kind'] == 'workflow_result'))

    def test_cli_inspect_registration_status_and_foreign_actor(self):
        new = self.successor()
        receipt = self.repo/'cli-operation.json'
        runner.save(receipt, self.cli('operation-template', self.old, '--successor', new, '--operation-id', 'synthetic-cli'))
        for d in (self.old, new):
            state = runner.read(d/'state.json'); state['criteria']['file']['evidence'].append(str(receipt)); runner.save(d/'state.json', state)
        proof = self.repo/'cli-proof.json'
        runner.save(proof, self.cli('inspect', self.old, '--successor', new, '--receipt', receipt)['expected_evidence'])
        args = ('register', self.old, '--successor', new, '--receipt', receipt, '--evidence', proof, '--source', 'Synthetic CLI exact evidence')
        self.cli(*args, actor='00000000-0000-4000-8000-000000000001', ok=False)
        first = self.cli(*args); self.assertEqual(first, self.cli(*args))
        self.assertFalse(self.cli('status', new)['execution_authority'])
        self.assertEqual(self.tick(), 1)

    def test_atomic_outbox_recheck_stops_stale_second_eligibility(self):
        new = self.successor(); self.operation(self.old, new)
        # Observe both before either durable claim exists, then send using the
        # exact stale render. The send transaction must still deduplicate.
        cfg = control.settings()
        with control.database() as con:
            rows = [json.loads(r[0]) for r in con.execute('select body from workflows')]
        for row in rows: control.workflow_observation(row)
        a, b = self.result(self.old), self.result(new)
        self.assertTrue(notices.eligible(a)); self.assertTrue(notices.eligible(b))
        for action in (a, b):
            control.send('followup:'+action['id']+':0', HUB, notices.render(action), workflow=action['workflow'])
        self.assertEqual(len(self.calls), 1)
        with control.database() as con:
            receipts = [json.loads(r[0]) for r in con.execute('select body from outbox')]
        self.assertTrue(any(r.get('reason') == 'same_delivery_already_claimed' for r in receipts))

    def test_wrong_native_home_and_origin_identity_invalidate_receipt_relation(self):
        new = self.successor(); self.operation(self.old, new)
        other_home = self.repo/'wrong-native'; other_home.mkdir()
        with patch.dict(os.environ, CODEX_HOME=str(other_home)):
            self.assertEqual(identity.identity(new)['validation'], 'invalid')
        meta = runner.read(new/'origin.json'); meta['origin_thread_id'] = HUB
        runner.save(new/'origin.json', meta)
        self.assertEqual(self.tick(), 0)

    def test_snapshot_proof_tamper_and_ambiguous_criteria_fail_closed(self):
        new = self.successor(); _, proof = self.operation(self.old, new)
        raw = runner.read(proof); raw['operation']['authority_sha256'] = '0'*64
        runner.save(proof, raw)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(identity.identity(new)['validation'], 'invalid')
        # Two indistinguishable acceptance rows do not establish a unique
        # complete mapping, even if every artifact hash is the same.
        for d in (self.old, new):
            c = runner.read(d/'contract.json'); item = dict(c['acceptance'][0], id='duplicate')
            c['acceptance'].append(item); runner.save(d/'contract.json', c)
            s = runner.read(d/'state.json'); s['contract_hash'] = runner.digest(c)
            s['criteria']['duplicate'] = copy.deepcopy(s['criteria']['file']); runner.save(d/'state.json', s)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            identity.operation_template(self.old, new, 'synthetic-ambiguous-mapping')

    def test_proof_change_between_projection_and_send_does_not_crash_or_dispatch(self):
        new = self.successor(); self.operation(self.old, new)
        with patch('completion_notifications.reserve', side_effect=ValueError('synthetic proof drift')):
            self.assertEqual(self.tick(), 0)
        self.assertTrue(all(a['completion_delivery']['validation'] == 'invalid'
                            for a in control.rows(True) if a['kind'] == 'workflow_result'))

    def test_neutral_scoped_actor_can_register_without_native_identity_in_general_code(self):
        import local_session_adapter as local
        origin_key, hub_key, user_key = [self.repo/name for name in ('origin-key.json', 'hub-key.json', 'user-key.json')]
        origin = local.create(self.repo, 'origin', 'Synthetic neutral origin', origin_key)['reference']
        hub = local.create(self.repo, 'hub', 'Synthetic neutral hub', hub_key, user_key)['reference']
        interaction.configure_session(hub, 'Synthetic neutral hub', actor_proof=local.load_actor_file(hub_key)[0])
        plan = self.repo/'neutral-plan.json'; runner.save(plan, runner.read(self.old/'contract.json'))
        with patch('control.register'):
            a, b = [workflow.create(self.repo, plan, origin_ref=origin) for _ in range(2)]
        grant = self.repo/'neutral-workflow-grant.json'
        local.grant(origin_key, a, grant, 'Synthetic exact workflow observation grant')
        with patch('control.register'):
            self.finish(a); self.finish(b)
        actor, reference, _ = local.load_actor_file(grant)
        kwargs = dict(actor_proof=actor, reference=reference)
        receipt = self.repo/'neutral-operation.json'
        runner.save(receipt, identity.operation_template(a, b, 'synthetic-neutral-operation', **kwargs))
        for d in (a, b):
            s = runner.read(d/'state.json'); s['criteria']['file']['evidence'].append(str(receipt)); runner.save(d/'state.json', s)
        proof = self.repo/'neutral-observation.json'
        runner.save(proof, identity.inspect(a, b, receipt, **kwargs)['expected_evidence'])
        args = ('register', a, '--successor', b, '--receipt', receipt, '--evidence', proof,
                '--source', 'Synthetic neutral scoped evidence')
        self.cli(*args, '--actor-file', origin_key, ok=False)
        self.cli(*args, '--actor-file', grant)
        self.assertEqual(len(identity.identity(b)['members']), 2)
        self.assertFalse(identity.identity(b)['execution_authority'])

    def test_real_manual_acknowledgements_are_pinned_then_drift_blocks_notice(self):
        a, b = self.successor(manual=True), self.successor(manual=True)
        for d in (a, b):
            log = d/'evidence'/'verified.log'
            workflow.acknowledge(d, 'file', log, 'Synthetic explicit human acceptance')
            s = runner.read(d/'state.json')
            s['criteria']['file']['evidence'] = [str(d/'acknowledgements'/'file.json')]
            runner.save(d/'state.json', s)
        self.operation(a, b)
        self.assertEqual(identity.identity(b)['validation'], 'valid')
        ack = runner.read(a/'acknowledgements'/'file.json'); ack['note'] += ' changed'
        runner.save(a/'acknowledgements'/'file.json', ack)
        self.assertEqual(identity.identity(b)['validation'], 'invalid')
        # The unrelated original delivery retains its own once-only notice.
        self.assertEqual(self.tick(), 1)

    def test_forged_result_id_cannot_use_trusted_completion_transport(self):
        row = dict(id='result-'+'0'*20, kind='workflow_result', workflow=str(self.old),
                   origin=ORIGIN, hub=HUB, state='unpresented', created_at=runner.now(), reminders=0,
                   baseline_generation=recovery.generation(self.old, runner.read(self.old/'state.json')))
        with control.database() as con, con: control.put(con, row)
        self.assertFalse(notices.eligible(row))
        receipt = control.send('followup:'+row['id']+':0', HUB, notices.render(row), workflow=str(self.old))
        self.assertEqual(receipt['state'], 'delivery_unknown')
        self.assertEqual(self.calls, [])


class ApprovedDeliveryCompletionIntegrationTests(unittest.TestCase):
    def test_approved_delivery_does_not_alias_entire_predecessor_or_distinct_later_work(self):
        import test_approved_delivery as fixtures_approved
        f = fixtures_approved.ApprovedDeliveryTests('runTest'); f.setUp()
        try:
            f.register()
            with patch.dict(os.environ, CODEX_THREAD_ID=fixtures_approved.HUB):
                notifications.configure_project(f.repo, False, 'Synthetic once notices enabled')
                notifications.configure('user_input_only', 'Synthetic completion once', completion='once')
            new = f.new
            with (new/'events.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(event='workflow_finished', status='completed', time=runner.now()))+'\n')
            control.register(new)
            later = f.create_delivery()
            with (later/'events.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(event='workflow_finished', status='completed', time=runner.now()))+'\n')
            control.register(later)
            receipt = (subprocess.CompletedProcess(['fixture'], 0, '', ''), fixtures_approved.ORIGIN)
            with patch('codex_session_adapter._queue', return_value=receipt) as send:
                control.tick(); self.assertEqual(send.call_count, 2)
                control.tick(); self.assertEqual(send.call_count, 2)
            self.assertEqual(runner.read(f.old/'state.json')['status'], 'blocked')
            self.assertEqual(approved_delivery.projection(f.old)['relations'][0]['validation'], 'valid')
            self.assertEqual(len(identity.identity(new)['members']), 1)
            self.assertEqual(len(identity.identity(later)['members']), 1)
        finally:
            f.doCleanups()


if __name__ == '__main__':
    unittest.main()
