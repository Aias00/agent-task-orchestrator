"""Real candidate CLI / private endpoint roundtrips; all user inputs synthetic.

No Codex services, native auth, real hub IDs, provider, daemon or detached job.
The actual runner executes only bounded foreground temporary-repository work.
"""
import asyncio
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import agent_registry
import agents
import interaction
import local_session_adapter
import runner
import session_router
import transport

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT/'tests/fixtures/neutral_hub_cli.py'
FIXTURE_KIND = 'fixture_neutral_e2e'
from legacy_support import LEGACY_CLI


class NeutralHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-neutral-hub-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'
        self.repo.mkdir()
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.repo)], check=True)
        self.env = dict(HOME=str(self.root/'empty-home'), ATO_HOME=str(self.root/'ato'),
            CODEX_HOME=str(self.root/'codex-inaccessible'), PATH='/usr/bin:/bin',
            CTASK_CODEX_BIN='/codex-inaccessible', CTASK_QUEUE_BIN='/codex-inaccessible',
            PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
        context = patch.dict(os.environ, self.env, clear=True)
        context.start()
        self.addCleanup(context.stop)
        self.origin_key, self.hub_key, self.user_key = [self.root/name for name in
            ('origin-root.json', 'hub-root.json', 'human-root.json')]
        self.origin = self.json_cli('local-session-create', '--project', self.repo,
            '--role', 'origin', '--source', 'synthetic fixture origin instruction',
            '--credential-file', self.origin_key)['reference']
        self.hub = self.json_cli('local-session-create', '--project', self.repo,
            '--role', 'hub', '--source', 'synthetic fixture hub instruction',
            '--credential-file', self.hub_key, '--user-credential-file', self.user_key)['reference']
        self.json_cli('hub', '--session-ref', self.hub, '--source', 'synthetic fixture chose hub',
            '--actor-file', self.hub_key)
        self.plan = self.repo/'plan.json'
        runner.save(self.plan, dict(objective='Synthetic neutral human decision and checked file',
            scope='temporary fixture only', authorization='synthetic fixture, no real user authorization',
            memory=False, agent={'kind': FIXTURE_KIND},
            acceptance=[dict(id='result', description='independent artifact content check',
                checks=[[sys.executable, '-c',
                    "from pathlib import Path; assert Path('result').read_text() == 'fixture output'"]])],
            limits=dict(runtime_seconds=30, attempt_seconds=10, check_seconds=3,
                max_attempts=1, max_stages=2, max_planning_attempts=2, max_wait_seconds=1)))
        self.workflow = Path(self.cli('workflow-create', '--repo', self.repo, '--plan', self.plan,
            '--origin-ref', self.origin).stdout.strip())
        self.origin_grant = self.grant(self.origin_key, 'origin-grant.json')
        self.hub_grant = self.grant(self.hub_key, 'hub-grant.json')
        self.user_grant = self.grant(self.user_key, 'user-grant.json')

    def cli(self, *args, ok=True, extra_env=None, entrypoint=CLI):
        result = subprocess.run([sys.executable, '-B', str(entrypoint), *map(str, args)],
            env=dict(self.env, **(extra_env or {})), cwd=self.repo,
            capture_output=True, text=True, timeout=40)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def json_cli(self, *args, **kwargs):
        return json.loads(self.cli(*args, **kwargs).stdout)

    def grant(self, root, name):
        path = self.root/name
        self.json_cli('local-session-grant', '--credential-file', root, '--workflow', self.workflow,
            '--output', path, '--source', 'synthetic scoped fixture grant')
        return path

    def ask(self, question='Choose synthetic fixture A?'):
        return self.json_cli('ask', self.workflow, '--question', question, '--recommendation', 'A',
            '--context', 'all data and users are synthetic', '--actor-file', self.origin_grant)

    def answer(self, q, text='A'):
        return self.json_cli('respond', q['id'], '--answer', text,
            '--source', 'synthetic user input fixture, not real authorization', '--actor-file', self.user_grant)

    def accept(self, q):
        return self.json_cli('accept-answer', q['id'], '--actor-file', self.origin_grant)

    def question_record(self, q):
        return runner.read(interaction.question_path(q['id']))

    def test_real_cli_roundtrip_continues_workflow_with_independent_checks_no_codex(self):
        q = self.ask()
        listed = self.json_cli('inbox', '--actor-file', self.hub_grant)
        self.assertEqual([row['id'] for row in listed], [q['id']])
        shown = self.json_cli('read', q['id'], '--actor-file', self.hub_grant,
            '--presented-source', 'synthetic CLI stdout shown to fixture user')
        self.assertEqual(shown['question'], 'Choose synthetic fixture A?')
        self.assertTrue(shown['presentation'])
        self.assertEqual(self.answer(q)['state'], 'answered')
        accepted = self.accept(q)
        self.assertEqual(accepted['id'], q['id'])
        self.assertEqual(accepted['state'], 'accepted')
        before = runner.read(self.workflow/'state.json')
        self.assertEqual(before['status'], 'created')
        self.assertEqual(before['criteria']['result']['status'], 'pending')
        result = self.json_cli('continue-task', self.workflow, '--accepted-answer', q['id'],
            '--actor-file', self.origin_grant)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['criteria']['result']['status'], 'passed')
        self.assertTrue(result['criteria']['result']['evidence'])
        subprocess.run([sys.executable, '-c', self.plan_check()], cwd=self.repo, env=self.env,
            check=True, timeout=5)
        self.assertFalse((self.root/'codex-inaccessible').exists())
        self.assertEqual((self.repo/'result').read_text(), 'fixture output')

    @staticmethod
    def plan_check():
        return "from pathlib import Path; assert Path('result').read_text() == 'fixture output'"

    def test_cli_runs_without_fixture_bootstrap_for_production_endpoint_commands(self):
        result = subprocess.run([sys.executable, '-B', str(LEGACY_CLI), 'inbox',
            '--actor-file', str(self.hub_grant)], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_unwrapped_candidate_cli_full_roundtrip_and_independent_continuation(self):
        def cli(*args):
            return self.cli(*args, entrypoint=LEGACY_CLI)
        def data(*args):
            return json.loads(cli(*args).stdout)
        plan = runner.read(self.plan)
        plan.pop('agent')  # No synthetic adapter registration in the actual entry point.
        runner.save(self.plan, plan)
        directory = Path(cli('workflow-create', '--repo', self.repo, '--plan', self.plan,
            '--origin-ref', self.origin).stdout.strip())
        grants = {}
        for role, root in (('origin', self.origin_key), ('hub', self.hub_key), ('user', self.user_key)):
            grants[role] = self.root/('direct-'+role+'.json')
            data('local-session-grant', '--credential-file', root, '--workflow', directory,
                '--output', grants[role], '--source', 'synthetic actual CLI fixture source')
        q = data('ask', directory, '--question', 'Synthetic actual CLI: use A?',
            '--actor-file', grants['origin'])
        self.assertEqual(data('inbox', '--actor-file', grants['hub'])[0]['id'], q['id'])
        self.assertEqual(data('read', q['id'], '--actor-file', grants['hub'])['question'],
            'Synthetic actual CLI: use A?')
        data('answer', q['id'], '--answer', 'A', '--source', 'synthetic user input, no real authority',
            '--actor-file', grants['user'])
        self.assertEqual(data('accept-answer', q['id'], '--actor-file', grants['origin'])['state'], 'accepted')
        self.assertEqual(runner.read(directory/'state.json')['status'], 'created')
        # Simulated authorized host work creates a real artifact. The candidate
        # runner must independently check it, without needing any model/planner.
        subprocess.run([sys.executable, '-c',
            "from pathlib import Path; Path('result').write_text('fixture output')"],
            cwd=self.repo, env=self.env, check=True, timeout=5)
        result = data('continue-task', directory, '--accepted-answer', q['id'], '--actor-file', grants['origin'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['criteria']['result']['status'], 'passed')
        self.assertEqual(result['planning_attempts'], 0)
        self.assertEqual(result['stages'], [])
        self.assertFalse((self.root/'codex-inaccessible').exists())

    def test_managed_pi_origin_proof_uses_same_cli_human_roundtrip_without_user_authority(self):
        """Actual local ACP subprocess fixture; no actual Pi/provider claim.

        The synchronous CLI entry point runs in a foreground thread of its
        trusted host, using the existing in-process Pi transport. Public CLI
        subprocess coverage is separate above; no socket/service is needed.
        """
        from pi_session_coordinator import PiSessionAdapter
        from pi_session_profile import Launch
        import appserver
        import codex_session_adapter
        import workflow
        class Profile:
            def launch(profile, runtime, project, sandbox):
                return Launch((sys.executable, str(ROOT/'tests/fixtures/pi_coordinator_fixture.py'), str(runtime)),
                    dict(HOME=str(runtime), PATH='/nonexistent', CODEX_HOME=str(runtime/'unavailable-codex')),
                    'synthetic ACP fixture, not actual Pi')
        def unavailable(*args, **kwargs):
            raise AssertionError('Codex native service inaccessible in managed Pi/CLI fixture')
        async def scenario():
            adapter = PiSessionAdapter(kind='fixture_pi_hub_'+uuid.uuid4().hex)
            agent_registry.register(adapter)
            host = None
            try:
                host, reference = await adapter.create_managed_session(Profile(), self.repo, sandbox='workspace-write')
                plan = runner.read(self.plan)
                plan.pop('agent')
                plan['acceptance'] = [dict(id='artifact', description='independent native fixture artifact check',
                    checks=[[sys.executable, '-c',
                        "from pathlib import Path; assert Path('fixture-artifact.txt').read_text() == 'fixture artifact\\n'"]])]
                runner.save(self.plan, plan)
                directory = workflow.create(self.repo, self.plan, origin_ref=reference.reference)
                host.bind_workflow(directory)
                state = runner.read(directory/'state.json')
                proof = adapter.origin_actor_proof(reference.identity, workflow_id=str(directory),
                    contract_hash=state['contract_hash'], generation=0, source='synthetic existing workflow authority')
                origin_file = self.root/'pi-origin-proof.json'
                local_session_adapter.private_create(origin_file, dict(version=2, reference=reference.reference,
                    workflow_id=str(directory), proof=asdict(proof)))
                hub_file, user_file = self.root/'pi-hub-grant.json', self.root/'pi-user-grant.json'
                local_session_adapter.grant(self.hub_key, directory, hub_file, 'synthetic hub grant')
                local_session_adapter.grant(self.user_key, directory, user_file, 'synthetic actual user grant')
                def entry(*args):
                    output = io.StringIO()
                    with patch.object(sys, 'argv', ['runner.py', *map(str, args)]), redirect_stdout(output):
                        from fixtures.legacy_cli import main
                        status = main(runner)
                    self.assertEqual(status, 0, output.getvalue())
                    return json.loads(output.getvalue())
                q = await asyncio.to_thread(entry, 'ask', directory, '--question', 'Synthetic Pi: produce tool artifact?',
                    '--actor-file', origin_file)
                shown = await asyncio.to_thread(entry, 'read', q['id'], '--actor-file', hub_file)
                self.assertEqual(shown['question'], q['question'])
                answered = await asyncio.to_thread(entry, 'respond', q['id'], '--answer', 'A tool',
                    '--source', 'synthetic actual user fixture, not production authorization', '--actor-file', user_file)
                self.assertEqual(answered['answer_actor']['role'], 'user')
                await host.drain()
                self.assertFalse(host.state['workflow_completed_by_end_turn'])
                self.assertEqual(runner.read(directory/'state.json')['criteria']['artifact']['status'], 'pending')
                accepted = await asyncio.to_thread(entry, 'accept-answer', q['id'], '--actor-file', origin_file)
                self.assertEqual(accepted['accepted_actor']['role'], 'origin')
                self.assertEqual(accepted['accepted_actor']['session']['reference'], reference.reference)
                # The host is now idle. Foreground workflow execution owns OS
                # signal handling and therefore runs on the main CLI thread.
                result = entry('continue-task', directory, '--accepted-answer', q['id'],
                    '--actor-file', origin_file)
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['criteria']['artifact']['status'], 'passed')
                self.assertEqual(len((host.runtime/'admissions.jsonl').read_text().splitlines()), 1)
            finally:
                if host:
                    await host.close()
                agent_registry._adapters.pop(adapter.kind, None)
        with ExitStack() as stack:
            for name in ('session_path', 'queue', 'create_session', 'current'):
                stack.enter_context(patch.object(codex_session_adapter, name, side_effect=unavailable))
            stack.enter_context(patch.object(appserver, 'Client', side_effect=unavailable))
            previous = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}
            try:
                asyncio.run(scenario())
            finally:
                for value, handler in previous.items():
                    signal.signal(value, handler)

    def test_hub_binding_requires_its_distinct_root_hub_proof(self):
        before = interaction.config()
        args = ('hub', '--session-ref', self.hub, '--source', 'synthetic unauthorized rebind')
        self.cli(*args, ok=False)
        for credential in (self.origin_key, self.user_key, self.hub_grant):
            self.cli(*args, '--actor-file', credential, ok=False)
        self.assertEqual(interaction.config(), before)

    def test_environment_and_registration_never_authenticate_actor(self):
        self.cli('ask', self.workflow, '--question', 'forged fixture?', ok=False,
            extra_env=dict(CTASK_SESSION_REF=self.origin, CTASK_SESSION_TOKEN='pretend',
                           CTASK_ENDPOINT_TOKEN='pretend', CODEX_THREAD_ID='synthetic-invalid-locator'))
        self.assertFalse(list((interaction.directory()/'questions').glob('*.json')))

    def test_hub_or_origin_model_output_cannot_become_user_answer(self):
        q = self.ask()
        for credential in (self.hub_grant, self.origin_grant):
            self.cli('answer', q['id'], '--answer', 'model says approved', '--source', 'adapter end_turn',
                '--actor-file', credential, ok=False)
        self.assertEqual(self.question_record(q)['state'], 'pending')
        self.assertNotIn('answer_source', self.question_record(q))

    def test_actual_source_required_even_with_user_credential(self):
        q = self.ask()
        self.cli('answer', q['id'], '--answer', 'A', '--source', ' ',
            '--actor-file', self.user_grant, ok=False)
        self.assertEqual(self.question_record(q)['state'], 'pending')

    def test_wrong_actor_cannot_ask_accept_or_present(self):
        self.cli('ask', self.workflow, '--question', 'forged?', '--actor-file', self.hub_grant, ok=False)
        q = self.ask()
        self.answer(q)
        self.cli('accept-answer', q['id'], '--actor-file', self.user_grant, ok=False)
        self.cli('read-question', q['id'], '--actor-file', self.user_grant,
            '--presented-source', 'forged display', ok=False)
        self.assertEqual(self.question_record(q)['state'], 'answered')
        self.assertNotIn('presentation', self.question_record(q))

    def test_listing_reading_and_passive_turn_completion_are_not_presentation_or_acceptance(self):
        q = self.ask()
        self.json_cli('inbox', '--actor-file', self.hub_grant)
        self.json_cli('read-question', q['id'], '--actor-file', self.hub_grant)
        record = self.question_record(q)
        self.assertNotIn('presentation', record)
        self.assertEqual(record['state'], 'pending')
        message = transport.managed_bus().message(record['question_delivery']['mailbox_id'])
        self.assertEqual(message['state'], 'turn_completed')
        self.assertEqual(message['canonical_id'], q['id'])
        self.assertEqual(runner.read(self.workflow/'state.json')['criteria']['result']['status'], 'pending')
        self.assertFalse((self.repo/'result').exists())

    def test_old_execution_grants_and_question_are_rejected(self):
        q = self.ask()
        state = runner.read(self.workflow/'state.json')
        state['execution_revision'] = 1
        runner.save(self.workflow/'state.json', state)
        self.cli('answer', q['id'], '--answer', 'A', '--source', 'synthetic user',
            '--actor-file', self.user_grant, ok=False)
        self.cli('ask', self.workflow, '--question', 'stale credential', '--actor-file', self.origin_grant, ok=False)
        self.assertEqual(self.question_record(q)['state'], 'pending')

    def test_workflow_start_timestamp_also_fences_same_revision_credentials(self):
        self.ask()
        (self.workflow/'events.jsonl').write_text(json.dumps(dict(event='workflow_started', time=123.0))+'\n')
        self.cli('inbox', '--actor-file', self.hub_grant, ok=False)

    def test_stop_blocks_answer_accept_grant_and_continuation_without_clearing_stop(self):
        q = self.ask()
        self.answer(q)
        (self.workflow/'STOP').touch()
        self.cli('accept-answer', q['id'], '--actor-file', self.origin_grant, ok=False)
        self.cli('continue-task', self.workflow, '--accepted-answer', q['id'],
            '--actor-file', self.origin_grant, ok=False)
        self.cli('local-session-grant', '--credential-file', self.user_key, '--workflow', self.workflow,
            '--output', self.root/'late-grant', '--source', 'synthetic source', ok=False)
        self.assertTrue((self.workflow/'STOP').exists())
        self.assertEqual(self.question_record(q)['state'], 'answered')

    def test_conflicting_duplicate_answer_and_unaccepted_continuation_rejected(self):
        q = self.ask()
        self.answer(q)
        self.answer(q)  # Exact duplicate retains one canonical envelope.
        self.cli('respond', q['id'], '--answer', 'B', '--source', 'synthetic different answer',
            '--actor-file', self.user_grant, ok=False)
        self.cli('continue-task', self.workflow, '--accepted-answer', q['id'],
            '--actor-file', self.origin_grant, ok=False)
        self.assertFalse((self.repo/'result').exists())
        self.assertEqual(len([x for x in transport.managed_bus().events() if x['kind'] == 'enqueued']), 2)

    def test_tampered_token_public_or_symlink_credential_rejected(self):
        q = self.ask()
        data = runner.read(self.user_grant)
        data['proof']['challenge'] = 'forged-synthetic-token'
        bad = self.root/'bad.json'
        local_session_adapter.private_create(bad, data)
        self.cli('respond', q['id'], '--answer', 'A', '--source', 'synthetic user', '--actor-file', bad, ok=False)
        self.user_grant.chmod(0o644)
        self.cli('respond', q['id'], '--answer', 'A', '--source', 'synthetic user', '--actor-file', self.user_grant, ok=False)
        self.user_grant.chmod(0o600)
        link = self.root/'linked.json'
        link.symlink_to(self.user_grant)
        self.cli('respond', q['id'], '--answer', 'A', '--source', 'synthetic user', '--actor-file', link, ok=False)
        self.assertEqual(self.question_record(q)['state'], 'pending')

    def test_scoped_grant_cannot_mint_new_credentials_or_cross_workflows(self):
        self.cli('local-session-grant', '--credential-file', self.hub_grant, '--workflow', self.workflow,
            '--output', self.root/'regrant', '--source', 'synthetic source', ok=False)
        other = Path(self.cli('workflow-create', '--repo', self.repo, '--plan', self.plan,
            '--origin-ref', self.origin).stdout.strip())
        self.cli('ask', other, '--question', 'wrong workflow?', '--actor-file', self.origin_grant, ok=False)

    def test_unavailable_native_activity_and_resume_are_truthful(self):
        capabilities = session_router.capability_contract(self.hub)
        self.assertEqual(capabilities.state('session.activity'), agent_registry.CapabilityState.UNKNOWN)
        self.assertEqual(capabilities.state('session.load_resume'), agent_registry.CapabilityState.UNSUPPORTED)
        self.assertEqual(capabilities.state('session.observe'), agent_registry.CapabilityState.UNSUPPORTED)
        with self.assertRaises(agent_registry.CapabilityUnavailable):
            session_router.observe_v2(self.hub, 'synthetic', generation=0, cursor={})
        session, _ = session_router.resolve_v2(self.hub)
        self.assertNotEqual(session.identity.native_id, self.hub)

    def test_unknown_delivery_has_one_barrier_and_is_never_replayed(self):
        proof, _, _ = local_session_adapter.load_actor_file(self.origin_grant)
        with patch.object(local_session_adapter.LocalCLIAdapter, 'send', side_effect=RuntimeError('synthetic lost receipt')) as send:
            q = interaction.ask(self.workflow, 'synthetic unknown delivery?', actor_proof=proof)
            again = interaction.ask(self.workflow, 'synthetic unknown delivery?', actor_proof=proof)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(q['id'], again['id'])
        self.assertEqual(again['question_delivery']['state'], 'delivery_unknown')
        self.assertEqual(len([x for x in transport.managed_bus().events() if x['kind'] == 'enqueued']), 1)

    def test_passive_receipt_rejects_wrong_exact_session_before_acknowledgment(self):
        proof, _, _ = local_session_adapter.load_actor_file(self.origin_grant)
        _, adapter = session_router.resolve_v2(self.hub)
        other, _ = session_router.resolve_v2(self.origin)
        send = adapter.send
        with patch.object(adapter, 'send', side_effect=lambda endpoint, message:
                send(dict(endpoint, session_id=other.identity.native_id), message)):
            q = interaction.ask(self.workflow, 'synthetic wrong endpoint?', actor_proof=proof)
        message = transport.managed_bus().message(q['question_delivery']['mailbox_id'])
        self.assertEqual(message['state'], 'delivery_unknown')
        self.assertIsNone(message['turn_id'])
        self.assertFalse([row for row in transport.managed_bus().events(message_id=message['id'])
            if row['kind'] == 'native_event'])

    def test_private_actor_audit_is_recorded_without_token(self):
        q = self.ask()
        answer = self.answer(q)
        self.assertEqual(answer['answer_actor']['role'], 'user')
        self.assertEqual(self.accept(q)['accepted_actor']['role'], 'origin')
        credential = runner.read(self.user_grant)
        self.assertNotIn(credential['proof']['challenge'], json.dumps(self.question_record(q)))

    def test_manual_criterion_requires_scoped_actual_user_and_evidence(self):
        plan = runner.read(self.plan)
        plan['acceptance'].append(dict(id='human', description='synthetic manual acceptance', kind='manual'))
        runner.save(self.plan, plan)
        self.workflow = Path(self.cli('workflow-create', '--repo', self.repo, '--plan', self.plan,
            '--origin-ref', self.origin).stdout.strip())
        self.origin_grant = self.grant(self.origin_key, 'manual-origin.json')
        self.hub_grant = self.grant(self.hub_key, 'manual-hub.json')
        self.user_grant = self.grant(self.user_key, 'manual-user.json')
        evidence = self.repo/'synthetic-user-decision.txt'
        evidence.write_text('Synthetic fixture user explicitly accepted; no real task authority')
        args = ('acknowledge', self.workflow, '--criterion', 'human', '--evidence', evidence,
            '--note', 'synthetic actual user acceptance source')
        self.cli(*args, ok=False)
        self.cli(*args, '--actor-file', self.origin_grant, ok=False)
        self.cli(*args, '--actor-file', self.hub_grant, ok=False)
        self.assertFalse((self.workflow/'acknowledgements/human.json').exists())
        self.cli(*args, '--actor-file', self.user_grant)
        receipt = runner.read(self.workflow/'acknowledgements/human.json')
        self.assertEqual(receipt['user_actor']['role'], 'user')
        self.assertEqual(receipt['evidence'], str(evidence))
        # A user acknowledgment records evidence, never bypasses final rechecks.
        self.assertEqual(runner.read(self.workflow/'state.json')['criteria']['human']['status'], 'pending')

    def test_wrong_project_or_changed_contract_rejected_before_question_mutation(self):
        q = self.ask()
        row = runner.read(session_router._path(self.hub))
        row['identity']['project'] = str(self.root)
        runner.save(session_router._path(self.hub), row)
        self.cli('respond', q['id'], '--answer', 'A', '--source', 'synthetic user',
            '--actor-file', self.user_grant, ok=False)
        row['identity']['project'] = str(self.repo)
        runner.save(session_router._path(self.hub), row)
        contract = runner.read(self.workflow/'contract.json')
        contract['objective'] = 'synthetic tampering, never authorized'
        runner.save(self.workflow/'contract.json', contract)
        self.cli('respond', q['id'], '--answer', 'A', '--source', 'synthetic user',
            '--actor-file', self.user_grant, ok=False)
        self.assertEqual(self.question_record(q)['state'], 'pending')


if __name__ == '__main__':
    unittest.main()
