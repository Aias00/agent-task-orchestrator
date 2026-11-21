"""Public CLI recovery bindings with isolated, synthetic observed sessions."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT/'tests/fixtures/fixture_ctask.py'
THREAD = '11111111-1111-4111-8111-000000000001'


class WorkflowCreateCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-create-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = dict(HOME=str(self.root/'home'), ATO_HOME=str(self.root/'ato'),
                        CODEX_HOME=str(self.root/'native'), PATH=os.defpath,
                        CTASK_CODEX_BIN='/unavailable', CTASK_QUEUE_BIN='/unavailable')
        self.plan = self.repo/'contract.json'
        self.plan.write_text(json.dumps(dict(objective='Synthetic CLI recovery binding',
            scope='temporary repo only', authorization='synthetic fixture authority', memory=False,
            acceptance=[dict(id='file', description='host check', checks=[[sys.executable, '-c', 'assert True']])])))

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)],
            cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode == 0, ok, result.stderr+result.stdout)
        return result

    def reference(self):
        return self.cli('session-register', '--kind', 'fixture_neutral_e2e', '--identity',
            'origin:opaque', '--project', self.repo, '--source', 'synthetic verified origin').stdout.strip()

    def create(self, *args, **kwargs):
        return self.cli('workflow-create', '--repo', self.repo, '--plan', self.plan, *args, **kwargs)

    def assert_supervised(self, directory, target):
        origin = json.loads((directory/'origin.json').read_text())
        ledger = json.loads((directory/'recovery.json').read_text())
        self.assertEqual(origin['codex_home'], self.env['CODEX_HOME'])
        self.assertEqual(ledger['codex_home'], self.env['CODEX_HOME'])
        self.assertEqual(ledger['thread_id'], target)
        self.assertEqual(ledger['source'], 'synthetic explicit supervision grant')
        self.assertEqual(ledger['requests'], [])
        self.assertEqual(ledger['contract_hash'], json.loads((directory/'state.json').read_text())['contract_hash'])
        summary = json.loads(self.cli('status', directory).stdout)['recovery']
        self.assertTrue(summary['enabled'])
        self.assertEqual(summary['status'], 'watching')
        self.assertEqual(summary['requests'], 0)
        self.assertFalse(summary['supervisor_alive'])
        self.assertFalse((directory/'supervisor.json').exists())
        return origin, ledger

    def test_verified_origin_ref_auto_recover_succeeds_through_public_cli(self):
        reference = self.reference()
        directory = Path(self.create('--origin-ref', reference, '--auto-recover',
            '--recovery-source', 'synthetic explicit supervision grant').stdout.strip())
        origin, ledger = self.assert_supervised(directory, reference)
        self.assertEqual(origin['origin_session_ref'], reference)
        self.assertIsNone(origin['origin_thread_id'])
        self.assertIsNone(ledger['rollout'])

    def test_legacy_origin_thread_auto_recover_still_succeeds(self):
        home = Path(self.env['CODEX_HOME'])
        home.mkdir()
        rollout = home/'fixture.jsonl'
        rollout.write_text(json.dumps(dict(type='session_meta', payload=dict(id=THREAD)))+'\n')
        with sqlite3.connect(home/'state_5.sqlite') as con:
            con.execute('create table threads (id text, rollout_path text)')
            con.execute('insert into threads values (?, ?)', (THREAD, str(rollout)))
        directory = Path(self.create('--origin-thread', THREAD, '--auto-recover',
            '--recovery-source', 'synthetic explicit supervision grant').stdout.strip())
        origin, ledger = self.assert_supervised(directory, THREAD)
        self.assertEqual(origin['origin_thread_id'], THREAD)
        self.assertEqual(ledger['rollout'], str(rollout))

    def test_missing_origin_or_source_is_rejected_before_creating_workflow(self):
        reference = self.reference()
        cases = [('--recovery-source', 'synthetic grant'), ('--origin-ref', reference),
                 ('--origin-thread', THREAD), (), ('--origin-ref', reference, '--recovery-source', '')]
        for args in cases:
            with self.subTest(args=args):
                result = self.create('--auto-recover', *args, ok=False)
                for option in ('--origin-thread', '--origin-ref', '--recovery-source'):
                    self.assertIn(option, result.stderr)
                self.assertFalse((self.repo/'.codex/tasks/workflows').exists())

    def test_unverified_reference_is_rejected_before_creating_workflow(self):
        self.create('--origin-ref', 'agentref-'+'a'*32, '--auto-recover',
                    '--recovery-source', 'synthetic grant', ok=False)
        self.assertFalse((self.repo/'.codex/tasks/workflows').exists())

    def test_passive_cli_reference_cannot_claim_session_observation(self):
        reference = json.loads(self.cli('local-session-create', '--project', self.repo, '--role', 'origin',
            '--source', 'synthetic passive origin', '--credential-file', self.root/'actor.json').stdout)['reference']
        self.create('--origin-ref', reference, '--auto-recover', '--recovery-source', 'synthetic grant', ok=False)
        self.assertEqual(list((self.repo/'.codex/tasks/workflows').glob('*/recovery.json')), [])
