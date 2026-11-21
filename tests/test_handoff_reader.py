"""Custom installation and compact reader commands, without any live queue."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import coordination
import runner
import test_compact_handoff as fixtures

ROOT = Path(__file__).resolve().parents[1]


class HandoffReaderTests(unittest.TestCase):
    setUp = fixtures.CompactHandoffTests.setUp
    tearDown = fixtures.CompactHandoffTests.tearDown
    transport = fixtures.CompactHandoffTests.transport
    prepare = fixtures.CompactHandoffTests.prepare
    material = fixtures.CompactHandoffTests.material

    def test_default_and_invalid_configuration_fall_back_without_path_lookup(self):
        home = self.repo/'empty-home'
        missing = self.repo/'missing/ato-read-handoff'
        non_executable = self.repo/'ato-read-handoff'
        non_executable.write_text('not executable')
        non_executable.chmod(0o600)
        for configured in ('', str(missing), str(non_executable), str(self.repo), 'relative/ato-read-handoff'):
            with self.subTest(configured=configured), patch('coordination.Path.home', return_value=home), \
                    patch.dict(os.environ, CTASK_HANDOFF_READER=configured, PATH=str(self.repo)):
                self.assertEqual(shlex.split(coordination.reader_command()),
                                 [str(home/'.local/bin/ato-read-handoff')])

    def test_explicit_reader_is_shell_quoted_including_nonstandard_filename(self):
        directory = self.repo/"bin ' $(touch injected)"
        directory.mkdir()
        for name in ('ato-read-handoff', 'custom-reader'):
            reader = directory/name
            reader.write_text('#!/bin/sh\nprintf \'%s\\n\' "$1"\n')
            reader.chmod(0o755)
            with self.subTest(name=name), patch.dict(os.environ, CTASK_HANDOFF_READER=str(reader)):
                command = coordination.reader_command()
                result = subprocess.run(['/bin/sh', '-c', command+' fixture-id'],
                    cwd=self.repo, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'fixture-id\n')
                self.assertIn('ato-read-handoff', command)
                self.assertFalse((self.repo/'injected').exists())

    def test_default_notice_is_bounded_in_utf8_bytes(self):
        self.prepare()
        with patch.dict(os.environ, CTASK_HANDOFF_READER=''), \
                patch('coordination.Path.home', return_value=self.repo/'home'):
            notice = self.material()
        self.assertLess(len(notice.encode('utf-8')), 260)
        self.assertIn('[ctask-host-handoff id='+self.identifier+']', notice)
        self.assertIn('ato-read-handoff '+self.identifier, notice)

    def test_unrepresentably_long_reader_path_is_rejected_without_truncation(self):
        self.prepare()
        folder = self.repo/('长'*70)
        folder.mkdir()
        reader = folder/'ato-read-handoff'
        reader.write_text('#!/bin/sh\nexit 0\n')
        reader.chmod(0o755)
        with patch.dict(os.environ, CTASK_HANDOFF_READER=str(reader)):
            with self.assertRaisesRegex(ValueError, 'compact notice byte limit'):
                self.material()
        self.assertFalse((coordination.payload_directory()/(self.identifier+'.json')).exists())

    def test_installed_ctask_and_notify_queue_custom_reader_and_reader_validates(self):
        folder = (self.repo/'bin with spaces').resolve()
        installed = subprocess.run([sys.executable, str(ROOT/'scripts/install.py'),
            '--helpers', '--bin-dir', str(folder)], capture_output=True, text=True, timeout=10)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        queued = self.repo/'queued.jsonl'
        queue = self.repo/'synthetic-queue'
        queue.write_text('#!'+sys.executable+'\nimport json,sys\n'
            'from pathlib import Path\n'
            'with Path(__file__).with_name("queued.jsonl").open("a") as f:\n'
            '    f.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'thread=sys.argv[sys.argv.index("--thread")+1]\n'
            'print("Queued message "+thread+" for thread "+thread+".")\n')
        queue.chmod(0o755)
        env = dict(os.environ, CTASK_QUEUE_BIN=str(queue), CTASK_CODEX_BIN='/unavailable', CTASK_HANDOFF_READER='')
        override = self.repo/'override bin/ato-read-handoff'
        override.parent.mkdir()
        override.symlink_to(folder/'ato-read-handoff')
        for launcher, configured in (('ctask', ''), ('ctask-notify', ''),
                                     ('ctask', str(override)), ('ctask-notify', str(override))):
            with self.subTest(launcher=launcher, configured=configured):
                self.prepare()
                before = (self.d/'state.json').read_bytes()
                result = subprocess.run([str(folder/launcher), 'control', 'tick'],
                    env=dict(env, CTASK_HANDOFF_READER=configured), capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                messages = [json.loads(line)[-1] for line in queued.read_text().splitlines()]
                notices = [message for message in messages if '[ctask-host-handoff id='+self.identifier+']' in message]
                self.assertEqual(len(notices), 1)
                notice = notices[0]
                self.assertLess(len(notice.encode('utf-8')), 260)
                self.assertEqual(shlex.split(notice.splitlines()[1]),
                                 [configured or str(folder/'ato-read-handoff'), self.identifier])
                read = subprocess.run(['/bin/sh', '-c', notice.splitlines()[1]], env=env,
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(read.returncode, 0, read.stderr)
                self.assertTrue(json.loads(read.stdout)['valid'])
                self.assertEqual((self.d/'state.json').read_bytes(), before)
        self.assertFalse((coordination.payload_directory().parent/'process.json').exists())
