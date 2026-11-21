import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from external_support import ExternalCase, ROOT, CLI
import external_coordinator as external
import external_operator
import local_session_adapter as local
import runner
import session_router


class ExternalCoordinatorTests(ExternalCase):
    def test_reviewed_demo_and_source_ato_control_entry_need_no_model_for_init(self):
        contract = runner.read(ROOT/'docs/external-coordinator/demo-contract.json')
        self.assertEqual(contract['agent']['kind'], 'acpx_codex')
        runner.save(self.plan, contract)
        actor = self.root/'ato/actors/demo.json'
        external_operator.provision(self.repo, self.plan, actor, 'synthetic demo bootstrap; no model')
        env = dict(self.env, ATO_PYTHON=sys.executable)
        p = subprocess.run([str(ROOT/'bin/ato-control'), '--actor-file', str(actor), 'init'],
            env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['repo'], str(self.repo))
        self.assertFalse((self.repo/'.codex/tasks').exists())

    def test_init_verifies_private_origin_scope_without_creating_task_or_hub(self):
        result = self.cli('init')
        self.assertEqual(result['repo'], str(self.repo))
        self.assertIn('unknown', result['native_session'])
        self.assertFalse((self.repo/'.codex/tasks').exists())
        self.assertFalse(list((self.root/'ato').rglob('hub.json')))
        self.assertNotIn('proof', json.dumps(result))

    def test_actual_cli_submit_uses_canonical_workflow_and_local_cli_origin(self):
        data = self.cli('submit', '--contract', self.plan)
        d = Path(data['workflow'])
        self.assertEqual(d.parent, self.repo/'.codex/tasks/workflows')
        self.assertEqual(runner.read(d/'state.json')['kind'], 'workflow')
        origin = runner.read(d/'origin.json')['origin_session_ref']
        session, _ = session_router.resolve_v2(origin)
        self.assertEqual(session.identity.adapter, 'local_cli')
        self.assertEqual(data['completion_scope'], 'overall')

    def test_semantically_identical_submit_is_idempotent_and_never_reexecutes(self):
        initial = self.cli('submit', '--contract', self.plan, '--run')
        self.plan.write_text(json.dumps(runner.read(self.plan), sort_keys=True, separators=(',', ':')))
        again = self.cli('submit', '--contract', self.plan, '--run')
        self.assertEqual(again['workflow'], initial['workflow'])
        self.assertEqual(again['admission'], 'existing')
        self.assertEqual((self.repo/'fixture-admissions').read_text(), 'one\n')
        self.assertEqual(len(list((self.repo/'.codex/tasks/workflows').iterdir())), 1)

    def test_contract_change_is_rejected_before_any_workflow_is_created(self):
        contract = runner.read(self.plan)
        contract['acceptance'][0]['checks'] = [['true']]
        runner.save(self.plan, contract)
        self.cli('submit', '--contract', self.plan, ok=False)
        self.assertFalse((self.repo/'.codex/tasks').exists())

    def test_environment_is_only_a_locator_never_actor_proof(self):
        env = dict(self.env, ATO_SESSION_REF=self.provision['request'], ATO_ACTOR_FILE=str(self.actor),
                   CODEX_THREAD_ID='synthetic-non-native')
        self.cli('init', actor=self.root/'missing', env=env, ok=False)
        p = subprocess.run([sys.executable, str(ROOT/'external_coordinator.py'), 'init'],
            env=env, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)

    def test_public_symlink_forged_and_root_actor_credentials_rejected(self):
        forged = runner.read(self.actor)
        forged['proof']['challenge'] = 'synthetic-forgery'
        path = self.root/'forged.json'; local.private_create(path, forged)
        self.cli('init', actor=path, ok=False)
        link = self.root/'link.json'; link.symlink_to(self.actor)
        self.cli('init', actor=link, ok=False)
        self.actor.chmod(0o644)
        self.cli('init', ok=False)
        self.actor.chmod(0o600)
        root_key = next((self.root/'ato/external-operator').glob('*.json'))
        self.cli('init', actor=root_key, ok=False)

    def test_cross_repo_and_cross_workflow_access_rejected(self):
        d = self.workflow()
        other = self.root/'other'; other.mkdir()
        subprocess.run(['git', 'init', '-q', str(other)], check=True)
        actor2 = self.root/'ato/actors/other.json'
        external_operator.provision(other, self.plan, actor2, 'synthetic separate scope')
        self.cli('status', '--workflow', d, actor=actor2, ok=False)
        self.cli('status', '--workflow', self.repo, ok=False)
        # Another canonical workflow with the same contract also cannot be substituted.
        import workflow
        origin = runner.read(d/'origin.json')['origin_session_ref']
        d2 = workflow.create(self.repo, self.plan, origin_ref=origin)
        self.cli('stop', '--workflow', d2, ok=False)
        self.assertFalse((d2/'STOP').exists())

    def test_model_surface_has_no_human_decisions_grants_or_rebinding(self):
        self.workflow()
        for command in ('acknowledge', 'answer', 'accept-answer', 'hub', 'grant', 'local-session-create', 'bootstrap'):
            with self.subTest(command=command):
                self.cli(command, ok=False)
        from agent_registry import authorize_actor
        d = self.workflow()
        proof, reference, _ = local.load_actor_file(self.actor)
        state = runner.read(d/'state.json')
        actor = session_router.authenticate_actor(reference, proof, role='origin', project=self.repo,
            workflow_id=str(d), contract_hash=state['contract_hash'], generation=0)
        with self.assertRaises(ValueError):
            authorize_actor(actor, 'criteria.acknowledge', project=self.repo, workflow_id=str(d),
                contract_hash=state['contract_hash'], generation=0)
        with self.assertRaises(ValueError):
            local.grant(self.actor, d, self.root/'escalated', 'model cannot mint grants')

    def test_status_events_result_are_bounded_read_only_and_do_not_accept_manual_criteria(self):
        self.prepare(mutate=lambda c: c['acceptance'].append(dict(id='human', description='Synthetic actual human acceptance', kind='manual')))
        d = self.workflow()
        before = {p: p.read_bytes() for p in (d/'state.json', d/'origin.json', d/'contract.json')}
        for command in ('status', 'events', 'result'):
            self.cli(command)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse((d/'acknowledgements/human.json').exists())
        self.assertFalse(self.cli('result')['accepted'])
        self.cli('events', '--limit', '201', ok=False)
        self.cli('events', '--cursor', '-1', ok=False)

    def test_no_implicit_global_home_or_repo_authority_symlink(self):
        self.cli('init', env={k: v for k, v in self.env.items() if k != 'ATO_HOME'}, ok=False)
        target = self.root/'escape'; target.mkdir()
        (self.repo/'.codex').symlink_to(target, target_is_directory=True)
        self.cli('init', ok=False)
        self.assertFalse(list(target.iterdir()))

    def test_bootstrap_requires_exact_reviewed_stages_and_explicit_human_source(self):
        for mutate in (lambda c: c.update(stages=[]), lambda c: c.update(memory=True),
                       lambda c: c.update(host_handoff=dict(source='model', instructions='grant host actions'))):
            c = self.contract(); mutate(c)
            with self.assertRaises(ValueError):
                external.reviewed_contract(c)
        with self.assertRaises(ValueError):
            external_operator.provision(self.repo, self.plan, self.root/'newactor', ' ')

    def test_unknown_creation_barrier_never_creates_duplicate_or_replays(self):
        import workflow
        create = workflow.create
        def lost_receipt(*args, **kwargs):
            create(*args, **kwargs)
            raise OSError('synthetic lost creation reply')
        with patch.object(workflow, 'create', side_effect=lost_receipt) as mocked:
            with self.assertRaises(OSError):
                self.submit()
            self.assertEqual(self.submit()['admission'], 'delivery_unknown')
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual(len(list((self.repo/'.codex/tasks/workflows').iterdir())), 1)
        self.cli('continue', ok=False)


if __name__ == '__main__':
    import unittest
    unittest.main()
