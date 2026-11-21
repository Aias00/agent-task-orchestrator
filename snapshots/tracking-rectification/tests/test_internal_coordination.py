"""Private canonical material receipts; synthetic identities and offline public CLI.

All roots are private temporary ATO_HOME/CODEX_HOME. No native queue/model,
credential/config lookup, daemon or actual user inbox is used.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_neutral_hub as fixtures
import agent_registry
import internal_coordination as internal
import interaction
import local_session_adapter as local
import notifications
import runner
import transport

ROOT = Path(__file__).resolve().parents[1]


class InternalCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NeutralHubTests('runTest'); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.repo, self.d = self.f.repo, self.f.workflow
        self.material = self.repo/'material.txt'; self.material.write_text('synthetic diagnostic evidence\n')
        self.origin = self.credentials(self.f.origin_grant)
        self.hub = self.credentials(self.f.hub_grant)
        self.block_send = patch('session_router.send', side_effect=AssertionError('no native send'))
        self.block_send.start(); self.addCleanup(self.block_send.stop)

    @staticmethod
    def credentials(path):
        proof, ref, _ = local.load_actor_file(path)
        return dict(actor_proof=proof, reference=ref)

    def submit(self, **kwargs):
        args = dict(directory=self.d, request_id='material-request-1', artifact_path=self.material,
                    summary='Explicitly requested synthetic report', source='synthetic requested submission')
        args.update(kwargs)
        return internal.submit(**args, **self.origin)

    def read(self, row, **kwargs):
        return internal.read_report(self.d, row['id'], 'actual synthetic hub read', **(kwargs or self.hub))

    def test_atomic_admission_stable_id_and_separate_read_assignment(self):
        row = self.submit(); again = self.submit()
        self.assertEqual(row, again)
        self.assertEqual(row['submission_state'], 'queued')
        self.assertEqual(row['inbox_state'], 'available')
        self.assertIsNone(row['read_receipt']); self.assertIsNone(row['assignment'])
        self.assertEqual(row['wakeup']['state'], 'unsupported')
        self.assertEqual(row['user_notice']['state'], 'not_requested')
        listed = internal.inbox(self.d, **self.hub)
        self.assertEqual(len(listed['reports']), 1); self.assertFalse(listed['receipt_created'])
        read = self.read(row)
        self.assertEqual(read['artifact_text'], self.material.read_text())
        self.assertEqual(read['inbox_state'], 'read'); self.assertFalse(read['actual_execution'])
        assigned = internal.assign(self.d, row['id'], 'inspect this exact report for 60 seconds',
            'explicit synthetic diagnostic assignment', 60, **self.hub)
        self.assertEqual(assigned['assignment_state'], 'assigned')
        self.assertEqual(assigned['handling_progress'], 'unverified')
        self.assertFalse(assigned['assignment']['execution_grant'])
        self.assertEqual(assigned['assignment']['generation'], row['generation'])
        self.assertEqual(assigned['artifact']['sha256'], hashlib.sha256(self.material.read_bytes()).hexdigest())

    def test_listing_does_not_create_read_or_presented_or_user_question(self):
        row = self.submit()
        for _ in range(2):
            listed = internal.inbox(self.d, **self.hub)['reports'][0]
            self.assertEqual(listed['inbox_state'], 'available')
            self.assertNotIn('presented_at', listed)
        self.assertFalse((interaction.directory()/'questions').exists())
        self.assertFalse(notifications.disposition(dict(kind='internal_report'))['notify'])

    def test_no_actor_proof_or_wrong_role_cannot_read_assign_or_submit(self):
        row = self.submit()
        with self.assertRaises(ValueError): internal.inbox(self.d)
        with self.assertRaises(ValueError): self.read(row, **self.origin)
        with self.assertRaises(ValueError): internal.submit(self.d, 'x', self.material, 'summary', 'source', **self.hub)
        with self.assertRaises(ValueError): internal.assign(self.d, row['id'], 'inspect', 'source', **self.origin)
        with self.assertRaises(ValueError): internal.inbox(self.d, role='worker', **self.hub)
        with self.assertRaises(ValueError): internal.inbox(self.d, role='user', **self.hub)

    def test_assignment_requires_read_and_does_not_renew_expiry(self):
        row = self.submit()
        with self.assertRaises(ValueError): internal.assign(self.d, row['id'], 'inspect', 'source', 60, **self.hub)
        self.read(row)
        assigned = internal.assign(self.d, row['id'], 'inspect', 'source', 60, **self.hub)
        with patch('runner.now', return_value=assigned['assignment']['expires_at']+1):
            again = internal.assign(self.d, row['id'], 'inspect', 'source', 60, **self.hub)
            self.assertEqual(again['assignment'], assigned['assignment'])
            self.assertEqual(again['assignment_state'], 'expired')
            with self.assertRaises(ValueError): internal.assign(self.d, row['id'], 'inspect', 'source', 61, **self.hub)
        for seconds in (0, -1, True, 901, float('inf')):
            with self.assertRaises(ValueError): internal.assign(self.d, row['id'], 'inspect', 'source', seconds, **self.hub)

    def test_read_is_deduplicated_and_never_claims_user_presentation(self):
        row = self.submit(); a = self.read(row); b = self.read(row)
        self.assertEqual(a['read_receipt'], b['read_receipt'])
        self.assertNotIn('presented_at', a)
        self.assertIn('not user presentation', a['read_receipt']['meaning'])
        self.assertEqual(transport.managed_bus().message(row['mailbox_id'])['state'], 'queued')

    def test_conflicting_dedup_artifact_summary_source_are_rejected(self):
        row = self.submit()
        for change in (dict(summary='changed'), dict(source='changed')):
            with self.assertRaises(ValueError): self.submit(**change)
        self.material.write_text('changed report')
        with self.assertRaises(ValueError): self.submit()
        self.assertEqual(len(internal.inbox(self.d, **self.hub)['reports']), 1)
        self.assertEqual(transport.managed_bus().message(row['mailbox_id'])['state'], 'queued')

    def test_path_escape_symlink_and_private_config_fail_closed(self):
        outside = self.f.root/'outside.txt'; outside.write_text('outside')
        link = self.repo/'link'; link.symlink_to(self.material)
        directory = self.repo/'alias'; directory.symlink_to(self.repo, target_is_directory=True)
        forbidden = self.repo/'.env'; forbidden.write_text('must not be copied')
        for path in (outside, link, directory/'material.txt', '../outside.txt', forbidden):
            with self.subTest(path=str(path)), self.assertRaises((OSError, ValueError)):
                self.submit(artifact_path=path)
        self.assertEqual(internal.inbox(self.d, **self.hub)['reports'], [])

    def test_nonregular_binary_and_oversized_payload_fail_before_submission(self):
        directory = self.repo/'directory'; directory.mkdir()
        binary = self.repo/'binary'; binary.write_bytes(b'\xff')
        huge = self.repo/'huge'; huge.write_bytes(b'x'*(internal.MAX_ARTIFACT+1))
        for path in (directory, binary, huge):
            with self.assertRaises((OSError, ValueError)): self.submit(artifact_path=path)
        with self.assertRaises(ValueError): self.submit(summary='x'*(internal.MAX_SUMMARY+1))
        with self.assertRaises(ValueError): self.submit(source='x'*513)
        self.assertEqual(internal.inbox(self.d, **self.hub)['reports'], [])

    def test_source_and_snapshot_tampering_block_receipts(self):
        row = self.submit(); self.material.write_text('tampered')
        with self.assertRaises(ValueError): self.read(row)
        self.material.write_text('synthetic diagnostic evidence\n')
        bus = internal.database(False)
        with bus._transaction() as con:
            con.execute('update internal_reports set artifact=? where id=?', (b'tampered snapshot', row['id']))
        with self.assertRaises(ValueError): self.read(row)

    def test_canonical_envelope_tampering_cannot_acknowledge(self):
        row = self.submit(); bus = internal.database(False)
        with bus._transaction() as con:
            con.execute('update bodies set body=? where message_id=?', ('{}', row['mailbox_id']))
        with self.assertRaises(ValueError): self.read(row)

    def test_wrong_mailbox_sender_cannot_forge_readable_material(self):
        row = self.submit(); bus = internal.database(False)
        with bus._transaction() as con:
            con.execute('update messages set sender=target where id=?', (row['mailbox_id'],))
        with self.assertRaises(ValueError): self.read(row)

    def test_stale_generation_and_wrong_workflow_proof_fail_closed(self):
        row = self.submit()
        state = runner.read(self.d/'state.json'); state['execution_revision'] = 1
        runner.save(self.d/'state.json', state)
        with self.assertRaises(ValueError): self.read(row)
        with self.assertRaises(ValueError): self.submit()
        new_grant = self.f.grant(self.f.hub_key, 'fresh-hub.json')
        fresh = self.credentials(new_grant)
        view = internal.inbox(self.d, **fresh)['reports'][0]
        self.assertEqual(view['binding_state'], 'stale')
        with self.assertRaises(ValueError): internal.read_report(self.d, row['id'], 'source', **fresh)
        # Actor proof is bound to the complete immutable workflow path.
        fake = self.repo/'other'; fake.mkdir()
        for name in ('state.json', 'contract.json', 'origin.json'):
            (fake/name).write_bytes((self.d/name).read_bytes())
        with self.assertRaises(ValueError): internal.inbox(fake, **fresh)

    def test_stop_preserves_inbox_but_forbids_new_submission_and_assignment(self):
        row = self.submit(); self.read(row)
        (self.d/'STOP').write_text('synthetic stop')
        with self.assertRaises(ValueError): self.submit(request_id='second')
        with self.assertRaises(ValueError): internal.assign(self.d, row['id'], 'inspect', 'source', **self.hub)
        self.assertEqual(len(internal.inbox(self.d, **self.hub)['reports']), 1)
        self.assertEqual((self.d/'STOP').read_text(), 'synthetic stop')

    def test_unknown_delivery_and_crash_barrier_never_resubmit(self):
        row = self.submit(); bus = transport.managed_bus()
        msg = bus.message(row['mailbox_id']); ep = bus.endpoint(msg['target'])
        cred = transport.host_endpoint(bus, kind=ep['kind'], native_id=ep['session_id'], project=ep['project'], source=ep['source'])
        owner = bus.acquire_endpoint(cred, owner='synthetic crash', lease_seconds=1)
        claimed = bus.claim(cred, owner, message_id=msg['id'])
        bus.begin_dispatch(cred, owner, msg['id'], claimed['claim_token'])
        with patch.object(bus, 'clock', return_value=runner.now()+2): bus.recover()
        again = self.submit()
        self.assertEqual(again['mailbox_id'], row['mailbox_id'])
        self.assertEqual(again['submission_state'], 'delivery_unknown')
        self.assertIsNone(again['read_receipt'])
        self.assertEqual(len(internal.inbox(self.d, **self.hub)['reports']), 1)

    def test_atomic_crash_rolls_back_both_report_and_mailbox(self):
        from message_bus import MessageBus
        original = MessageBus._enqueue
        def crash(bus, *a, **kw):
            original(bus, *a, **kw)
            raise KeyboardInterrupt('synthetic process death before commit')
        with patch.object(MessageBus, '_enqueue', crash):
            with self.assertRaises(KeyboardInterrupt): self.submit()
        bus = internal.database(False)
        with bus._transaction() as con:
            self.assertEqual(con.execute('select count(*) from internal_reports').fetchone()[0], 0)
            self.assertEqual(con.execute("select count(*) from messages where kind='internal_report'").fetchone()[0], 0)
        row = self.submit(); self.assertEqual(row['submission_state'], 'queued')

    def test_crash_after_commit_returns_same_durable_request_on_retry(self):
        with patch('internal_coordination.projection', side_effect=KeyboardInterrupt('synthetic response loss')):
            with self.assertRaises(KeyboardInterrupt): self.submit()
        listed = internal.inbox(self.d, **self.hub)['reports']
        self.assertEqual(len(listed), 1)
        again = self.submit()
        self.assertEqual(again['mailbox_id'], listed[0]['mailbox_id'])
        self.assertIsNone(again['read_receipt'])

    def test_unknown_id_is_not_a_read_receipt(self):
        self.submit()
        for identifier in ('internal-'+'0'*32, '../escape', 'recorded_only-report'):
            with self.assertRaises(ValueError): internal.read_report(self.d, identifier, 'source', **self.hub)
        self.assertIsNone(internal.inbox(self.d, **self.hub)['reports'][0]['read_receipt'])

    def test_no_legacy_recorded_only_upgrade_or_user_notice_backfill(self):
        old = interaction.directory()/'old-report.json'
        runner.save(old, dict(state='recorded_only', text='historical synthetic report'))
        before = old.read_bytes(); self.submit()
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(len(internal.inbox(self.d, **self.hub)['reports']), 1)

    def test_public_neutral_cli_submit_list_read_assign_and_wrong_actor(self):
        row = self.f.json_cli('internal-report', 'submit', self.d, '--request-id', 'cli-1',
            '--artifact', self.material, '--summary', 'synthetic material', '--source', 'explicit fixture request',
            '--actor-file', self.f.origin_grant)
        inbox = self.f.json_cli('internal-report', 'inbox', self.d, '--actor-file', self.f.hub_grant)
        self.assertEqual(inbox['reports'][0]['id'], row['id'])
        self.f.cli('internal-report', 'read', self.d, '--id', row['id'], '--source', 'wrong actor',
            '--actor-file', self.f.origin_grant, ok=False)
        read = self.f.json_cli('internal-report', 'read', self.d, '--id', row['id'], '--source', 'actual fixture read',
            '--actor-file', self.f.hub_grant)
        self.assertEqual(read['inbox_state'], 'read')
        assigned = self.f.json_cli('internal-report', 'assign', self.d, '--id', row['id'],
            '--source', 'actual fixture assignment', '--next-action', 'bounded diagnosis', '--seconds', 60,
            '--actor-file', self.f.hub_grant)
        self.assertEqual(assigned['assignment_state'], 'assigned')
        self.assertFalse(assigned['actual_execution'])

    def test_parallel_cli_submissions_have_one_canonical_material(self):
        args = [sys.executable, str(ROOT/'runner.py'), 'internal-report', 'submit', str(self.d),
            '--request-id', 'parallel', '--artifact', str(self.material), '--summary', 'same report',
            '--source', 'same explicit request', '--actor-file', str(self.f.origin_grant)]
        one = subprocess.Popen(args, env=self.f.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        two = subprocess.Popen(args, env=self.f.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        a, ae = one.communicate(timeout=20); b, be = two.communicate(timeout=20)
        self.assertEqual(one.returncode, 0, ae); self.assertEqual(two.returncode, 0, be)
        self.assertEqual(json.loads(a)['mailbox_id'], json.loads(b)['mailbox_id'])
        self.assertEqual(len(internal.inbox(self.d, **self.hub)['reports']), 1)

    def test_listing_preserves_database_bytes_and_creates_no_receipts(self):
        row = self.submit()
        bus = internal.database(False)
        before = bus.path.read_bytes()
        with bus._transaction() as con:
            records = [tuple(r) for r in con.execute('select * from internal_reports')]
            receipts = [tuple(r) for r in con.execute('select * from receipts')]
        internal.inbox(self.d, **self.hub)
        with bus._transaction() as con:
            self.assertEqual(records, [tuple(r) for r in con.execute('select * from internal_reports')])
            self.assertEqual(receipts, [tuple(r) for r in con.execute('select * from receipts')])
        self.assertEqual(before, bus.path.read_bytes())

    def test_assignment_seal_tampering_cannot_extend_handling(self):
        row = self.submit(); self.read(row)
        assigned = internal.assign(self.d, row['id'], 'inspect', 'source', 60, **self.hub)
        forged = assigned['assignment']; forged['expires_at'] += 36000
        bus = internal.database(False)
        with bus._transaction() as con:
            con.execute('update internal_reports set assignment=? where id=?', (json.dumps(forged), row['id']))
        with self.assertRaises(ValueError): internal.inbox(self.d, **self.hub)
        with self.assertRaises(ValueError): internal.assign(self.d, row['id'], 'inspect', 'source', 60, **self.hub)

    def test_material_assignment_and_read_are_not_workflow_execution_claim(self):
        import task_responsibility
        row = self.submit(); self.read(row)
        internal.assign(self.d, row['id'], 'inspect', 'source', 60, **self.hub)
        state = runner.read(self.d/'state.json')
        handler = task_responsibility.handler(self.d, state, self.f.origin)
        self.assertEqual(handler['status'], 'unhandled')
        self.assertFalse(task_responsibility.view(self.d)['obligations'])
        self.assertEqual(runner.read(self.d/'state.json'), state)

    def test_necessary_question_keeps_its_canonical_route(self):
        row = self.submit()
        question = self.f.ask('Choose between two synthetic diagnostic options?')
        listed = self.f.json_cli('inbox', '--actor-file', self.f.hub_grant)
        self.assertEqual([q['id'] for q in listed], [question['id']])
        self.assertIsNone(internal.inbox(self.d, **self.hub)['reports'][0]['read_receipt'])
        self.assertEqual(self.f.question_record(question)['state'], 'pending')

    def test_private_store_permissions_and_no_transport_credentials_in_result(self):
        row = self.submit()
        encoded = json.dumps(row)
        for key in ('claim_token', 'owner_token', 'proof', 'token_hash'):
            self.assertNotIn('"'+key+'"', encoded)
        store = transport.managed_bus().path
        self.assertEqual(store.stat().st_mode & 0o077, 0)
        self.assertEqual(self.read(row)['artifact']['bytes'], len(self.material.read_bytes()))


if __name__ == '__main__':
    unittest.main()
