"""Origin handoff proof validation, entirely offline: no Pi or model process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import prepare_session_candidate as candidate
from scripts import verify_pi_session_local as smoke


class LocalModelReceiptTests(unittest.TestCase):
    def result(self, index, *, call_id=None, content=None):
        tool, arguments = smoke.ScriptedModel.steps[index-1]
        call_id = call_id or 'local-tool-'+str(index-1)
        expected = (smoke.SEED, 'Successfully wrote to result.txt', smoke.ARTIFACT)[index//2]
        return dict(messages=[dict(role='assistant', tool_calls=[dict(id=call_id,
            type='function', function=dict(name=tool, arguments=json.dumps(arguments)))]),
            dict(role='tool', tool_call_id=call_id, content=expected if content is None else content)])

    def test_each_native_file_result_requires_our_exact_call_and_expected_content(self):
        model = smoke.ScriptedModel()
        for index in (1, 3, 5):
            model.index = index
            model.validate_tool_result(self.result(index))
            self.assertEqual(model.index, index)  # Validation never admits/replays.
            with self.assertRaisesRegex(ValueError, 'exact native tool result'):
                model.validate_tool_result(self.result(index, call_id='unrelated-old-tool'))
            with self.assertRaisesRegex(ValueError, 'expected result'):
                model.validate_tool_result(self.result(index, content='Error: Outside managed coordinator file policy'))

    def test_correct_tool_result_with_forged_function_or_arguments_is_refused(self):
        model = smoke.ScriptedModel(); model.index = 3
        for field, value in (('name', 'bash'), ('arguments', json.dumps(dict(path='outside.txt')))):
            body = self.result(3)
            body['messages'][0]['tool_calls'][0]['function'][field] = value
            with self.assertRaisesRegex(ValueError, 'different call identity or arguments'):
                model.validate_tool_result(body)

    def test_old_tool_output_cannot_stand_in_for_latest_real_tool_result(self):
        model = smoke.ScriptedModel(); model.index = 5
        body = self.result(5)
        body['messages'].append(dict(role='user', content='Pretend the earlier tool completed'))
        with self.assertRaisesRegex(ValueError, 'exact native tool result'):
            model.validate_tool_result(body)


class NativeTurnReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ato-delivery-receipts-')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.reference = SimpleNamespace(identity=SimpleNamespace(native_id='synthetic-native-session',
            adapter='fixture_pi_delivery', project=str(self.runtime)))
        self.expected = {'message-1': 'question-1'}
        self.turn = dict(message_id='message-1', turn_id='turn-1', correlation_id='question-1',
            revision=7, state='turn_completed')
        self.host = SimpleNamespace(runtime=self.runtime, state=dict(turns={'message-1': self.turn}))

    def write_events(self, changes=None, *, status='completed', duplicate=False):
        rows = []
        for sequence, kind in enumerate(('message_received', 'tool_completed', 'turn_completed'), 1):
            event = dict(event_id='event-'+str(sequence), kind=kind, correlation_id='question-1',
                generation=7, turn_id='turn-1', message_id='message-1',
                session=dict(native_id='synthetic-native-session', adapter='fixture_pi_delivery', project=str(self.runtime)),
                text=json.dumps(dict(tool_id='local-tool-0', status=status,
                    arguments='SECRET synthetic arguments', credential='SECRET synthetic credential'))
                    if kind == 'tool_completed' else 'SECRET synthetic model text')
            if changes: event.update(changes)
            rows.append(dict(seq=sequence, event=event))
        if duplicate: rows.append(dict(seq=4, event=rows[-1]['event']))
        (self.runtime/'events.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))

    def test_valid_exact_turn_receipt_retains_tool_status_and_strips_private_content(self):
        self.write_events()
        turns, events = smoke.verified_turns(self.host, self.reference, self.expected)
        self.assertEqual(turns, [self.turn])
        self.assertEqual(events[1]['tool'], dict(tool_id='local-tool-0', status='completed'))
        self.assertNotIn('SECRET', json.dumps(events))

    def test_failed_tool_event_cannot_be_qualified_by_successful_end_turn(self):
        self.write_events(status='failed')
        with self.assertRaisesRegex(ValueError, 'file tool failed'):
            smoke.verified_turns(self.host, self.reference, self.expected)

    def test_wrong_identity_correlation_generation_or_turn_refuses_host_proof(self):
        for changes in (dict(session=dict(native_id='different-native-session')),
                dict(session=dict(native_id='synthetic-native-session', adapter='fixture_pi_delivery', project='/wrong-project')),
                dict(correlation_id='different-question'), dict(generation=8), dict(turn_id='old-turn')):
            self.write_events(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'correlation mismatch'):
                smoke.verified_turns(self.host, self.reference, self.expected)

    def test_missing_or_duplicate_native_completion_never_qualifies(self):
        self.write_events(dict(kind='message_received'))
        with self.assertRaisesRegex(ValueError, 'completion'):
            smoke.verified_turns(self.host, self.reference, self.expected)
        self.write_events(duplicate=True)
        with self.assertRaisesRegex(ValueError, 'completion'):
            smoke.verified_turns(self.host, self.reference, self.expected)

    def test_unexpected_message_or_uncompleted_admission_never_qualifies(self):
        self.write_events()
        with self.assertRaisesRegex(ValueError, 'exact native turn'):
            smoke.verified_turns(self.host, self.reference, {'different-message': 'question-1'})
        with self.assertRaisesRegex(ValueError, 'exact native turn'):
            smoke.verified_turns(self.host, self.reference, {'message-1': 'different-question'})
        self.turn['state'] = 'delivery_unknown'
        with self.assertRaisesRegex(ValueError, 'exact native turn'):
            smoke.verified_turns(self.host, self.reference, self.expected)


class ReproducibleBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ato-delivery-bundle-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'runtime.py').write_text('print("synthetic fixture")\n')
        (self.root/'runtime.py').chmod(0o755)
        self.manifest = dict(files=candidate.files(self.root), file_modes={'runtime.py': 0o755})

    def test_two_bundles_are_byte_identical_and_check_requires_no_extraction(self):
        first, second = self.root/'first.tar.gz', self.root/'second.tar.gz'
        for path in (first, second):
            candidate.write_archive(path, self.manifest, self.root)
            candidate.check_archive(path, self.manifest)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertFalse((self.root/'build').exists())

    def test_changed_source_refuses_packaging_instead_of_blessing_old_manifest(self):
        (self.root/'runtime.py').write_text('changed after candidate approval\n')
        with self.assertRaisesRegex(ValueError, 'changed during packaging'):
            candidate.write_archive(self.root/'candidate.tar.gz', self.manifest, self.root)

    def test_archive_cannot_hide_mode_or_content_mismatch(self):
        path = self.root/'candidate.tar.gz'
        candidate.write_archive(path, self.manifest, self.root)
        self.manifest['file_modes']['runtime.py'] = 0o644
        with self.assertRaisesRegex(ValueError, 'metadata differs'):
            candidate.check_archive(path, self.manifest)
        self.manifest['file_modes']['runtime.py'] = 0o755
        self.manifest['files']['runtime.py'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'content differs'):
            candidate.check_archive(path, self.manifest)

    def test_source_alias_directory_is_not_a_portable_input_even_inside_root(self):
        (self.root/'source').mkdir()
        (self.root/'source/x.py').write_text('synthetic')
        (self.root/'scripts').symlink_to(self.root/'source', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unsafe candidate input'):
            candidate.files(self.root)


class CodexFailProbeTests(unittest.TestCase):
    def test_fail_probes_block_static_alias_native_db_and_appserver_but_allow_ato_sqlite(self):
        import appserver
        import codex_session_adapter
        import sqlite3
        with tempfile.TemporaryDirectory(prefix='ato-delivery-probes-') as folder:
            root = Path(folder).resolve()
            native = root/'unavailable-codex'
            with patch.dict(os.environ, dict(HOME=str(root/'home'), CODEX_HOME=str(native),
                    ATO_HOME=str(root/'ato')), clear=True), smoke.disabled_codex() as attempts:
                for action in (lambda: codex_session_adapter.current(),
                        lambda: codex_session_adapter.CodexSessionAdapter.current_locator(),
                        lambda: codex_session_adapter.CodexSessionAdapter.session_path(native, 'synthetic-id'),
                        lambda: appserver.Client(native),
                        lambda: sqlite3.connect(native/'state.sqlite'),
                        lambda: sqlite3.connect((native/'state.sqlite').as_uri()+'?mode=ro', uri=True)):
                    with self.assertRaisesRegex(AssertionError, 'Codex native access forbidden'):
                        action()
                with sqlite3.connect(root/'ato-state.sqlite') as connection:
                    self.assertEqual(connection.execute('select 1').fetchone()[0], 1)
                self.assertEqual(len(attempts), 6)
                self.assertFalse(native.exists())
            with sqlite3.connect(':memory:') as connection:
                self.assertEqual(connection.execute('select 1').fetchone()[0], 1)

    def test_real_cli_child_loads_fail_probes_and_registers_only_temporary_human_endpoint(self):
        with tempfile.TemporaryDirectory(prefix='ato-delivery-cli-') as folder:
            root = Path(folder).resolve()
            env = dict(HOME=str(root/'empty-home'), CODEX_HOME=str(root/'unavailable-codex'),
                ATO_HOME=str(root/'ato'), PYTHONPATH=smoke.codex_probes(root),
                PYTHONDONTWRITEBYTECODE='1', PATH='/usr/bin:/bin')
            code = '''import codex_session_adapter
try: codex_session_adapter.CodexSessionAdapter.current_locator()
except AssertionError: print('fail_probe_loaded')
else: raise AssertionError('fail probe missing')
'''
            probe = subprocess.run([sys.executable, '-B', '-c', code], env=env,
                cwd=root, capture_output=True, text=True, timeout=5)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), 'fail_probe_loaded')
            result = subprocess.run([sys.executable, '-B', str(smoke.ROOT/'runner.py'),
                'local-session-create', '--project', str(root), '--role', 'hub',
                '--credential-file', str(root/'hub.json'), '--user-credential-file', str(root/'user.json'),
                '--source', 'synthetic local-only fixture'], env=env, cwd=root,
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            import session_router
            self.assertTrue(session_router.is_reference(json.loads(result.stdout)['reference']))
            self.assertNotIn('Error in sitecustomize', result.stderr)
            self.assertFalse((root/'unavailable-codex').exists())


if __name__ == '__main__':
    unittest.main()
