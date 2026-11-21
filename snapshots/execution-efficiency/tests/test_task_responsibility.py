"""Workflow-scoped obligations using bounded, real read-only controller fixtures.

No provider/native CLI is invoked. Native identity fixtures contain only synthetic
metadata. Controller transport is captured; subprocess CLI reads the actual DB.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_interaction as fixtures
from test_interaction import ORIGIN, HUB
import agent_registry
import control
import failure_attention as attention
import interaction
import notifications
import recovery
import runner
import task_responsibility as responsibility
import workflow

ROOT = Path(__file__).resolve().parents[1]


class ResponsibilityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.InteractionTests('runTest'); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.repo, self.home = self.f.repo.resolve(), self.f.home.resolve()
        self.env_patch = patch.dict(os.environ, ATO_HOME=str(self.repo/'ato-control'))
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        interaction.configure(HUB, 'synthetic unified hub')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.enable('fixture authorized controller', 60, 0)
            notifications.configure('user_input_only', 'ordinary status silent')
            attention.configure('one unattended failure notice')
        plan = runner.read(self.repo/'input.json'); plan['responsibility_policy'] = dict(version=1)
        runner.save(self.repo/'responsibility-input.json', plan)
        self.d = workflow.create(self.repo, self.repo/'responsibility-input.json', ORIGIN)
        self.log = self.repo/'failed-check.log'; self.log.write_text('independent check failed\n')
        w = workflow.Workflow(self.d)
        w.stage(dict(objective='Exact bounded fixture stage', memory=False, tasks=[dict(id='file',
            prompt='synthetic fixture only', checks=[[sys.executable, '-c', 'pass']])]), ['file'])
        self.child = Path(w.state['active_run'])
        child = runner.read(self.child/'state.json'); child.update(status='blocked', owner_pid=None, active_process=None)
        child['tasks']['file'].update(status='failed', attempts=2, attempt_sequence=2, evidence=[str(self.log)])
        runner.save(self.child/'state.json', child)
        self.state = runner.read(self.d/'state.json')
        self.state.update(status='blocked', error='independent file check failed', active_run=str(self.child),
                          criteria=dict(file=dict(status='failed', evidence=[str(self.log)])))
        self.persist()
        self.proof = self.repo/'diagnosis.txt'; self.proof.write_text('bounded synthetic diagnosis\n')
        self.env = dict(os.environ)

    def persist(self):
        runner.save(self.d/'state.json', self.state)

    def tick(self, activity='idle'):
        with patch('session_router.activity', return_value=activity), patch('control.send', return_value=dict(state='queued')) as send:
            control.tick()
        return send

    def records(self, kind):
        return [r for r in control.rows(True) if r.get('kind') == kind and r['workflow'] == str(self.d)]

    def action(self):
        return dict(kind='failure_attention', workflow=str(self.d), origin=ORIGIN, hub=HUB,
            baseline_generation=recovery.generation(self.d, self.state),
            failure_episode=attention.failure_episode(self.d, self.state),
            failure_evidence={str(p):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.d/'state.json', self.d/'contract.json', self.d/'origin.json', self.child/'state.json')})

    def claim(self, **kwargs):
        return responsibility.claim(self.d, 'origin', 'inspect frozen failure evidence', [str(self.proof)],
                                    'synthetic verified origin', **kwargs)

    def dispose(self, outcome='external_wait', **kwargs):
        return responsibility.dispose(self.d, 'origin', outcome, 'recheck exact recorded external condition',
            [str(self.proof)], 'synthetic actual diagnosis', **kwargs)

    def test_active_origin_is_advisory_not_workflow_handler(self):
        with patch('session_router.activity', return_value='active'):
            self.assertTrue(attention.current(self.action()))
            self.assertEqual(recovery.handler_status(self.d, self.state, ORIGIN), 'unhandled')
        self.tick('active')
        self.assertEqual(len(self.records('failure_attention')), 1)
        self.assertEqual(self.records('responsibility')[0]['state'], 'pending_disposition')

    def test_unknown_activity_cannot_hide_or_grant_execution(self):
        self.tick('unknown'); self.tick('unknown')
        self.assertEqual(len(self.records('failure_attention')), 1)
        self.assertFalse(self.records('responsibility')[0]['execution_grant'])
        self.assertFalse((self.d/'recovery.json').exists())

    def test_episode_ignores_metadata_heartbeat_elapsed_and_generation(self):
        action = self.action(); first = action['failure_episode']
        self.state.update(elapsed_seconds=999, updated_at=1000, heartbeat=888, execution_revision=7)
        self.persist()
        child = runner.read(self.child/'state.json'); child.update(heartbeat=123, elapsed_seconds=500)
        runner.save(self.child/'state.json', child)
        self.assertEqual(first, attention.failure_episode(self.d, self.state))
        self.assertTrue(attention.current(action))

    def test_real_attempt_and_check_bytes_change_episode(self):
        first = attention.failure_episode(self.d, self.state)
        child = runner.read(self.child/'state.json'); child['tasks']['file']['attempt_sequence'] += 1
        runner.save(self.child/'state.json', child)
        second = attention.failure_episode(self.d, self.state); self.assertNotEqual(first, second)
        self.log.write_text('new substantive independent result')
        self.assertNotEqual(second, attention.failure_episode(self.d, self.state))

    def test_contract_and_origin_hash_drift_are_not_metadata(self):
        action = self.action()
        data = runner.read(self.d/'origin.json'); data['evidence'] += ' changed'
        runner.save(self.d/'origin.json', data)
        self.assertEqual(attention.evaluate(action)['reason_code'], 'immutable_failure_evidence_changed')
        self.assertFalse(attention.current(action))

    def test_legacy_unknown_semantics_do_not_bypass_checksum_mismatch(self):
        action = self.action(); action.pop('failure_episode')
        self.state['elapsed_seconds'] += 1; self.persist()
        result = attention.evaluate(action)
        self.assertFalse(result['current']); self.assertEqual(result['obligation'], 'unknown')

    def test_claim_exact_task_actor_and_finite_expiry(self):
        c = self.claim(seconds=60)
        self.assertEqual(c['expires_at']-c['created_at'], 60)
        self.assertEqual(c['project'], str(self.repo)); self.assertEqual(c['actor']['role'], 'origin')
        self.assertFalse(c['execution_grant'])
        self.assertEqual(responsibility.handler(self.d, self.state, ORIGIN)['status'], 'workflow_claimed')
        self.assertFalse(attention.current(self.action()))

    def test_expired_claim_cannot_renew_or_hide_obligation(self):
        c = self.claim(seconds=60)
        with patch('runner.now', return_value=c['expires_at']+1):
            again = self.claim(seconds=60); self.assertEqual(c, again)
            self.assertTrue(attention.current(self.action()))
            self.assertEqual(attention.evaluate(self.action())['reason_code'], 'claim_expired')
            with self.assertRaisesRegex(ValueError, 'no renewal'):
                self.claim(seconds=61)
            self.tick('active')
        self.assertEqual(self.records('responsibility')[0]['state'], 'pending_disposition')

    def test_wrong_role_actor_and_claim_limits_fail_closed(self):
        with patch.dict(os.environ, CODEX_THREAD_ID='not-bound'):
            with self.assertRaises(ValueError): self.claim()
        for seconds in (0, -1, 901, True):
            with self.assertRaises(ValueError): self.claim(seconds=seconds)
        with self.assertRaises(ValueError):
            responsibility.claim(self.d, 'worker', 'act', [str(self.proof)], 'fixture')

    def test_hub_claim_is_authenticated_and_separate_from_origin(self):
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            c = responsibility.claim(self.d, 'hub', 'inspect task diagnosis', [str(self.proof)], 'fixture hub')
        self.assertEqual(c['actor']['reference'], HUB)
        with self.assertRaises(ValueError):
            responsibility.claim(self.d, 'hub', 'inspect task diagnosis', [str(self.proof)], 'fixture hub')

    def test_claim_other_project_contract_generation_and_evidence_are_invalid(self):
        c = self.claim()
        for field, value in [('project', '/unrelated'), ('workflow', '/unrelated'), ('contract_hash', 'wrong'), ('generation', [None, 55])]:
            row = self.records('responsibility')[0]; row['claim'][field] = value
            self.assertEqual(responsibility.claim_reason(row, self.d, self.state, ORIGIN, HUB), 'claim_binding_changed')
        self.proof.write_text('changed evidence')
        self.assertEqual(responsibility.handler(self.d, self.state, ORIGIN)['reason_code'], 'claim_evidence_changed')

    def test_unrelated_execution_owner_is_not_a_workflow_handler(self):
        owner = dict(project=str(self.repo), task_id='/another-workflow', contract_hash=self.state['contract_hash'],
                     generation=recovery.generation(self.d, self.state), role='origin', owner='request', fence=1)
        with patch('recovery.current_execution_owner', return_value=owner):
            self.assertTrue(attention.current(self.action()))
        owner['task_id'] = str(self.d)
        with patch('recovery.current_execution_owner', return_value=owner):
            self.assertEqual(responsibility.handler(self.d, self.state, ORIGIN)['status'], 'execution_owned')

    def test_unknown_process_is_cleanup_fenced_and_not_verified_handling(self):
        self.state['active_process'] = dict(pid=os.getpid(), argv=['synthetic'])
        self.persist()
        h = responsibility.handler(self.d, self.state, ORIGIN)
        self.assertEqual(h['status'], 'unhandled'); self.assertTrue(h['cleanup_fenced'])
        self.assertTrue(attention.current(self.action()))
        sent = self.tick('active')
        self.assertFalse(any(c.args[0].startswith('operational:') for c in sent.call_args_list))

    def test_diagnostic_reply_requires_disposition_even_after_deadline(self):
        self.tick('active')
        op = self.records('operational')[0]
        with patch('session_router.observe_reply', return_value=dict(turn='late', text='diagnosed')):
            with patch('runner.now', return_value=op['disposition_due_at']+1): self.tick('unknown')
        self.assertEqual(self.records('operational')[0]['reason_code'], 'diagnostic_disposition_overdue')
        r = self.records('responsibility')[0]
        self.assertEqual(r['state'], 'pending_disposition')
        self.assertEqual(r['reason_code'], 'diagnostic_reply_requires_disposition')

    def test_external_wait_has_condition_finite_trigger_and_no_recovery(self):
        self.tick()
        row = self.records('responsibility')[0]
        due = row['created_at']+120
        self.dispose(condition='external fixture gate becomes available', recheck_at=due)
        self.tick(); self.assertEqual(self.records('responsibility')[0]['state'], 'external_wait')
        with patch('runner.now', return_value=due+1): self.tick()
        r = self.records('responsibility')[0]
        self.assertEqual(r['state'], 'pending_disposition'); self.assertEqual(r['reason_code'], 'external_wait_due')
        self.assertFalse((self.d/'recovery.json').exists())
        with self.assertRaisesRegex(ValueError, 'renewed'):
            self.dispose(condition='external fixture gate becomes available', recheck_at=due+60)

    def test_invalid_wait_and_unverified_actions_do_not_resolve_work(self):
        for due in (None, float('nan'), float('inf'), runner.now()-1, runner.now()+90000):
            with self.assertRaises(ValueError): self.dispose(condition='specific gate', recheck_at=due)
        with self.assertRaises(ValueError): self.dispose(outcome='authorized_action')
        with self.assertRaises(ValueError): self.dispose(outcome='closed')
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_canonical_question_is_once_only_and_never_automatic_answer(self):
        self.tick('active'); op = self.records('operational')[0]
        with patch('interaction.subprocess.run', side_effect=self.f.transport):
            q = control.operational_disposition(op['id'], 'decision', 'Choose reviewed option?',
                'Compared frozen options', [str(self.proof)], 'Choose A', 'actual synthetic diagnosis')
        self.dispose(outcome='human_question', question_id=q['id'])
        self.tick()
        self.assertEqual(self.records('responsibility')[0]['state'], 'awaiting_user')
        canonical = runner.read(interaction.question_path(q['id']))
        self.assertEqual(canonical['state'], 'pending'); self.assertNotIn('answer', canonical)
        with self.assertRaises(ValueError): self.dispose(outcome='human_question', question_id='question-missing')

    def test_accepted_question_still_requires_actual_followthrough(self):
        with patch('interaction.subprocess.run', side_effect=self.f.transport):
            q = interaction.ask(self.d, 'Which existing option?', 'A')
            self.dispose(outcome='human_question', question_id=q['id'])
            with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
                interaction.answer(q['id'], 'A', 'synthetic actual user answer')
            interaction.accept(q['id'])
        self.tick()
        row = self.records('responsibility')[0]
        self.assertEqual(row['state'], 'pending_disposition')
        self.assertEqual(row['reason_code'], 'answer_requires_followthrough')
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_metadata_drift_after_presented_or_unknown_notice_does_not_resend(self):
        self.tick()
        original = self.records('failure_attention')[0]
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.record(original['id'], state='presented', note='Actual synthetic user-facing report')
        self.state['execution_revision'] = 7; self.state['elapsed_seconds'] += 2; self.persist()
        sent = self.tick('active')
        self.assertFalse(any(c.args[0].startswith('followup:failure-') for c in sent.call_args_list))
        self.assertEqual(len(self.records('failure_attention')), 1)
        self.assertEqual(self.records('failure_attention')[0]['id'], original['id'])

    def test_claim_suppresses_notice_temporarily_without_staling_obligation(self):
        self.tick(); self.claim(seconds=60); self.tick()
        self.assertEqual(self.records('failure_attention')[0]['state'], 'stale')
        self.assertEqual(self.records('failure_attention')[0]['stale_reason_code'], 'workflow_claim_valid')
        self.assertEqual(self.records('responsibility')[0]['state'], 'pending_disposition')
        c = self.records('responsibility')[0]['claim']
        with patch('runner.now', return_value=c['expires_at']+1): sent = self.tick('active')
        self.assertFalse(any(c.args[0].startswith('followup:failure-') for c in sent.call_args_list))
        self.assertEqual(len(self.records('failure_attention')), 1)

    def test_new_actual_failed_attempt_creates_new_obligation_and_once_notice(self):
        self.tick()
        child = runner.read(self.child/'state.json'); child['tasks']['file']['attempt_sequence'] += 1
        runner.save(self.child/'state.json', child)
        self.tick('active'); self.tick('unknown')
        self.assertEqual(len(self.records('failure_attention')), 2)
        r = self.records('responsibility')
        self.assertEqual(len(r), 2)
        self.assertEqual(sorted(v['state'] for v in r), ['pending_disposition', 'superseded'])

    def test_stop_and_unknown_delivery_keep_fences_and_counters(self):
        before = (self.d/'state.json').read_bytes()
        ledger = dict(enabled=False, requests=[dict(state='delivery_unknown')])
        runner.save(self.d/'recovery.json', ledger)
        sent = self.tick('active')
        self.assertEqual(attention.evaluate(self.action())['reason_code'], 'claimed_recovery_delivery_no_replay')
        self.assertFalse(sent.called)
        self.assertEqual(before, (self.d/'state.json').read_bytes())
        self.assertEqual(runner.read(self.d/'recovery.json'), ledger)
        (self.d/'STOP').touch()
        with self.assertRaises(ValueError): self.claim()
        self.assertEqual(attention.evaluate(self.action())['reason_code'], 'stop_requested')

    def test_readonly_cli_query_exposes_followups_when_inbox_empty(self):
        self.tick('active')
        files = [self.d/'state.json', self.d/'contract.json', self.d/'origin.json',
                 control.directory()/'state.sqlite']
        before = {str(p):p.read_bytes() for p in files}
        result = subprocess.run([sys.executable, str(ROOT/'runner.py'), 'responsibility-status', str(self.d)],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        view = json.loads(result.stdout)
        self.assertTrue(view['read_only']); self.assertTrue(view['obligations']); self.assertTrue(view['followups'])
        self.assertFalse((interaction.directory()/'questions').exists())
        self.assertEqual(before, {str(p):p.read_bytes() for p in files})

    def test_cli_verified_role_claim_and_rejected_spoof(self):
        argv = [sys.executable, str(ROOT/'runner.py'), 'responsibility-claim', str(self.d), '--role', 'origin',
                '--next-action', 'inspect bounded evidence', '--evidence', str(self.proof), '--source', 'fixture CLI']
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        c = json.loads(result.stdout); self.assertFalse(c['execution_grant'])
        env = dict(self.env, CODEX_THREAD_ID=HUB)
        bad = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(len(self.records('responsibility')), 1)

    def test_readonly_query_before_controller_state_does_not_create_database(self):
        # Use a fresh ATO namespace, with only the task's recorded state.
        with patch.dict(os.environ, ATO_HOME=str(self.repo/'fresh-query-root')):
            path = responsibility.ato_config.interaction_directory()
            view = responsibility.view(self.d)
            self.assertEqual(view['followups'], [])
            self.assertFalse(path.exists())

    def test_legacy_creation_and_policy_validation_remain_explicit(self):
        self.assertNotIn('responsibility_policy', runner.read(self.f.d/'contract.json'))
        for policy in (None, dict(version=2), dict(version=True), dict(version=1, renew=True)):
            plan = runner.read(self.repo/'responsibility-input.json'); plan['responsibility_policy'] = policy
            with self.assertRaises(ValueError): workflow.validate(plan)

    def test_no_new_role_grants_for_worker_user_or_session_host(self):
        for role in (agent_registry.ActorRole.WORKER, agent_registry.ActorRole.USER, agent_registry.ActorRole.SESSION_HOST):
            self.assertNotIn('responsibility.claim', agent_registry._ACTOR_ACTIONS[role])
            self.assertNotIn('responsibility.disposition', agent_registry._ACTOR_ACTIONS[role])
        self.assertFalse(notifications.disposition(dict(kind='responsibility', version=1))['notify'])

    def test_successor_and_closure_require_verified_delivery_not_reply(self):
        self.tick()
        for validation, effective, expected in [('invalid', 'blocked', 'pending_disposition'),
                                                 ('valid', 'completed', 'completed'),
                                                 ('user_accepted', 'completed', 'completed')]:
            row = self.records('responsibility')[0]
            with patch('workflow_lineage.delivery', return_value=dict(validation=validation, effective_status=effective)):
                responsibility.refresh(row, self.d, self.state, ORIGIN)
            self.assertEqual(row['state'], expected)

    def test_late_local_pass_does_not_upgrade_independent_overall_criteria(self):
        self.tick()
        child = runner.read(self.child/'state.json'); child['tasks']['file']['summary'] = 'local self-check passes'
        child['local_checks'] = dict(passed=True)
        runner.save(self.child/'state.json', child)
        self.tick('active')
        self.assertEqual(self.records('responsibility')[0]['state'], 'pending_disposition')
        self.assertEqual(runner.read(self.d/'state.json')['criteria']['file']['status'], 'failed')

    def test_cli_disposition_writes_only_diagnostic_ledger(self):
        self.tick(); before = {n:(self.d/n).read_bytes() for n in ('state.json', 'contract.json', 'origin.json')}
        argv = [sys.executable, str(ROOT/'runner.py'), 'responsibility-disposition', str(self.d), '--role', 'origin',
                '--outcome', 'external_wait', '--next-action', 'inspect exact fixture gate', '--evidence', str(self.proof),
                '--source', 'fixture diagnosis', '--condition', 'fixture gate available', '--recheck-at', str(runner.now()+60)]
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {n:(self.d/n).read_bytes() for n in before})
        self.assertFalse((self.d/'recovery.json').exists())

    def test_parallel_cli_claims_are_atomic_and_keep_original_expiry(self):
        argv = [sys.executable, str(ROOT/'runner.py'), 'responsibility-claim', str(self.d), '--role', 'origin',
                '--next-action', 'same bounded diagnosis', '--evidence', str(self.proof), '--source', 'fixture CLI']
        one = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        two = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: one.kill() if one.poll() is None else None)
        self.addCleanup(lambda: two.kill() if two.poll() is None else None)
        a, ae = one.communicate(timeout=20); b, be = two.communicate(timeout=20)
        self.assertEqual(one.returncode, 0, ae); self.assertEqual(two.returncode, 0, be)
        self.assertEqual(json.loads(a), json.loads(b))
        self.assertEqual(len(self.records('responsibility')), 1)

    @patch('runner.process_identity', return_value='fixture-verified-kernel-start')
    def test_verified_progress_requires_two_live_probes_not_heartbeat(self, identity):
        self.tick()
        self.state.update(status='running', execution_revision=1, owner_pid=os.getpid(),
            owner_identity=runner.process_identity(os.getpid()))
        self.persist()
        child = runner.read(self.child/'state.json')
        child.update(status='running', active_process=dict(pid=os.getpid(),
            identity=runner.process_identity(os.getpid()), phase='host_check', evidence=str(self.log), argv=['fixture-check']))
        runner.save(self.child/'state.json', child)
        row = self.records('responsibility')[0]
        responsibility.refresh(row, self.d, self.state, ORIGIN)
        self.assertEqual(row['state'], 'pending_disposition')
        self.state['heartbeat'] = 999
        responsibility.refresh(row, self.d, self.state, ORIGIN)
        self.assertEqual(row['state'], 'pending_disposition')
        self.log.write_text(self.log.read_text()+'real subsequent output\n')
        responsibility.refresh(row, self.d, self.state, ORIGIN)
        self.assertEqual(row['state'], 'resolved')
        self.assertEqual(row['reason_code'], 'verified_subsequent_execution_progress')

    @patch('runner.process_identity', return_value='fixture-verified-kernel-start')
    def test_authorized_action_disposition_records_actual_execution_not_new_grant(self, identity):
        self.tick()
        self.state.update(status='running', execution_revision=1, owner_pid=os.getpid(),
            owner_identity=runner.process_identity(os.getpid()))
        self.persist()
        child = runner.read(self.child/'state.json')
        child.update(status='running', active_process=dict(pid=os.getpid(),
            identity=runner.process_identity(os.getpid()), phase='host_check', evidence=str(self.log), argv=['fixture-check']))
        runner.save(self.child/'state.json', child)
        value = self.dispose(outcome='authorized_action')
        self.assertEqual(value['execution_evidence']['pid'], os.getpid())
        self.assertFalse(value['execution_grant'])
        self.assertFalse((self.d/'recovery.json').exists())

    def test_existing_canonical_question_owns_single_notification_route(self):
        with patch('interaction.subprocess.run', side_effect=self.f.transport):
            q = interaction.ask(self.d, 'Real existing decision?', 'A')
        self.assertFalse(attention.current(self.action()))
        self.assertEqual(attention.evaluate(self.action())['reason_code'], 'canonical_question_pending')
        self.tick('active')
        self.assertFalse(self.records('failure_attention'))
        self.assertEqual(self.records('responsibility')[0]['state'], 'pending_disposition')

    def test_unknown_notice_and_diagnostic_delivery_are_never_replayed(self):
        with patch('session_router.activity', return_value='active'), patch('control.send', return_value=dict(state='delivery_unknown')) as send:
            control.tick(); control.tick()
        calls = [c.args[0] for c in send.call_args_list]
        self.assertEqual(sum(k.startswith('operational:') for k in calls), 1)
        self.assertEqual(sum(k.startswith('followup:failure-') for k in calls), 1)
        self.assertEqual(self.records('responsibility')[0]['state'], 'pending_disposition')

    def test_v2_scoped_proof_is_required_and_role_generation_are_verified(self):
        import agents
        import local_session_adapter as local
        origin_key = self.repo/'neutral-origin-root.json'
        hub_key = self.repo/'neutral-hub-root.json'
        user_key = self.repo/'neutral-user-root.json'
        origin = local.create(self.repo, 'origin', 'synthetic neutral fixture', origin_key)['reference']
        hub = local.create(self.repo, 'hub', 'synthetic neutral fixture', hub_key, user_key)['reference']
        proof, _, _ = local.load_actor_file(hub_key)
        interaction.configure_session(hub, 'synthetic unified neutral hub', actor_proof=proof)
        d = workflow.create(self.repo, self.repo/'responsibility-input.json', origin_ref=origin)
        state = runner.read(d/'state.json'); state['status'] = 'blocked'; runner.save(d/'state.json', state)
        grant = self.repo/'neutral-origin-scoped.json'
        local.grant(origin_key, d, grant, 'synthetic exact task proof')
        proof, reference, _ = local.load_actor_file(grant)
        kwargs = dict(directory=d, role='origin', next_action='diagnose exact task', paths=[str(self.proof)], source='synthetic v2')
        with patch.dict(os.environ, CTASK_SESSION_REF=origin):
            with self.assertRaises(ValueError): responsibility.claim(**kwargs)
        argv = [sys.executable, str(ROOT/'runner.py'), 'responsibility-claim', str(d), '--role', 'origin',
                '--next-action', kwargs['next_action'], '--evidence', str(self.proof), '--source', kwargs['source'],
                '--actor-file', str(grant)]
        actual = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(json.loads(actual.stdout)['actor']['role'], 'origin')
        value = responsibility.claim(**kwargs, actor_proof=proof, reference=reference)
        self.assertEqual(value['actor']['verified']['role'], 'origin')
        with self.assertRaises(ValueError):
            responsibility.claim(**dict(kwargs, role='hub'), actor_proof=proof, reference=reference)
        state['execution_revision'] = 1; runner.save(d/'state.json', state)
        with self.assertRaises(ValueError): responsibility.claim(**kwargs, actor_proof=proof, reference=reference)

    def test_task_summary_exposes_obligation_without_mutating_task_or_control(self):
        self.tick('active')
        before = {p:p.read_bytes() for p in [self.d/'state.json', self.d/'contract.json', control.directory()/'state.sqlite']}
        result = subprocess.run([sys.executable, str(ROOT/'runner.py'), 'task-summary', str(self.d), '--format', 'json'],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        view = json.loads(result.stdout)
        self.assertTrue(view['responsibility']['obligations'])
        self.assertEqual(before, {p:p.read_bytes() for p in before})

    def test_old_external_wait_cannot_handle_a_different_failed_attempt(self):
        self.tick()
        self.dispose(condition='exact external gate', recheck_at=runner.now()+60)
        self.tick(); self.assertFalse(attention.current(self.action()))
        child = runner.read(self.child/'state.json'); child['tasks']['file']['attempt_sequence'] += 1
        runner.save(self.child/'state.json', child)
        self.assertTrue(attention.current(self.action()))
        self.tick('active')
        self.assertEqual(len(self.records('responsibility')), 2)
        self.assertEqual(len(self.records('failure_attention')), 2)

    @patch('runner.process_identity', return_value='fixture-verified-kernel-start')
    def test_foreign_stage_identity_is_not_task_bound_host_work(self, identity):
        child = runner.read(self.child/'state.json')
        child.update(status='running', active_process=dict(pid=os.getpid(), identity='fixture-verified-kernel-start',
            phase='host_check', evidence=str(self.log), argv=['fixture']))
        runner.save(self.child/'state.json', child)
        self.assertEqual(responsibility.handler(self.d, self.state, ORIGIN)['status'], 'host_active')
        parent = runner.read(self.child/'parent.json'); parent['workflow_dir'] = '/different-workflow'
        runner.save(self.child/'parent.json', parent)
        h = responsibility.handler(self.d, self.state, ORIGIN)
        self.assertEqual(h['status'], 'unhandled'); self.assertTrue(h['cleanup_fenced'])
        self.assertTrue(attention.current(self.action()))

    def test_actual_execution_lease_json_generation_is_exact_not_unrelated(self):
        import transport
        bus = transport.managed_bus(); resource = recovery.execution_resource(self.state['repo'])
        generation = recovery.generation(self.d, self.state)
        with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
            token, fence = bus.acquire_execution(resource, project=self.state['repo'], task_id=str(self.d),
                contract_hash=self.state['contract_hash'], generation=generation,
                role='origin', owner='synthetic-existing-host-work', source='fixture verified repo lock')
        try:
            h = responsibility.handler(self.d, self.state, ORIGIN)
            self.assertEqual(h['status'], 'execution_owned'); self.assertEqual(h['role'], 'origin')
            self.assertFalse(attention.current(self.action()))
            other = dict(self.state, contract_hash='unrelated-contract')
            self.assertEqual(responsibility.handler(self.d, other, ORIGIN)['status'], 'unhandled')
        finally:
            bus.release_execution(resource, token, fence, generation=generation)
        self.assertEqual(responsibility.handler(self.d, self.state, ORIGIN)['status'], 'unhandled')

    def test_unavailable_execution_observation_is_not_handling_or_replay_authority(self):
        with patch('recovery.current_execution_owner', side_effect=OSError('synthetic owner observation unavailable')):
            h = responsibility.handler(self.d, self.state, ORIGIN)
            self.assertEqual(h['status'], 'unhandled'); self.assertTrue(h['cleanup_fenced'])
            self.assertEqual(h['reason_code'], 'execution_ownership_unknown')
            self.assertTrue(attention.current(self.action()))
            sent = self.tick('unknown')
        self.assertFalse(any(c.args[0].startswith('operational:') for c in sent.call_args_list))
        self.assertFalse((self.d/'recovery.json').exists())

    def test_unverified_process_output_does_not_resolve_episode_notice_or_obligation(self):
        self.tick()
        self.state.update(status='running', execution_revision=1, owner_pid=os.getpid())
        self.persist()
        child = runner.read(self.child/'state.json')
        child.update(status='running', active_process=dict(pid=os.getpid(), argv=['fixture'], evidence=str(self.log)))
        runner.save(self.child/'state.json', child)
        self.tick(); self.log.write_text('new output from unverified identity'); self.tick()
        self.assertNotEqual(self.records('failure_attention')[0]['state'], 'resolved')
        self.assertEqual(self.records('responsibility')[0]['state'], 'pending_disposition')

    def test_completed_label_with_failed_criteria_is_not_evidenced_closure(self):
        self.tick(); action = self.action()
        self.state['status'] = 'completed'; self.persist()
        row = self.records('responsibility')[0]
        responsibility.refresh(row, self.d, self.state, ORIGIN)
        self.assertEqual(row['state'], 'pending_disposition')
        self.assertEqual(attention.evaluate(action)['reason_code'], 'completion_evidence_requires_reconciliation')
        self.assertEqual(attention.evaluate(action)['obligation'], 'unknown')
        with self.assertRaises(ValueError): self.dispose(outcome='closed')


if __name__ == '__main__':
    unittest.main()
