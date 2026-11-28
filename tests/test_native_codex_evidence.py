"""Provider-free negative controls for the opt-in native evidence gate."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_codex_mcp.py'
spec = importlib.util.spec_from_file_location('native_evidence', SCRIPT)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {'ATO_HOME': str(self.root / 'ato'),
                                                   'CODEX_HOME': str(self.root / 'codex')})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def events(self, events):
        path = self.root / 'events.jsonl'
        path.write_text(''.join(json.dumps(e) + '\n' for e in events))
        return path

    def test_prose_is_not_native_tool_evidence(self):
        path = self.events([{'type': 'item.completed', 'item': {'type': 'agent_message',
                              'text': 'All MCP calls succeeded'}}, {'type': 'turn.completed'}])
        self.assertEqual(native.native_calls(path), [])

    def test_approval_failure_is_not_success(self):
        path = self.events([{'type': 'item.completed', 'item': {'type': 'mcp_tool_call',
                              'status': 'failed', 'error': {'message': 'approval required'}}},
                            {'type': 'turn.completed'}])
        with self.assertRaisesRegex(RuntimeError, 'MCP call failed'):
            native.native_calls(path)

    def test_business_denial_is_not_success(self):
        path = self.events([{'type': 'item.completed', 'item': {'type': 'mcp_tool_call',
                              'status': 'completed', 'result': {'structured_content': {'ok': False}}}},
                            {'type': 'turn.completed'}])
        with self.assertRaisesRegex(RuntimeError, 'business call failed'):
            native.native_calls(path)

    def test_incomplete_turn_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'did not complete'):
            native.native_calls(self.events([{'type': 'turn.started'}]))

    def test_missing_and_stale_evidence_fail_without_client_invocation(self):
        with self.assertRaises(FileNotFoundError):
            native.verify(self.root)
        native.save(self.root / 'manifest.json', {'baseline': native.BASELINE, 'source': {}})
        with patch.object(native, 'client_identity', side_effect=AssertionError('must not invoke client')):
            with self.assertRaisesRegex(RuntimeError, 'Stale source'):
                native.verify(self.root)

    def test_unhashed_artifact_fails(self):
        manifest = {'baseline': native.BASELINE, 'source': native.source_identity(),
                    'client': {'fixture': True}, 'artifacts': {},
                    'sessions': [{'name': n} for n in ('stdio-submit', 'stdio-final', 'http-replay', 'http-lifecycle')]}
        native.save(self.root / 'manifest.json', manifest)
        with patch.object(native.shutil, 'which', return_value='/synthetic/codex'), \
             patch.object(native, 'client_identity', return_value={'fixture': True}):
            with self.assertRaisesRegex(RuntimeError, 'Unhashed/missing'):
                native.verify(self.root)

    def test_raw_event_hash_tamper_fails(self):
        raw = self.root / 'raw.jsonl'
        raw.write_text('original')
        manifest = {'baseline': native.BASELINE, 'source': native.source_identity(),
                    'client': {'fixture': True}, 'artifacts': {'raw.jsonl': native.digest(raw.read_bytes())}}
        native.save(self.root / 'manifest.json', manifest)
        raw.write_text('modified')
        with patch.object(native.shutil, 'which', return_value='/synthetic/codex'), \
             patch.object(native, 'client_identity', return_value={'fixture': True}):
            with self.assertRaisesRegex(RuntimeError, 'Missing/changed artifact'):
                native.verify(self.root)

    def test_evidence_store_cannot_write(self):
        from ato.store import Store
        database = self.root / 'ato.db'
        Store(database)
        before = native.digest(database.read_bytes())
        reader = native.EvidenceStore(database)
        self.assertEqual(reader.list_tasks(), [])
        self.assertEqual(native.digest(database.read_bytes()), before)
        import sqlite3
        with reader._connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM tasks')
