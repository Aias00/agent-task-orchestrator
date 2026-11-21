"""Approved scope observations through public APIs/CLI, using synthetic private state."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import approved_delivery as approved
import control
import interaction
import notifications
import recovery
import runner
import workflow
import workflow_lineage

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = '71111111-1111-4111-8111-111111111111'
HUB = '72222222-2222-4222-8222-222222222222'
FOREIGN = '73333333-3333-4333-8333-333333333333'


class ApprovedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-approved-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'project'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)
        self.native = self.root/'native'; self.native.mkdir()
        self.called = self.root/'forbidden-queue-call'
        self.queue = self.root/'deny-queue'
        self.queue.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(self.called)!r}).touch()\nraise SystemExit(93)\n')
        self.queue.chmod(0o700)
        env = dict(ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.native), CODEX_THREAD_ID=ORIGIN,
                   CTASK_QUEUE_BIN=str(self.queue), PYTHONDONTWRITEBYTECODE='1')
        self.envpatch = patch.dict(os.environ, env)
        self.envpatch.start(); self.addCleanup(self.envpatch.stop)
        for name in ('CTASK_SESSION_REF', 'ATO_SESSION_REF', 'ATO_ACTOR_FILE', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            os.environ.pop(name, None)
        self.headers = {}
        with sqlite3.connect(self.native/'state_5.sqlite') as con:
            con.execute('create table threads(id text, rollout_path text)')
            for actor in (ORIGIN, HUB, FOREIGN):
                path = self.native/(actor+'.jsonl')
                path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=actor,
                    cwd=str(self.repo if actor == ORIGIN else self.root))))+'\n')
                con.execute('insert into threads values(?,?)', (actor, str(path)))
                self.headers[actor] = path
        self.plan = self.root/'original-plan.json'
        runner.save(self.plan, dict(objective='Deliver fixture production and retain external qualification',
            scope='Isolated fixture analysis', authorization='Fixture analysis authority only', memory=False,
            acceptance=[dict(id=x, description='Synthetic '+x, checks=[['true']])
                        for x in ('production', 'external-source', 'measurement')]))
        self.old = workflow.create(self.repo, self.plan, ORIGIN)
        state = runner.read(self.old/'state.json'); state['status'] = 'blocked'
        state['criteria'] = {x: dict(status='failed', evidence=[]) for x in state['criteria']}
        runner.save(self.old/'state.json', state)
        interaction.configure(HUB, 'Synthetic fixture user selected hub')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            notifications.configure('user_input_only', 'Fixture quiet routine progress')
            notifications.configure_project(self.repo, True, 'Fixture no messages')
            control.enable('Fixture observation only', 60, 0)
        q = interaction.ask(self.old, 'Approve separate synthetic production delivery?', 'Retain external checks')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB), patch('codex_session_adapter.queue',
                side_effect=subprocess.TimeoutExpired('synthetic fixture transport fault', 0)):
            interaction.answer(q['id'], 'Approve the separately described fixture production scope', 'Synthetic explicit user answer fixture:1')
        interaction.accept(q['id'])
        self.qid = q['id']; self.qpath = interaction.question_path(self.qid)
        self.new = self.create_delivery()
        self.evidence = self.root/'approved-proof.json'
        self.refresh_proof()
        self.addCleanup(self.assert_no_send)

    def assert_no_send(self):
        self.assertFalse(self.called.exists(), 'fixture attempted an actual queue request')

    def create_delivery(self, manual=False):
        data = runner.read(self.plan)
        data.update(objective='Separately authorized synthetic production delivery',
            authorization='Synthetic accepted fixture production authorization', scope='Fixture production operation only',
            acceptance=[dict(id='deploy', description='Synthetic deploy verified', checks=[['true']]),
                        dict(id='public', description='Synthetic public check verified', checks=[['true']])])
        if manual:
            data['acceptance'].append(dict(id='user-ack', kind='manual', description='Actual fixture user acknowledgement'))
        path = self.root/('manual-plan.json' if manual else 'delivery-plan.json'); runner.save(path, data)
        d = workflow.create(self.repo, path, ORIGIN)
        log = d/'evidence'/'verified.txt'; log.write_text('Synthetic acceptance passed; no business operation')
        if manual:
            workflow.acknowledge(d, 'user-ack', log, 'Synthetic user explicitly accepted this fixture result')
        state = runner.read(d/'state.json')
        state.update(status='completed', criteria={c['id']: dict(status='passed', evidence=[str(log)]) for c in data['acceptance']})
        if manual:
            state['criteria']['user-ack']['evidence'] = [str(d/'acknowledgements'/'user-ack.json')]
        runner.save(d/'state.json', state)
        return d

    def refresh_proof(self):
        self.proof = approved.inspect(self.old, self.qid, self.new)['expected_evidence']
        self.proof.update(approved_criteria={'production': ['deploy', 'public']},
                          deferred_criteria=['external-source', 'measurement'])
        runner.save(self.evidence, self.proof)

    def register(self):
        return approved.register(self.old, self.qid, self.new, self.evidence, 'Synthetic exact observation')

    def cli(self, *argv, actor=ORIGIN, ok=True, **env):
        proc = subprocess.run([sys.executable, '-B', str(ROOT/'runner.py'), *map(str, argv)],
            cwd=ROOT, env=dict(os.environ, CODEX_THREAD_ID=actor, **env), capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout+proc.stderr)
            return json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0, proc.stdout+proc.stderr)
        return proc

    def projected(self):
        return approved.projection(self.old)['relations'][0]

    def mutate(self, path, change):
        value = runner.read(path); change(value); runner.save(path, value)

    def immutable(self):
        files = [self.qpath, interaction.directory()/'hub.json', self.native/'state_5.sqlite', *self.headers.values()]
        for d in (self.old, self.new):
            files.extend(p for p in d.rglob('*') if p.is_file() and p.suffix != '.lock')
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    def test_public_cli_registration_query_idempotence_retains_original_goal(self):
        before = self.immutable()
        args = ('approved-delivery', 'register', self.old, '--question', self.qid, '--delivery', self.new,
                '--evidence', self.evidence, '--source', 'Synthetic exact observation')
        first = self.cli(*args, actor=HUB)
        self.assertEqual(self.cli(*args), first)
        status = self.cli('status', self.old)
        self.assertEqual(status['status'], 'blocked')
        self.assertEqual(status['delivery']['effective_status'], 'blocked')
        self.assertCountEqual(status['remaining'], ['production', 'external-source', 'measurement'])
        relation = self.cli('approved-delivery', 'status', self.old)['relations'][0]
        self.assertEqual(relation['approved_scope_status'], 'delivered')
        self.assertEqual(relation['delivery_workflow'], str(self.new))
        self.assertTrue(relation['permission_expanded'])
        self.assertIn('authorization', relation['authority_delta'])
        self.assertFalse(relation['execution_authority']); self.assertFalse(relation['original_workflow_complete'])
        self.assertEqual(relation['remaining_deferred_criteria'], ['external-source', 'measurement'])
        self.assertEqual(before, self.immutable())
        self.assertFalse((self.old/'workflow-lineage.json').exists())
        self.assertFalse((self.old/'user-closure.json').exists())

    def test_read_question_and_inbox_expose_actual_delivery_without_changing_acceptance(self):
        self.register(); before = self.qpath.read_bytes()
        value = self.cli('read-question', self.qid, actor=HUB)
        self.assertEqual(value['state'], 'accepted')
        self.assertEqual(value['approved_delivery']['approved_scope_status'], 'delivered')
        self.assertIsNone(value['presentation'])
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            self.assertEqual(interaction.inbox()[0]['approved_delivery']['validation'], 'valid')
        self.assertEqual(self.qpath.read_bytes(), before)

    def test_historical_original_stop_stays_and_observation_does_not_allow_execution_actions(self):
        (self.old/'STOP').write_text('Synthetic original STOP')
        before = self.immutable(); self.register()
        q = self.cli('read-question', self.qid, actor=HUB)
        self.assertTrue(q['approved_delivery']['historical']['stop'])
        self.cli('read-question', self.qid, '--presented-source', 'Must not rewrite history', actor=HUB, ok=False)
        self.cli('answer', self.qid, '--answer', 'New permission', '--source', 'Not allowed', actor=HUB, ok=False)
        self.cli('accept-answer', self.qid, ok=False)
        self.cli('ask', self.old, '--question', 'Should remain stopped', ok=False)
        self.assertEqual(before, self.immutable())

    def test_controller_resolves_specific_question_keeps_true_deferred_obligation(self):
        self.register()
        for _ in range(2):
            self.cli('control', 'tick', actor=HUB)
        rows = self.cli('followups', '--all', actor=HUB)
        question = next(x for x in rows if x['id'] == self.qid)
        self.assertEqual(question['state'], 'resolved')
        self.assertEqual(question['resolution_basis'], 'approved_delivery')
        self.assertNotIn('presented_at', question)
        operational = next(x for x in rows if x['workflow'] == str(self.old) and x['kind'] == 'operational')
        self.assertEqual(operational['diagnostic_criteria'], ['external-source', 'measurement'])
        self.assertNotIn('production', operational['diagnostic_criteria'])
        self.assertNotIn(operational['state'], control.TERMINAL)
        self.assertEqual(runner.read(self.old/'state.json')['status'], 'blocked')

    def test_existing_continuation_still_tracks_full_original_goal_after_scoped_observation(self):
        continuation = workflow.create(self.repo, self.plan, ORIGIN)
        self.cli('control', 'tick', actor=HUB)
        note = self.root/'continuation-note.txt'; note.write_text('Synthetic continuation retains the complete original contract')
        self.cli('followup-link', self.qid, '--workflow', continuation, '--note', 'Synthetic full goal continuation',
                 '--evidence', note, actor=HUB)
        self.register(); self.cli('control', 'tick', actor=HUB)
        action = next(x for x in self.cli('followups', '--all') if x['id'] == self.qid)
        self.assertEqual(action['workflow'], str(continuation))
        self.assertEqual(action['state'], 'awaiting_execution')
        self.assertEqual(action['approved_delivery']['approved_scope_status'], 'delivered')
        self.assertNotEqual(action.get('resolution_basis'), 'approved_delivery')

    def test_delivered_scope_does_not_suppress_another_unaccepted_question(self):
        self.register()
        second = interaction.ask(self.old, 'Need synthetic external source credentials?', 'Wait for actual user')
        self.cli('control', 'tick', actor=HUB)
        rows = self.cli('followups', '--all', actor=HUB)
        q = next(x for x in rows if x['id'] == second['id'])
        self.assertNotIn(q['state'], control.TERMINAL)
        self.assertIsNone(approved.for_question(runner.read(interaction.question_path(second['id']))))

    def test_controller_original_stop_preserved_and_delivery_visible_on_existing_question(self):
        (self.old/'STOP').touch()
        self.cli('control', 'tick', actor=HUB)
        self.register(); self.cli('control', 'tick', actor=HUB)
        action = next(x for x in self.cli('followups', '--all') if x['id'] == self.qid)
        self.assertEqual(action['state'], 'stopped')
        self.assertEqual(action['approved_delivery']['approved_scope_status'], 'delivered')
        self.assertTrue((self.old/'STOP').exists())

    def test_no_blanket_lineage_for_changed_authority(self):
        data = dict(predecessor_workflow=str(self.old), successor_workflow=str(self.new),
            predecessor_contract_sha256=approved.sha(self.old/'contract.json'),
            successor_contract_sha256=approved.sha(self.new/'contract.json'), permission_expanded=False,
            origin_thread=ORIGIN, hub_thread=HUB, acceptance_mapping={}, source='Synthetic forged no-expansion')
        path = self.root/'false-lineage.json'; runner.save(path, data)
        with self.assertRaises(ValueError):
            workflow_lineage.link(self.old, self.new, path, 'Not compatible authority')
        self.register(); self.assertEqual(workflow_lineage.delivery(self.old, runner.read(self.old/'state.json'))['validation'], 'unlinked')

    def test_forged_false_permission_expanded_and_omitted_authority_delta_rejected(self):
        self.proof['permission_expanded'] = False; runner.save(self.evidence, self.proof)
        with self.assertRaises(ValueError): self.register()
        self.refresh_proof(); self.proof['authority_delta'] = {}; runner.save(self.evidence, self.proof)
        with self.assertRaises(ValueError): self.register()

    def test_prose_alone_no_accepted_source_no_permission(self):
        for field, value in [('state', 'answered'), ('answer_source', ''), ('accepted_at', None),
                             ('answer_delivery', {}), ('answer', ' ')]:
            with self.subTest(field=field):
                original = self.qpath.read_bytes()
                self.mutate(self.qpath, lambda q: q.update({field: value}))
                with self.assertRaises(ValueError): self.register()
                self.qpath.write_bytes(original)

    def test_wrong_question_origin_hub_workflow_contract_generation_rejected(self):
        changes = dict(id='question-'+'e'*20, origin_thread=HUB, hub_thread=FOREIGN,
                       workflow=str(self.new), contract_hash='0'*64, generation=[None, 999])
        for field, value in changes.items():
            with self.subTest(field=field):
                original = self.qpath.read_bytes()
                self.mutate(self.qpath, lambda q: q.update({field: value}))
                with self.assertRaises(ValueError): self.register()
                self.qpath.write_bytes(original)

    def test_missing_or_malformed_original_criteria_cannot_hide_deferred_work(self):
        self.register(); raw = (self.old/'state.json').read_bytes()
        for value in ({}, [], dict(production={'status': 'failed'}), dict(production=None)):
            with self.subTest(value=value):
                self.mutate(self.old/'state.json', lambda s: s.update(criteria=value))
                result = self.cli('approved-delivery', 'status', self.old)
                self.assertEqual(result['validation'], 'invalid')
                self.assertNotEqual(approved.diagnostic_scope(self.old, runner.read(self.old/'state.json'))['validation'], 'valid')
                (self.old/'state.json').write_bytes(raw)

    def test_malformed_timestamp_and_non_boolean_authority_claim_are_refused(self):
        raw = self.qpath.read_bytes()
        for value in ('accepted', True, float('inf'), -1):
            self.mutate(self.qpath, lambda q: q.update(accepted_at=value))
            with self.assertRaises(ValueError): self.refresh_proof()
            self.qpath.write_bytes(raw)
        self.refresh_proof(); self.proof['permission_expanded'] = 1
        runner.save(self.evidence, self.proof)
        with self.assertRaises(ValueError): self.register()

    def test_wrong_actor_cannot_inspect_or_register(self):
        for action in ('inspect', 'register'):
            self.cli('approved-delivery', action, self.old, '--question', self.qid, '--delivery', self.new,
                '--evidence', self.evidence, '--source', 'Foreign actor', actor=FOREIGN, ok=False)
        self.assertEqual(approved.projection(self.old)['validation'], 'unlinked')

    def test_unverified_environment_reference_is_not_an_actor_grant(self):
        self.cli('approved-delivery', 'register', self.old, '--question', self.qid, '--delivery', self.new,
            '--evidence', self.evidence, '--source', 'Spoofed locator', actor=FOREIGN,
            CTASK_SESSION_REF='agentref-'+'a'*32, ok=False)

    def test_partial_or_forged_delivery_completion_refused(self):
        for change in (lambda s: s.update(status='blocked'),
                       lambda s: s['criteria']['deploy'].update(status='failed'),
                       lambda s: s['criteria']['public'].update(evidence=[])):
            raw = (self.new/'state.json').read_bytes(); self.mutate(self.new/'state.json', change)
            with self.assertRaises(ValueError): self.register()
            (self.new/'state.json').write_bytes(raw)
        (self.new/'STOP').touch()
        with self.assertRaises(ValueError): self.register()

    def test_missing_and_changed_command_evidence_invalidates_observation(self):
        self.register()
        log = self.new/'evidence'/'verified.txt'
        log.write_text('Changed verification')
        self.assertEqual(self.projected()['validation'], 'invalid')
        log.unlink()
        self.assertEqual(self.projected()['validation'], 'invalid')
        with self.assertRaises((ValueError, OSError)): self.register()

    def test_manual_delivery_needs_actual_matching_ack_and_pins_underlying_evidence(self):
        self.new = self.create_delivery(manual=True); self.refresh_proof()
        self.register(); self.assertEqual(self.projected()['validation'], 'valid')
        (self.new/'evidence'/'verified.txt').write_text('Changed user evidence')
        self.assertEqual(self.projected()['validation'], 'invalid')

    def test_missing_manual_ack_cannot_be_replaced_by_completed_state(self):
        self.new = self.create_delivery(manual=True)
        (self.new/'acknowledgements'/'user-ack.json').unlink()
        with self.assertRaises((ValueError, OSError)): self.refresh_proof()

    def test_wrong_manual_contract_revision_criterion_and_note_rejected(self):
        self.new = self.create_delivery(manual=True)
        path = self.new/'acknowledgements'/'user-ack.json'
        raw = path.read_bytes()
        for field, value in dict(contract_hash='0'*64, execution_revision=17, criterion='wrong', note='').items():
            with self.subTest(field=field):
                self.mutate(path, lambda q: q.update({field: value}))
                with self.assertRaises(ValueError): self.refresh_proof()
                path.write_bytes(raw)

    def test_tampered_manifest_and_registered_evidence_fail_closed(self):
        self.register(); self.evidence.write_text('{}')
        result = self.cli('approved-delivery', 'status', self.old)
        self.assertEqual(result['validation'], 'invalid')
        self.assertEqual(result['relations'][0]['approved_scope_status'], 'unverified')
        scope = approved.diagnostic_scope(self.old, runner.read(self.old/'state.json'))
        self.assertIn('production', scope['diagnostic_criteria'])

    def test_tampered_ledger_fails_closed(self):
        self.register(); path, _ = approved._ledger_path()
        self.mutate(path, lambda d: d['records'][0]['proof'].update(permission_expanded=False))
        self.assertEqual(approved.projection(self.old)['validation'], 'invalid')

    def test_changed_question_history_contract_and_delivery_state_invalidate(self):
        self.register()
        targets = [(self.qpath, lambda q: q.update(answer_source='Different source')),
                   (self.new/'state.json', lambda q: q.update(elapsed_seconds=42)),
                   (self.old/'contract.json', lambda q: q.update(objective='Rewritten original goal'))]
        for path, change in targets:
            with self.subTest(path=path):
                raw = path.read_bytes(); self.mutate(path, change)
                self.assertEqual(self.projected()['validation'], 'invalid')
                path.write_bytes(raw)

    def test_original_progress_preserves_observation_but_new_execution_generation_is_stale(self):
        self.register()
        self.mutate(self.old/'state.json', lambda q: q['criteria']['measurement'].update(status='passed'))
        self.assertEqual(self.projected()['validation'], 'valid')
        self.assertEqual(self.projected()['remaining_deferred_criteria'], ['external-source'])
        self.mutate(self.old/'state.json', lambda q: q.update(execution_revision=1))
        self.assertEqual(self.projected()['validation'], 'invalid')

    def test_native_header_home_and_current_hub_identity_are_pinned(self):
        self.register()
        path = self.headers[HUB]; raw = path.read_bytes()
        self.mutate(path, lambda d: d['payload'].update(cwd=str(self.repo)))
        self.assertEqual(self.projected()['validation'], 'invalid'); path.write_bytes(raw)
        with patch.dict(os.environ, CODEX_HOME=str(self.root/'wrong-native-home')):
            # Native accounts have separate ATO namespaces. A different account
            # sees no relation and cannot suppress any original obligation.
            self.assertNotEqual(approved.projection(self.old)['validation'], 'valid')
            with self.assertRaises((OSError, ValueError)): self.register()
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            interaction.configure(FOREIGN, 'Changed default does not own old approval')
        self.assertEqual(self.projected()['validation'], 'invalid')

    def test_transcript_growth_does_not_invalidate_exact_header_proof(self):
        self.register()
        with self.headers[ORIGIN].open('a') as stream:
            stream.write(json.dumps(dict(type='event_msg', payload=dict(type='agent_message', message='Synthetic later progress')))+'\n')
        self.assertEqual(self.projected()['validation'], 'valid')

    def test_wrong_delivery_origin_and_repository_identity_rejected(self):
        self.mutate(self.new/'origin.json', lambda o: o.update(origin_thread_id=HUB))
        with self.assertRaises(ValueError): self.register()
        self.mutate(self.new/'origin.json', lambda o: o.update(origin_thread_id=ORIGIN, repo=str(self.root)))
        with self.assertRaises(ValueError): self.register()

    def test_symlink_manifest_log_and_workflow_alias_rejected(self):
        alias = self.root/'manifest-alias'; alias.symlink_to(self.evidence)
        with self.assertRaises(ValueError): approved.register(self.old, self.qid, self.new, alias, 'Alias')
        alias.unlink(); alias.symlink_to(self.old, target_is_directory=True)
        with self.assertRaises(ValueError): approved.inspect(alias, self.qid, self.new)
        log = self.new/'evidence'/'verified.txt'; log.unlink(); log.symlink_to(self.evidence)
        with self.assertRaises(ValueError): self.register()

    def test_explicit_partition_and_actual_completed_mapping_required(self):
        raw = copy.deepcopy(self.proof)
        for patch_ in (dict(approved_criteria={}), dict(deferred_criteria=[]),
                       dict(deferred_criteria=['external-source', 'measurement', 'production']),
                       dict(approved_criteria={'production': ['missing']}),
                       dict(approved_criteria={'production': ['deploy', 'deploy']})):
            runner.save(self.evidence, dict(raw, **patch_))
            with self.assertRaises(ValueError): self.register()

    def test_conflicting_delivery_assignment_and_missing_proof_rejected(self):
        self.register()
        other = self.create_delivery()
        proof = approved.inspect(self.old, self.qid, other)['expected_evidence']
        proof.update(approved_criteria=self.proof['approved_criteria'], deferred_criteria=self.proof['deferred_criteria'])
        path = self.root/'conflict.json'; runner.save(path, proof)
        with self.assertRaises(ValueError): approved.register(self.old, self.qid, other, path, 'Conflicting delivery')
        path.unlink()
        with self.assertRaises((OSError, ValueError)): approved.register(self.old, self.qid, other, path, 'Missing proof')

    def test_self_relation_rejected(self):
        with self.assertRaises(ValueError): approved.inspect(self.old, self.qid, self.old)

    def test_mixed_existing_lineage_cycle_rejected_at_public_register(self):
        runner.save(self.new/'workflow-lineage.json', dict(successor=str(self.old)))
        with self.assertRaisesRegex(ValueError, 'cycle'): self.register()

    def test_approved_relation_cycle_rejected_at_public_register(self):
        self.register()
        raw = (self.new/'state.json').read_bytes()
        self.mutate(self.new/'state.json', lambda s: s.update(status='blocked'))
        question = interaction.ask(self.new, 'Synthetic reverse relation approval?')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB), patch('codex_session_adapter.queue',
                side_effect=subprocess.TimeoutExpired('synthetic fixture transport fault', 0)):
            interaction.answer(question['id'], 'Synthetic reverse fixture approval', 'Synthetic cycle fixture')
        interaction.accept(question['id'])
        (self.new/'state.json').write_bytes(raw)
        log = self.old/'evidence'/'complete.txt'; log.write_text('Synthetic original independent completion')
        self.mutate(self.old/'state.json', lambda s: s.update(status='completed', criteria={
            key: dict(status='passed', evidence=[str(log)]) for key in s['criteria']}))
        proof = approved.inspect(self.new, question['id'], self.old)['expected_evidence']
        proof.update(approved_criteria={'deploy': ['production']}, deferred_criteria=['public'])
        path = self.root/'cycle-proof.json'; runner.save(path, proof)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            approved.register(self.new, question['id'], self.old, path, 'Synthetic cycle refusal')

    def test_new_lineage_cycle_invalidates_existing_projection(self):
        self.register()
        runner.save(self.new/'workflow-lineage.json', dict(successor=str(self.old)))
        self.assertEqual(approved.projection(self.old)['validation'], 'invalid')
        self.assertIn('production', approved.diagnostic_scope(self.old, runner.read(self.old/'state.json'))['diagnostic_criteria'])

    def test_existing_scope_cannot_be_reassigned_through_different_question(self):
        self.register()
        question = interaction.ask(self.old, 'Synthetic different approval for the same production?')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB), patch('codex_session_adapter.queue',
                side_effect=subprocess.TimeoutExpired('synthetic fixture transport fault', 0)):
            interaction.answer(question['id'], 'Synthetic second decision', 'Synthetic second source')
        interaction.accept(question['id'])
        other = self.create_delivery()
        proof = approved.inspect(self.old, question['id'], other)['expected_evidence']
        proof.update(approved_criteria=self.proof['approved_criteria'], deferred_criteria=self.proof['deferred_criteria'])
        path = self.root/'overlap.json'; runner.save(path, proof)
        with self.assertRaisesRegex(ValueError, 'scope already assigned'):
            approved.register(self.old, question['id'], other, path, 'Synthetic conflicting scope')
        proof = approved.inspect(self.old, question['id'], self.new)['expected_evidence']
        proof.update(approved_criteria={'external-source': ['public']}, deferred_criteria=['production', 'measurement'])
        runner.save(path, proof)
        with self.assertRaisesRegex(ValueError, 'already assigned'):
            approved.register(self.old, question['id'], self.new, path, 'Synthetic reused delivery')

    def test_fully_observed_scope_quiets_diagnostics_without_closing_original_and_reopens_on_tamper(self):
        # Establish an existing diagnostic first; the relation must not erase
        # its unknown-delivery barrier or imply original overall completion.
        self.cli('control', 'tick', actor=HUB)
        self.proof.update(approved_criteria={key: ['deploy', 'public'] for key in
            ('production', 'external-source', 'measurement')}, deferred_criteria=[])
        runner.save(self.evidence, self.proof); self.register()
        for _ in range(2): self.cli('control', 'tick', actor=HUB)
        actions = [x for x in self.cli('followups', '--all') if x['workflow'] == str(self.old)
                   and x['kind'] in ('workflow', 'operational')]
        self.assertTrue(actions)
        self.assertTrue(all(x['state'] in control.TERMINAL for x in actions))
        self.assertEqual(runner.status(self.old)['status'], 'blocked')
        self.assertEqual(len(runner.status(self.old)['remaining']), 3)
        self.evidence.write_text('{}'); self.cli('control', 'tick', actor=HUB)
        actions = [x for x in self.cli('followups', '--all') if x['workflow'] == str(self.old)
                   and x['kind'] == 'operational']
        self.assertTrue(any(x['state'] not in control.TERMINAL for x in actions))

    def test_failure_attention_retains_residual_failure_but_retires_delivered_scope_only(self):
        import failure_attention
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            failure_attention.configure('Synthetic fixture failure preference')
        state = runner.read(self.old/'state.json')
        action = dict(id='failure-synthetic-approved-fixture', kind='failure_attention', workflow=str(self.old),
            origin=ORIGIN, hub=HUB, state='unpresented', created_at=runner.now(), reminders=0,
            baseline_generation=recovery.generation(self.old, state),
            failure_evidence={str(self.old/'state.json'): approved.sha(self.old/'state.json')})
        self.register()
        with patch('recovery.handler_status', return_value='unhandled'):
            self.assertTrue(failure_attention.current(action), 'real deferred gaps still require attention')
        # A separate fixture run observes all explicitly mapped scope. Never
        # change a registered observation or the original raw failed criteria.
        self.assertFalse(self.projected()['original_workflow_complete'])

    def test_failure_attention_is_not_reissued_for_scope_already_delivered(self):
        import failure_attention
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            failure_attention.configure('Synthetic fixture failure preference')
        self.proof.update(approved_criteria={key: ['deploy', 'public'] for key in
            ('production', 'external-source', 'measurement')}, deferred_criteria=[])
        runner.save(self.evidence, self.proof); self.register()
        state = runner.read(self.old/'state.json')
        action = dict(id='failure-synthetic-approved-fixture', kind='failure_attention', workflow=str(self.old),
            origin=ORIGIN, hub=HUB, state='unpresented', created_at=runner.now(), reminders=0,
            baseline_generation=recovery.generation(self.old, state),
            failure_evidence={str(self.old/'state.json'): approved.sha(self.old/'state.json')})
        with control.database() as con, con:
            control.put(con, action)
        self.cli('control', 'tick', actor=HUB)
        actual = next(x for x in self.cli('followups', '--all') if x['id'] == action['id'])
        self.assertEqual(actual['state'], 'stale')
        self.assertNotIn('presented_at', actual)

    def test_active_or_unknown_delivery_process_cannot_be_claimed_complete(self):
        raw = (self.new/'state.json').read_bytes()
        for change in (dict(owner_pid=os.getpid()), dict(active_process=dict(pid=0))):
            self.mutate(self.new/'state.json', lambda s: s.update(change))
            with self.assertRaisesRegex(ValueError, 'active or unverified'): self.refresh_proof()
            (self.new/'state.json').write_bytes(raw)

    def test_repo_override_environment_cannot_redirect_git_identity_proof(self):
        with patch.dict(os.environ, GIT_DIR='/synthetic-missing-git', GIT_WORK_TREE='/synthetic-wrong-tree'):
            self.register()
            self.assertEqual(self.projected()['validation'], 'valid')

    def test_neutral_cli_requires_scoped_actor_and_preserves_human_origin_role_separation(self):
        import local_session_adapter as local
        origin_key, hub_key, user_key = [self.root/name for name in ('n-origin.json', 'n-hub.json', 'n-user.json')]
        origin = local.create(self.repo, 'origin', 'Synthetic neutral origin', origin_key)['reference']
        hub = local.create(self.repo, 'hub', 'Synthetic neutral hub', hub_key, user_key)['reference']
        interaction.configure_session(hub, 'Synthetic neutral fixture binding', actor_proof=local.load_actor_file(hub_key)[0])
        # This fixture does not run or reconfigure a controller or worker.
        with patch('control.register'):
            old = workflow.create(self.repo, self.plan, origin_ref=origin)
        grants = {}
        for role, key in [('origin', origin_key), ('hub', hub_key), ('user', user_key)]:
            path = self.root/('n-'+role+'-grant.json')
            local.grant(key, old, path, 'Synthetic exact neutral workflow grant')
            grants[role] = path
        origin_proof = local.load_actor_file(grants['origin'])[0]
        q = interaction.ask(old, 'Synthetic neutral production approval?', actor_proof=origin_proof)
        interaction.answer(q['id'], 'Synthetic neutral separate production approval', 'Synthetic neutral user:1',
                           actor_proof=local.load_actor_file(grants['user'])[0])
        interaction.accept(q['id'], actor_proof=origin_proof)
        data = runner.read(self.plan); data['authorization'] = 'Synthetic separately accepted neutral production'
        plan = self.root/'neutral-delivery-plan.json'; runner.save(plan, data)
        with patch('control.register'):
            new = workflow.create(self.repo, plan, origin_ref=origin)
        log = new/'evidence'/'passed.txt'; log.write_text('Synthetic neutral check passed')
        self.mutate(new/'state.json', lambda s: s.update(status='completed', criteria={
            key: dict(status='passed', evidence=[str(log)]) for key in s['criteria']}))
        args = ('approved-delivery', 'inspect', old, '--question', q['id'], '--delivery', new)
        self.cli(*args, ok=False)
        result = self.cli(*args, '--actor-file', grants['origin'])
        proof = result['expected_evidence']
        proof.update(approved_criteria={'production': ['production']}, deferred_criteria=['external-source', 'measurement'])
        evidence = self.root/'neutral-approved-proof.json'; runner.save(evidence, proof)
        args = ('approved-delivery', 'register', old, '--question', q['id'], '--delivery', new,
                '--evidence', evidence, '--source', 'Synthetic neutral observation')
        self.cli(*args, '--actor-file', grants['user'], ok=False)
        self.cli(*args, '--actor-file', origin_key, ok=False)
        self.cli(*args, '--actor-file', grants['hub'])
        query = self.cli('approved-delivery', 'status', old)
        self.assertEqual(query['relations'][0]['approved_scope_status'], 'delivered')
        self.assertFalse(query['execution_authority'])
        self.assertEqual(self.cli('read-question', q['id'], '--actor-file', grants['hub'])['state'], 'accepted')

    def test_invalidated_proof_reopens_only_previously_observed_question_disposition(self):
        self.register(); self.cli('control', 'tick', actor=HUB)
        self.evidence.write_text('{}'); self.cli('control', 'tick', actor=HUB)
        q = next(x for x in self.cli('followups', '--all') if x['id'] == self.qid)
        self.assertEqual(q['state'], 'awaiting_execution')
        self.assertEqual(q['approved_delivery']['validation'], 'invalid')

    def test_unknown_answer_and_operational_dispatch_do_not_replay_or_fake_receipt(self):
        # An actual origin acceptance can coexist with an uncertain transport
        # outcome. Neither observation nor controller may retry that envelope.
        self.mutate(self.qpath, lambda q: q.update(answer_delivery=dict(state='delivery_unknown')))
        self.refresh_proof(); self.register()
        self.cli('control', 'tick', actor=HUB)
        with control.database() as con, con:
            row = next(x for x in control.rows(True) if x['kind'] == 'operational' and x['workflow'] == str(self.old))
            row['origin_delivery'] = dict(state='delivery_unknown')
            control.put(con, row)
        for _ in range(2): self.cli('control', 'tick', actor=HUB)
        row = next(x for x in control.rows(True) if x['id'] == row['id'])
        self.assertEqual(row['origin_delivery']['state'], 'delivery_unknown')
        self.assertNotIn('presented_at', row)
        self.assertEqual(runner.read(self.qpath)['answer_delivery']['state'], 'delivery_unknown')


if __name__ == '__main__':
    unittest.main()
