"""Public-path qualification using private homes, synthetic actors and real Git repos."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agents
import agent_registry
import codex_session_adapter
import control
import interaction
import notifications
import recovery
import runner
import session_router
import workflow
import workspace_association as association

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = '61111111-1111-4111-8111-111111111111'
HUB = '62222222-2222-4222-8222-222222222222'
FOREIGN = '63333333-3333-4333-8333-333333333333'


class WorkspaceBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-workspace-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root/'workspace'; self.workspace.mkdir()
        self.repo = self.workspace/'child'
        self.git('init', '-q', str(self.repo))
        self.native = self.root/'native'; self.native.mkdir()
        self.ato = self.root/'ato'
        self.called = self.root/'queue-was-called'
        self.queue = self.root/'queue-denied'
        self.queue.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(self.called)!r}).touch()\nraise SystemExit(88)\n')
        self.queue.chmod(0o700)
        env = dict(CODEX_HOME=str(self.native), ATO_HOME=str(self.ato), CODEX_THREAD_ID=ORIGIN,
                   CTASK_QUEUE_BIN=str(self.queue))
        self.environment = patch.dict(os.environ, env)
        self.environment.start(); self.addCleanup(self.environment.stop)
        for key in ('CTASK_SESSION_REF', 'ATO_SESSION_REF', 'ATO_ACTOR_FILE',
                    'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            os.environ.pop(key, None)
        self.paths = {}
        with sqlite3.connect(self.native/'state_5.sqlite') as con:
            con.execute('create table threads(id text, rollout_path text)')
            for actor, cwd in ((ORIGIN, self.workspace), (HUB, self.root), (FOREIGN, self.root)):
                path = self.native/(actor+'.jsonl')
                path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=actor, cwd=str(cwd))))+'\n')
                self.paths[actor] = path
                con.execute('insert into threads values(?,?)', (actor, str(path)))
        plan = self.root/'plan.json'
        runner.save(plan, dict(objective='Complete five synthetic rectifications',
            scope='Private fixture only; original full acceptance retained',
            authorization='Synthetic fixture actor authority', memory=False,
            acceptance=[dict(id='all-five', description='Complete full fixture goal', checks=[['true']])]))
        self.d = workflow.create(self.repo, plan, ORIGIN)
        state = runner.read(self.d/'state.json'); state['status'] = 'blocked'
        runner.save(self.d/'state.json', state)
        interaction.configure(HUB, 'Synthetic user chose fixture hub')
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            notifications.configure_project(self.repo, True, 'Synthetic fixture silence; no queue calls')
            control.enable('Synthetic fixture follow-up', 60, 0)
        self.proof = self.root/'association-proof.json'
        self.adapter = agent_registry.get('codex')
        self.addCleanup(self.assert_no_send)

    def assert_no_send(self):
        self.assertFalse(self.called.exists(), 'test attempted to dispatch a synthetic queue message')

    def git(self, *argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.run(['git', *argv], env=env, check=True, capture_output=True, text=True)

    def cli(self, *args, actor=ORIGIN, ok=True, **extra):
        env = dict(os.environ, CODEX_THREAD_ID=actor, **extra)
        result = subprocess.run([sys.executable, str(ROOT/'runner.py'), *map(str, args)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
        return result

    def inspect(self, actor=ORIGIN):
        return self.cli('workspace-association', 'inspect', self.d, '--workspace', self.workspace, actor=actor)

    def register(self, actor=ORIGIN):
        runner.save(self.proof, self.inspect(actor)['expected_evidence'])
        return self.cli('workspace-association', 'register', self.d, '--workspace', self.workspace,
                        '--evidence', self.proof, '--source', 'Explicit synthetic association', actor=actor)

    def question(self):
        return self.cli('ask', self.d, '--question', 'Which synthetic option?', '--recommendation', 'Keep scope')

    def immutable(self):
        files = [self.d/'origin.json', self.d/'state.json', self.d/'contract.json',
                 self.native/'state_5.sqlite', *self.paths.values()]
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    def assert_binding_fails(self):
        with self.assertRaises((ValueError, OSError, KeyError)):
            interaction.binding(self.d, require_open=False)

    def test_strict_default_fails_at_read_and_controller_then_exact_cli_registration_repairs(self):
        # Build only the canonical synthetic question: it represents the old
        # already-persisted question whose legacy native cwd predates Git root.
        s = runner.read(self.d/'state.json')
        qid = 'question-'+'1'*20
        folder = interaction.directory()/'questions'; folder.mkdir()
        runner.save(folder/(qid+'.json'), dict(id=qid, workflow=str(self.d), objective='Full fixture goal',
            state='pending', question='Synthetic decision?', recommendation='Keep scope', context='',
            contract_hash=s['contract_hash'], origin_thread=ORIGIN, hub_thread=HUB,
            hub_source='Synthetic instruction', generation=recovery.generation(self.d, s),
            created_at=runner.now(), question_delivery=dict(state='recorded_only')))
        self.cli('read-question', qid, actor=HUB, ok=False)
        self.cli('control', 'tick', actor=HUB)
        self.assertEqual(next(a for a in control.rows(True) if a['id'] == qid)['kind'], 'source_error')
        before = self.immutable()
        self.register(actor=HUB)
        q = self.cli('read-question', qid, actor=HUB)
        self.assertEqual(q['state'], 'pending'); self.assertIsNone(q['presentation'])
        self.cli('control', 'tick', actor=HUB)
        action = next(a for a in control.rows(True) if a['id'] == qid)
        self.assertEqual(action['kind'], 'question')
        self.assertNotIn('presented_at', action)
        self.assertEqual(self.immutable(), before)
        self.assertEqual(interaction.binding(self.d)[3], ORIGIN)

    def test_registered_origin_can_ask_and_hub_read_without_approval_or_actor_upgrade(self):
        before = self.immutable(); self.register()
        q = self.question()
        self.assertEqual(q['question_delivery']['state'], 'recorded_only')
        self.assertEqual(self.cli('read-question', q['id'], actor=HUB)['state'], 'pending')
        self.assertFalse(self.adapter.verify_session(ORIGIN, self.repo))
        self.assertFalse(self.adapter.is_actor(ORIGIN, self.repo))
        self.assertTrue(self.adapter.verify_session(ORIGIN, self.workspace))
        with self.assertRaises(ValueError):
            session_router.register('codex', ORIGIN, self.repo, 'Cannot broaden actor authority')
        identity = agent_registry.SessionIdentity('codex', ORIGIN, str(self.repo), 'Synthetic proof')
        with self.assertRaises(ValueError):
            session_router.register_v2(identity)
        with self.assertRaises(ValueError):
            session_router.bind_origin(runner.read(self.d/'origin.json'), self.repo)
        self.assertEqual(self.immutable(), before)

    def test_registration_is_idempotent_and_evidence_pin_does_not_follow_transcript_growth(self):
        first = self.register()
        self.paths[ORIGIN].write_text(self.paths[ORIGIN].read_text()+json.dumps(
            dict(type='event_msg', payload=dict(type='agent_message', message='Synthetic progress')))+'\n')
        second = self.cli('workspace-association', 'register', self.d, '--workspace', self.workspace,
            '--evidence', self.proof, '--source', 'Same registration')
        self.assertEqual(first, second)
        self.assertEqual(self.inspect()['validation'], 'valid')

    def test_foreign_actor_cannot_inspect_register_revoke_read_ask_answer_accept(self):
        self.register(); q = self.question()
        commands = [('workspace-association', 'inspect', self.d, '--workspace', self.workspace),
            ('workspace-association', 'register', self.d, '--workspace', self.workspace,
             '--evidence', self.proof, '--source', 'Foreign actor'),
            ('workspace-association', 'revoke', self.d, '--source', 'Foreign actor'),
            ('read-question', q['id']), ('ask', self.d, '--question', 'Foreign ask'),
            ('answer', q['id'], '--answer', 'Pretend approval', '--source', 'Foreign actor'),
            ('accept-answer', q['id'])]
        for command in commands:
            with self.subTest(command=command):
                self.cli(*command, actor=FOREIGN, ok=False)
        self.cli('read-question', q['id'], actor=ORIGIN, ok=False)
        self.cli('ask', self.d, '--question', 'Hub cannot impersonate origin', actor=HUB, ok=False)
        self.assertEqual(self.inspect()['validation'], 'valid')

    def test_unverified_actor_locator_and_v2_grant_file_cannot_authorize_registration(self):
        self.register()
        self.cli('workspace-association', 'inspect', self.d, '--workspace', self.workspace,
                 actor='64444444-4444-4444-8444-444444444444', ok=False)
        # Native locator still has to match the allowed actor; neutral locator
        # and ambient files grant no legacy role.
        self.cli('workspace-association', 'revoke', self.d, '--source', 'Spoofed reference',
                 actor=FOREIGN, CTASK_SESSION_REF='agentref-'+'a'*32, ok=False)
        fake = self.root/'fake-actor.json'; runner.save(fake, dict(role='hub', workflow=str(self.d)))
        self.cli('workspace-association', 'register', self.d, '--workspace', self.workspace,
                 '--evidence', self.proof, '--source', 'Invalid grant', '--actor-file', fake, ok=False)

    def test_revoke_preserves_history_fails_closed_and_requires_explicit_reregistration(self):
        first = self.register(); q = self.question(); before = self.immutable()
        self.cli('workspace-association', 'revoke', self.d, '--source', 'Explicit synthetic revocation', actor=HUB)
        self.assert_binding_fails()
        self.cli('read-question', q['id'], actor=HUB, ok=False)
        second = self.register()
        self.assertNotEqual(first['association_id'], second['association_id'])
        path, _ = association._paths(self.d)
        ledger = runner.read(path)
        self.assertEqual([r['state'] for r in ledger['records']], ['revoked', 'active'])
        self.assertEqual(ledger['records'][0]['proof'], first['proof'])
        self.assertEqual(self.immutable(), before)

    def test_execution_generation_and_started_event_invalidate_association(self):
        self.register()
        original = (self.d/'state.json').read_bytes()
        state = runner.read(self.d/'state.json'); state['execution_revision'] = 1
        runner.save(self.d/'state.json', state); self.assert_binding_fails()
        (self.d/'state.json').write_bytes(original)
        with (self.d/'events.jsonl').open('a') as handle:
            handle.write(json.dumps(dict(event='workflow_started', time=123456789))+'\n')
        self.assert_binding_fails()
        # Revocation remains possible after expiry; no old workflow mutation.
        self.cli('workspace-association', 'revoke', self.d, '--source', 'Stale generation', actor=HUB)

    def test_evidence_tampering_and_extra_claims_cannot_create_association(self):
        proof = self.inspect()['expected_evidence']
        for change in ({'project': str(self.root)}, {'execution_authority': True}, {'version': 2},
                       {'generation': [None, 9]}, {'workflow_id': 'foreign'}):
            runner.save(self.proof, dict(proof, **change))
            self.cli('workspace-association', 'register', self.d, '--workspace', self.workspace,
                     '--evidence', self.proof, '--source', 'Changed evidence', ok=False)
        self.assert_binding_fails()
        self.register(); self.proof.write_text(self.proof.read_text()+' ')
        self.assert_binding_fails()

    def test_record_tampering_is_not_a_new_trusted_observation(self):
        self.register(); path, _ = association._paths(self.d)
        ledger = runner.read(path); ledger['records'][-1]['proof']['project'] = str(self.root)
        runner.save(path, ledger); self.assert_binding_fails()

    def test_invalid_sources_oversized_proof_and_symlink_lock_do_not_register(self):
        runner.save(self.proof, self.inspect()['expected_evidence'])
        self.cli('workspace-association', 'register', self.d, '--workspace', self.workspace,
                 '--evidence', self.proof, '--source', ' ', ok=False)
        self.proof.write_text(' '*(association.MAX_EVIDENCE_BYTES+1))
        self.cli('workspace-association', 'register', self.d, '--workspace', self.workspace,
                 '--evidence', self.proof, '--source', 'Oversized fixture proof', ok=False)
        runner.save(self.proof, self.inspect()['expected_evidence'])
        path, lock = association._paths(self.d, create=True)
        sentinel = self.root/'lock-sentinel'; sentinel.write_text('Unchanged')
        lock.symlink_to(sentinel)
        self.cli('workspace-association', 'register', self.d, '--workspace', self.workspace,
                 '--evidence', self.proof, '--source', 'Symlink lock', ok=False)
        self.assertFalse(path.exists()); self.assertEqual(sentinel.read_text(), 'Unchanged')

    def test_header_changes_cannot_bypass_revocation_by_returning_to_exact_project(self):
        self.register()
        self.cli('workspace-association', 'revoke', self.d, '--source', 'Explicit fixture revocation')
        self.paths[ORIGIN].write_text(json.dumps(dict(type='session_meta',
            payload=dict(id=ORIGIN, cwd=str(self.repo))))+'\n')
        self.assert_binding_fails()

    def test_wrong_origin_home_header_project_and_contract_are_rejected(self):
        self.register()
        originals = {p: p.read_bytes() for p in (self.d/'origin.json', self.d/'state.json',
            self.d/'contract.json', self.paths[ORIGIN], interaction.directory()/'hub.json')}
        mutations = [
            (self.d/'origin.json', {'origin_thread_id': FOREIGN}),
            (self.d/'origin.json', {'run_id': 'wrong-workflow'}),
            (self.d/'origin.json', {'repo': str(self.workspace)}),
            (self.d/'origin.json', {'codex_home': str(self.ato)}),
            (self.d/'state.json', {'repo': str(self.workspace)}),
            (self.d/'state.json', {'id': 'foreign'}),
            (self.d/'contract.json', {'scope': 'Changed authority'}),
            (interaction.directory()/'hub.json', {'thread_id': FOREIGN}),
            (interaction.directory()/'hub.json', {'codex_home': str(self.ato)})]
        for path, change in mutations:
            with self.subTest(change=change):
                runner.save(path, dict(json.loads(originals[path]), **change))
                self.assert_binding_fails()
                path.write_bytes(originals[path])
        for payload in (dict(id=FOREIGN, cwd=str(self.workspace)),
                        dict(id=ORIGIN, cwd=str(self.root)), dict(id=ORIGIN)):
            with self.subTest(payload=payload):
                runner.save(self.paths[ORIGIN], dict(type='session_meta', payload=payload))
                self.assert_binding_fails()
                self.paths[ORIGIN].write_bytes(originals[self.paths[ORIGIN]])
        self.assertEqual(self.inspect()['validation'], 'valid')

    def test_native_home_switch_even_with_copied_identity_cannot_reuse_association(self):
        self.register()
        other = self.root/'other-native'; shutil.copytree(self.native, other)
        with patch.dict(os.environ, CODEX_HOME=str(other)):
            self.assert_binding_fails()

    def test_invalid_contract_grant_is_not_legalized_by_matching_state_digest(self):
        contract = runner.read(self.d/'contract.json')
        contract['permissions'] = dict(network_access='yes', source='Invalid synthetic grant')
        runner.save(self.d/'contract.json', contract)
        state = runner.read(self.d/'state.json'); state['contract_hash'] = runner.digest(contract)
        runner.save(self.d/'state.json', state)
        self.cli('workspace-association', 'inspect', self.d, '--workspace', self.workspace, ok=False)
        self.assert_binding_fails()

    def test_foreign_child_repo_and_copied_workflow_cannot_inherit_registration(self):
        self.register()
        foreign = self.workspace/'foreign'; self.git('init', '-q', str(foreign))
        other = foreign/'.codex/tasks/workflows'/self.d.name
        shutil.copytree(self.d, other)
        for filename in ('state.json', 'origin.json'):
            data = runner.read(other/filename); data['repo'] = str(foreign); runner.save(other/filename, data)
        with self.assertRaises(ValueError):
            interaction.binding(other)
        self.cli('workspace-association', 'register', other, '--workspace', self.workspace,
                 '--evidence', self.proof, '--source', 'Wrong repo proof', ok=False)
        self.assertEqual(self.inspect()['validation'], 'valid')

    def test_unverified_git_root_nested_parent_and_linked_worktree_fail_closed(self):
        # Existing .git directory is not sufficient proof of a Git repository.
        shutil.rmtree(self.repo/'.git'); (self.repo/'.git').mkdir()
        self.cli('workspace-association', 'inspect', self.d, '--workspace', self.workspace, ok=False)
        shutil.rmtree(self.repo/'.git'); self.git('init', '-q', str(self.repo))
        self.cli('workspace-association', 'inspect', self.d, '--workspace', self.root, ok=False)
        # Linked-worktree-shaped Git indirection is deliberately unsupported
        # in v1; no fixture commit or worktree creation is needed to reject it.
        linked = self.workspace/'linked'; linked.mkdir()
        (linked/'.git').write_text('gitdir: '+str(self.repo/'.git')+'\n')
        other = linked/'.codex/tasks/workflows'/self.d.name; shutil.copytree(self.d, other)
        for filename in ('state.json', 'origin.json'):
            data = runner.read(other/filename); data['repo'] = str(linked); runner.save(other/filename, data)
        self.cli('workspace-association', 'inspect', other, '--workspace', self.workspace, ok=False)

    def test_git_identity_replacement_and_ambient_git_overrides_cannot_rebind(self):
        self.register()
        foreign = self.root/'foreign'; self.git('init', '-q', str(foreign))
        with patch.dict(os.environ, GIT_DIR=str(foreign/'.git'), GIT_WORK_TREE=str(foreign)):
            self.assertEqual(self.inspect()['validation'], 'valid')
        (self.repo/'.git').rename(self.repo/'.git-old')
        self.git('init', '-q', str(self.repo))
        self.assert_binding_fails()

    def test_symlink_alias_workflow_evidence_header_and_git_escape_rejected(self):
        self.register()
        alias = self.root/'alias'; alias.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(ValueError):
            interaction.binding(alias/'child'/self.d.relative_to(self.repo))
        original = self.paths[ORIGIN].read_bytes()
        runner.save(self.paths[ORIGIN], dict(type='session_meta', payload=dict(id=ORIGIN, cwd=str(alias))))
        self.assert_binding_fails(); self.paths[ORIGIN].write_bytes(original)
        actual = self.root/'actual-proof.json'; self.proof.rename(actual); self.proof.symlink_to(actual)
        self.assert_binding_fails(); self.proof.unlink(); actual.rename(self.proof)
        (self.repo/'.git').rename(self.root/'git-external')
        (self.repo/'.git').symlink_to(self.root/'git-external', target_is_directory=True)
        self.assert_binding_fails()

    def test_native_rollout_symlink_outside_home_and_index_ambiguity_rejected(self):
        self.register()
        original = self.paths[ORIGIN]
        external = self.root/'external.jsonl'; original.rename(external); original.symlink_to(external)
        self.assert_binding_fails(); original.unlink(); external.rename(original)
        shutil.copyfile(original, external)
        with sqlite3.connect(self.native/'state_5.sqlite') as con:
            con.execute('update threads set rollout_path=? where id=?', (str(external), ORIGIN))
        self.assert_binding_fails()
        with sqlite3.connect(self.native/'state_5.sqlite') as con:
            con.execute('update threads set rollout_path=? where id=?', (str(original), ORIGIN))
        with sqlite3.connect(self.native/'state_5.sqlite') as con:
            con.execute('insert into threads values(?,?)', (ORIGIN, str(original)))
        self.assert_binding_fails()

    def test_stop_and_completion_boundaries_are_preserved_after_registration(self):
        self.register(); q = self.question(); (self.d/'STOP').write_text('Synthetic original STOP')
        before = (self.d/'STOP').read_bytes()
        self.cli('read-question', q['id'], actor=HUB, ok=False)
        self.cli('ask', self.d, '--question', 'No STOP override', ok=False)
        self.assertEqual(interaction.binding(self.d, require_open=False)[3], ORIGIN)
        self.assertEqual((self.d/'STOP').read_bytes(), before)
        (self.d/'STOP').unlink()
        state = runner.read(self.d/'state.json'); state['status'] = 'completed'; runner.save(self.d/'state.json', state)
        self.cli('read-question', q['id'], actor=HUB, ok=False)

    def test_original_exact_project_and_missing_cwd_compatibility_remain_unchanged(self):
        self.paths[ORIGIN].write_text(json.dumps(dict(type='session_meta', payload=dict(id=ORIGIN, cwd=str(self.repo))))+'\n')
        self.assertEqual(interaction.binding(self.d)[3], ORIGIN)
        self.assertTrue(self.adapter.verify_session(ORIGIN, self.repo))
        self.paths[ORIGIN].write_text(json.dumps(dict(type='session_meta', payload=dict(id=ORIGIN)))+'\n')
        self.assertEqual(interaction.binding(self.d)[3], ORIGIN)
        self.assertFalse(self.adapter.verify_session(ORIGIN, self.repo))
        self.cli('workspace-association', 'inspect', self.d, '--workspace', self.workspace, ok=False)


if __name__ == '__main__':
    unittest.main()
