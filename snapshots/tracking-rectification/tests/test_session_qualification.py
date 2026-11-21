"""Delivery integrity and final integration faults; no actual Pi/provider calls."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ato_config
import control
import interaction
import recovery
import runner
import transport
from scripts import prepare_session_candidate as candidate
from scripts import verify_pi_session_local as smoke


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-final-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, dict(HOME=str(self.root/'empty-home'),
            ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'native-one')), clear=True)
        self.env.start(); self.addCleanup(self.env.stop)

    def test_legacy_accounts_in_one_ato_root_cannot_share_policy_hub_or_mailbox(self):
        first = interaction.directory()
        runner.save(first/'hub.json', dict(thread_id='synthetic-one'))
        runner.save(first/'time-policy.json', dict(mode='advisory'))
        bus = transport.managed_bus()
        endpoint = transport.host_endpoint(bus, kind='fixture', native_id='same-synthetic-id',
            project=self.root, source='synthetic first account')
        with patch.dict(os.environ, CODEX_HOME=str(self.root/'native-two')):
            second = interaction.directory()
            self.assertNotEqual(first, second)
            self.assertIsNone(interaction.config())
            self.assertFalse((second/'time-policy.json').exists())
            other = transport.host_endpoint(transport.managed_bus(), kind='fixture',
                native_id='same-synthetic-id', project=self.root, source='synthetic second account')
            with self.assertRaises(ValueError):
                transport.managed_bus().authenticate(endpoint)
            self.assertNotEqual(endpoint.token, other.token)
        self.assertEqual(interaction.config()['thread_id'], 'synthetic-one')
        self.assertEqual(transport.managed_bus().authenticate(endpoint)['session_id'], 'same-synthetic-id')
        self.assertEqual(ato_config.ato_home(), self.root/'ato')

    def test_fallback_layout_and_neutral_only_context_need_no_codex_installation(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ['HOME'] = str(self.root/'empty')
            os.environ['CODEX_HOME'] = str(self.root/'legacy')
            self.assertEqual(interaction.directory(), self.root/'legacy/ctask-interaction')
            os.environ['ATO_HOME'] = str(self.root/'neutral')
            os.environ.pop('CODEX_HOME')
            self.assertEqual(interaction.directory(), self.root/'neutral/ctask-interaction')
            self.assertFalse((self.root/'empty/.codex').exists())

    def state(self, status='completed'):
        directory = self.root/'workflow'; directory.mkdir(exist_ok=True)
        return directory, dict(repo=str(self.root), contract_hash='synthetic-contract', status=status,
            execution_revision=3, owner_pid=None, active_process=None, active_run=None,
            planning_attempts=2, tasks={'one':dict(attempt_sequence=4)})

    def test_explicit_foreground_revalidation_preserves_history_but_recovery_stays_terminal(self):
        d, state = self.state()
        before = json.dumps(state, sort_keys=True)
        with self.assertRaises(ValueError):
            with recovery.execution_owner(d, state): pass
        with recovery.locked(self.root/'repo.lock'):
            with recovery.execution_owner(d, state, explicit_run=True):
                lease = transport.managed_bus().execution_lease(recovery.execution_resource(self.root))
                self.assertEqual(lease['state'], 'active')
                self.assertEqual(json.loads(lease['generation']), [None, 3])
        self.assertEqual(before, json.dumps(state, sort_keys=True))
        self.assertEqual(transport.managed_bus().execution_lease(recovery.execution_resource(self.root))['state'], 'released')

    def test_explicit_foreground_cannot_clear_stop_closure_or_grant_host_recovery(self):
        d, state = self.state('stopped')
        for marker in ('STOP', 'user-closure.json'):
            (d/marker).write_text('synthetic closure')
            with self.assertRaises(ValueError):
                with recovery.execution_owner(d, state, explicit_run=True): pass
            self.assertTrue((d/marker).exists())
            (d/marker).unlink()
        for role in ('origin', 'session_host'):
            with self.assertRaises(ValueError):
                with recovery.execution_owner(d, state, role=role, explicit_run=True): pass

    def test_unknown_origin_failure_notice_is_once_only_and_never_proves_idle(self):
        import test_task_disposition as old
        fixture = old.TaskDispositionTests()
        fixture.setUp()
        try:
            fixture.configure()
            with patch('session_router.activity', return_value='unknown'):
                action = fixture.observe()[0]
                self.assertEqual(action['handler_status'], 'activity_unknown')
                self.assertIn('未知', action['question'])
                self.assertEqual(recovery.handler_status(fixture.d,
                    runner.read(fixture.d/'state.json'), action['origin']), 'activity_unknown')
                with patch.dict(os.environ, CODEX_THREAD_ID=old.HUB):
                    control.record(action['id'], state='presented', note='synthetic user presentation')
                self.assertEqual(len(fixture.observe()), 1)
                import failure_attention
                self.assertFalse(failure_attention.pending(control.rows(True)[0]))
                runner.save(fixture.d/'recovery.json', dict(requests=[dict(state='delivery_unknown')]))
                self.assertFalse(failure_attention.current(action))
        finally:
            fixture.tearDown()

    def skeleton(self):
        repo = self.root/'source'; repo.mkdir()
        for name, text in [('runtime.py', 'pass\n'), ('tests/test_old.py', '# protected\n'),
                ('docs/session-decoupling/migration.md', '# candidate\n'),
                ('scripts/example.py', '# source\n'), ('README.md', '# readme\n')]:
            path=repo/name; path.parent.mkdir(exist_ok=True,parents=True);path.write_text(text)
        base = self.root/'baseline.json'
        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        base.write_text(json.dumps(dict(actual_runtime_files={'runtime.py':digest(repo/'runtime.py')},
            inherited_tests={'tests/test_old.py':digest(repo/'tests/test_old.py')})))
        folder = repo/'build/session-decoupling';folder.mkdir(parents=True)
        seed = folder/'stage-start.json'
        seed.write_text(json.dumps(dict(git_head='synthetic-seeded-head',files=candidate.files(repo))))
        (folder/'seeded-diff.patch').write_text('synthetic prior dirty diff\n')
        return repo,base,seed

    def test_candidate_detects_protected_test_change_instead_of_packaging_it(self):
        repo,base,seed=self.skeleton()
        candidate.build(repo,base,seed)
        (repo/'tests/test_old.py').write_text('# weakened\n')
        with self.assertRaisesRegex(ValueError, 'protected inherited'):
            candidate.build(repo,base,seed)

    def test_candidate_inventory_includes_new_tests_docs_and_source_delta(self):
        repo,base,seed=self.skeleton()
        (repo/'tests/test_new.py').write_text('# meaningful new behavior\n')
        (repo/'runtime.py').write_text('x=1\n')
        manifest=candidate.build(repo,base,seed)
        self.assertFalse(manifest['runtime_activated'])
        self.assertIn('tests/test_new.py',manifest['files'])
        self.assertIn('runtime.py',manifest['actual_live_to_candidate_delta'])
        self.assertIsNone(manifest['qualification_stage_delta']['tests/test_new.py']['seeded_sha256'])
        self.assertIn('docs/session-decoupling/migration.md',manifest['files'])
        self.assertEqual(manifest['qualification']['cloud_model'],'not_tested')

    def test_candidate_rejects_symlinked_source_and_omits_private_state_dependencies(self):
        repo,base,seed=self.skeleton()
        for name in ('.codex/tasks/state.json','private/auth.json','integrations/pi-acp/node_modules/native.py'):
            p=repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic private data')
        manifest=candidate.build(repo,base,seed)
        self.assertFalse(any('node_modules' in p or '.codex' in p or p.startswith('private/') for p in manifest['files']))
        (repo/'docs/escape.md').symlink_to(base)
        with self.assertRaisesRegex(ValueError,'unsafe'):
            candidate.build(repo,base,seed)

    def body(self, tool=None):
        return dict(model=smoke.MODEL,stream=True,messages=[dict(role='user',content='synthetic')]
            +([dict(role='tool',content='synthetic actual tool result')] if tool is None else []),
            tools=[dict(type='function',function=dict(name=tool))] if tool else [])

    def test_scripted_model_real_tool_protocol_is_bounded_and_never_replays(self):
        model=smoke.ScriptedModel()
        for tool,_ in model.steps:
            output=model.response(self.body(tool))
            self.assertIn('data: [DONE]',output)
            self.assertIn('"finish_reason": "tool_calls"' if tool else '"finish_reason": "stop"',output)
        self.assertEqual(len(model.receipts),6)
        with self.assertRaises(ValueError):model.response(self.body('read'))
        self.assertEqual(model.index,6)
        self.assertNotIn('content',json.dumps(model.receipts))

    def test_scripted_model_refuses_unexpected_model_and_fabricated_tool_results(self):
        model=smoke.ScriptedModel()
        bad=self.body('read');bad['model']='external-provider'
        with self.assertRaises(ValueError):model.response(bad)
        self.assertEqual(model.index,0)
        model.response(self.body('read'))
        with self.assertRaises(ValueError):model.response(self.body('write'))
        self.assertEqual(model.index,1)

    def test_retained_native_receipts_strip_model_text_arguments_and_credentials(self):
        from types import SimpleNamespace
        host=SimpleNamespace(runtime=self.root)
        event=dict(event_id='id',kind='tool_completed',correlation_id='canonical',generation=3,
            turn_id='native-turn',message_id='message',session={'native_id':'native'},
            text='synthetic SECRET model text and arguments',source='private token')
        (self.root/'events.jsonl').write_text(json.dumps(dict(seq=1,event=event))+'\n')
        records=smoke.safe_events(host)
        self.assertEqual(records[0]['turn_id'],'native-turn')
        self.assertNotIn('SECRET',json.dumps(records))
        self.assertNotIn('source',records[0])

    def test_smoke_requires_explicit_local_only_before_any_native_activity(self):
        output=self.root/'proof.json'
        result=subprocess.run([sys.executable,str(Path(smoke.__file__)),'--output',str(output)],
            capture_output=True,text=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('--local-only',result.stderr)
        self.assertFalse(output.exists())


class StaleFollowupTests(unittest.TestCase):
    import test_interaction as fixtures
    setUp=fixtures.InteractionTests.setUp
    tearDown=fixtures.InteractionTests.tearDown
    transport=fixtures.InteractionTests.transport

    def test_stale_controller_followup_is_recorded_without_rebinding_or_native_send(self):
        with patch('interaction.subprocess.run',side_effect=self.transport):
            q=interaction.ask(self.d,'Synthetic original question?')
        path=interaction.question_path(q['id']);before=path.read_bytes()
        state=runner.read(self.d/'state.json');state['execution_revision']=1;runner.save(self.d/'state.json',state)
        key='followup:'+q['id']+':0'
        with patch('control.subprocess.run',side_effect=AssertionError('stale native send forbidden')):
            record=control.send(key,q['hub_thread'],'stale synthetic message',workflow=str(self.d))
            self.assertEqual(record['state'],'recorded_only')
            self.assertEqual(control.send(key,q['hub_thread'],'duplicate',workflow=str(self.d)),record)
        self.assertEqual(path.read_bytes(),before)
        with self.assertRaises(ValueError):interaction.validate_question(runner.read(path))


if __name__=='__main__':unittest.main()
