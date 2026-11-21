"""Compact failure integration with the public, configurable handoff reader."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import ato_config
import control
import coordination
import failure_notice
import test_compact_failure as fixtures
from test_interaction import HUB

ROOT = Path(__file__).resolve().parents[1]


class CompactFailureReaderTests(unittest.TestCase):
    setUp = fixtures.CompactFailureTests.setUp
    tearDown = fixtures.CompactFailureTests.tearDown
    transport = fixtures.CompactFailureTests.transport
    prepare = fixtures.CompactFailureTests.prepare
    send = fixtures.CompactFailureTests.send

    def test_controller_calls_public_reader_resolver_and_direct_api_shares_it(self):
        self.assertIs(coordination.reader_command, ato_config.reader_command)
        self.assertIs(failure_notice.default_reader_command, coordination.reader_command)
        self.prepare()
        with patch('coordination.reader_command', return_value='/trusted/bin/ato-read-handoff') as resolver:
            result, sent = self.send()
        resolver.assert_called_once_with()
        self.assertEqual(result['state'], 'queued')
        self.assertIn('/trusted/bin/ato-read-handoff '+self.action['id'], sent.call_args.args[0][-1])

    def test_custom_install_with_spaces_produces_executable_reader_command(self):
        self.prepare()
        reader = (self.repo/'bin with spaces/ato-read-handoff').resolve()
        installed = subprocess.run([sys.executable, str(ROOT/'scripts/install.py'), '--helpers',
            '--bin-dir', str(reader.parent)], capture_output=True, text=True, timeout=10)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        with patch.dict(os.environ, CTASK_HANDOFF_READER=str(reader)):
            result, sent = self.send()
        notice = sent.call_args.args[0][-1]
        command = notice.splitlines()[1]
        self.assertEqual(shlex.split(command), [str(reader), self.action['id']])
        self.assertLess(len(notice.encode('utf-8')), 300)
        before = (self.d/'state.json').read_bytes()
        read = subprocess.run(['/bin/sh', '-c', command],
            env=dict(os.environ, CODEX_THREAD_ID=HUB), capture_output=True, text=True, timeout=10)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertTrue(json.loads(read.stdout)['valid'])
        self.assertEqual((self.d/'state.json').read_bytes(), before)

    def test_direct_prepare_uses_same_default_fallback(self):
        self.prepare()
        with patch.dict(os.environ, CTASK_HANDOFF_READER=''), \
                patch('coordination.Path.home', return_value=self.repo/'home'):
            command = coordination.reader_command()
            notice = failure_notice.prepare(self.key, HUB, str(self.d), 'private fixture')
        self.assertEqual(notice.splitlines()[1], command+' '+self.action['id'])
        self.assertLess(len(notice.encode('utf-8')), 300)

    def test_overlong_reader_path_cannot_queue_a_truncated_notice(self):
        self.prepare()
        reader = self.repo/('长'*70)/'ato-read-handoff'
        reader.parent.mkdir()
        reader.write_text('#!/bin/sh\nexit 0\n')
        reader.chmod(0o755)
        with patch.dict(os.environ, CTASK_HANDOFF_READER=str(reader)), \
                patch('control.subprocess.run') as sent:
            with self.assertRaisesRegex(ValueError, 'compact notice bound'):
                control.send(self.key, HUB, 'private fixture', workflow=str(self.d))
        sent.assert_not_called()
        with control.database() as con:
            self.assertIsNone(con.execute('select body from outbox where key=?', (self.key,)).fetchone())

    def test_unknown_failure_delivery_is_never_replayed(self):
        self.prepare()
        before = (self.d/'state.json').read_bytes()
        with patch('control.subprocess.run', side_effect=subprocess.TimeoutExpired('synthetic queue', 1)) as sent:
            first = control.send(self.key, HUB, 'private fixture', workflow=str(self.d))
            second = control.send(self.key, HUB, 'private fixture', workflow=str(self.d))
        self.assertEqual(first['state'], 'delivery_unknown')
        self.assertEqual(second['state'], 'delivery_unknown')
        self.assertEqual(sent.call_count, 1)
        self.assertEqual((self.d/'state.json').read_bytes(), before)
