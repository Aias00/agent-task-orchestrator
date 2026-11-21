"""Offline migration/rollback rehearsals, never actual deployment or hub data."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import control
import interaction
import runner
from message_bus import MessageBus

spec = importlib.util.spec_from_file_location('upgrade_runtime', ROOT/'scripts/upgrade_runtime.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ctask upgrade ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base/'old runtime'; self.runtime.mkdir()
        (self.runtime/'runner.py').write_text('# fixture old deployment\n')
        self.tasks = self.runtime/'.codex/tasks'; self.tasks.mkdir(parents=True)
        self.workflow = self.tasks/'workflows/legacy'; self.workflow.mkdir(parents=True)
        self.state = dict(kind='workflow', status='stopped', elapsed_seconds=125.5,
                          failures=['old failure'], generation=3, tasks={}, acceptance={'old': 'failed'})
        records = dict(state=self.state, origin=dict(origin_thread_id='legacy-codex-id',
            source='Original fixture user authorization'), recovery=dict(attempts=2,
            max_attempts=2, deadline=1234, requests=[{'state': 'expired'}]),
            contract=dict(objective='Original complete goal', authorization='Existing authorization',
                          limits={'runtime_seconds': 600}, acceptance=[{'id': 'old'}]))
        for name, record in records.items():
            (self.workflow/(name+'.json')).write_text(json.dumps(record)+'\n')
        (self.workflow/'STOP').touch()
        self.home = self.base/'home'; self.interaction = self.home/'ctask-interaction'
        self.interaction.mkdir(parents=True)
        (self.interaction/'hub.json').write_text(json.dumps(dict(thread_id='fixture-hub',
            source='Actual fixture user choice', codex_home=str(self.home))))
        (self.interaction/'questions').mkdir()
        (self.interaction/'questions/old.json').write_text(json.dumps(dict(id='old',
            state='answered', answer='Actual fixture user decision', answer_source='Fixture user turn',
            answer_delivery={'state': 'delivery_unknown'}, origin_thread='legacy-codex-id')))
        # The exact old controller schema/claims, with no mailbox schema/tables.
        directory = self.interaction/'control'; directory.mkdir()
        with sqlite3.connect(directory/'state.sqlite') as con:
            con.executescript('''create table actions(id text primary key, body text not null);
                create table events(seq integer primary key autoincrement, key text unique not null,
                    at real not null, action_id text, kind text not null, body text not null);
                create table outbox(key text primary key, body text not null);
                create table workflows(path text primary key, body text not null);''')
            con.execute('insert into actions values (?,?)', ('old', json.dumps(dict(id='old', state='paused',
                reminder_count=3, presented_at=12, answer_source='Fixture user turn'))))
            con.execute('insert into outbox values (?,?)', ('old-send', json.dumps(dict(state='dispatching',
                target='fixture-hub', at=42))))
            con.execute('insert into events(key,at,action_id,kind,body) values(?,?,?,?,?)',
                        ('old-event', 42, 'old', 'failed', '{"reason":"original failure"}'))

    def stage(self):
        return upgrade.stage(self.runtime, self.base/'staged', [self.tasks, self.interaction], True)

    def test_cli_dry_run_is_nonmutating_including_legacy_records(self):
        before = tree(self.base)
        result = subprocess.run([sys.executable, str(ROOT/'scripts/upgrade_runtime.py'),
            '--dry-run', '--runtime', str(self.runtime)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['activation_performed'])
        self.assertEqual(tree(self.base), before)
        self.assertIn('message_bus.py', report['source_sha256'])

    def test_staged_runtime_runs_and_old_runtime_is_untouched(self):
        before_runtime, before_data = tree(self.runtime), tree(self.interaction)
        report = self.stage()
        self.assertEqual(tree(self.runtime), before_runtime)
        self.assertEqual(tree(self.interaction), before_data)
        self.assertFalse(report['activation_performed'])
        code = self.base/'staged/runtime'
        result = subprocess.run([sys.executable, str(code/'runner.py'), '--help'],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mailbox-register', result.stdout)
        self.assertTrue((code/'skills/codex-long-task/references/controller.md').exists())
        installed = subprocess.run([sys.executable, str(code/'scripts/install.py'),
            '--bin-dir', str(self.base/'isolated bin'), '--skills-dir', str(self.base/'isolated skills')],
            text=True, capture_output=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        launcher = json.loads(installed.stdout)['launcher']
        help_result = subprocess.run([launcher, '--help'], text=True, capture_output=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('mailbox-pump', help_result.stdout)
        self.assertEqual(tree(self.runtime), before_runtime)
        self.assertEqual(tree(self.interaction), before_data)
        for row in report['snapshots']:
            self.assertEqual(tree(self.base/'staged'/row['path']), row['sha256'])
        self.assertEqual((self.base/'staged/upgrade-manifest.json').stat().st_mode & 0o777, 0o600)

    def test_restore_preserves_legacy_budgets_history_stop_and_unknown_claims_on_restart(self):
        self.stage()
        restored = self.base/'restored'
        upgrade.restore(self.base/'staged', restored, True)
        # Restore makes independent copies and does not rewrite absolute identity bindings.
        self.assertEqual(tree(restored/'0'), tree(self.tasks))
        self.assertEqual(tree(restored/'1'), tree(self.interaction))
        state = runner.read(restored/'0/workflows/legacy/state.json')
        self.assertEqual(state, self.state)
        self.assertTrue((restored/'0/workflows/legacy/STOP').exists())
        self.assertEqual(runner.read(restored/'0/workflows/legacy/recovery.json')['attempts'], 2)
        self.assertEqual(runner.read(restored/'0/workflows/legacy/contract.json')['limits']['runtime_seconds'], 600)
        # Open new source against a copy of the old controller; it must not re-send its outbox.
        for _ in range(2):
            with patch('control.directory', return_value=restored/'1/control'), \
                 patch('interaction.home', return_value=self.home), \
                 patch('control.mailbox.send', side_effect=AssertionError('must not redispatch')):
                rows = control.rows(True)
                self.assertEqual(rows[0]['reminder_count'], 3)
                self.assertEqual(rows[0]['state'], 'paused')
                self.assertEqual(control.send('old-send', 'fixture-hub', 'must not be sent')['state'], 'dispatching')
        with sqlite3.connect(restored/'1/control/state.sqlite') as con:
            self.assertEqual(con.execute('select key,kind,body from events').fetchall(),
                             [('old-event', 'failed', '{"reason":"original failure"}')])
        self.assertEqual(interaction.public(runner.read(restored/'1/questions/old.json'))['answer_source'],
                         'Fixture user turn')
        # Additive mailbox creation/restart does not import old questions or reset legacy files.
        before = tree(self.tasks)
        for _ in range(2):
            b = MessageBus(restored/'1/mailbox/messages.sqlite')
            self.assertEqual(b.events(), [])
        self.assertEqual(tree(self.tasks), before)
        self.assertEqual(runner.read(restored/'0/workflows/legacy/state.json'), self.state)
        self.assertEqual(runner.read(restored/'1/hub.json')['thread_id'], 'fixture-hub')

    def test_snapshot_tampering_and_path_traversal_fail_before_restore(self):
        self.stage()
        snapshot = self.base/'staged/state/0/workflows/legacy/state.json'
        snapshot.write_text('{}')
        destination = self.base/'bad-restore'
        with self.assertRaisesRegex(ValueError, 'integrity'):
            upgrade.restore(self.base/'staged', destination, True)
        self.assertFalse(destination.exists())
        manifest_path = self.base/'staged/upgrade-manifest.json'
        manifest = json.loads(manifest_path.read_text()); manifest['snapshots'][0]['path'] = '../escape'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'location'):
            upgrade.restore(self.base/'staged', destination, True)

    def test_existing_destinations_offline_and_state_boundaries_are_enforced(self):
        before = tree(self.base)
        with self.assertRaisesRegex(ValueError, 'offline'):
            upgrade.stage(self.runtime, self.base/'staged', [self.tasks], False)
        with self.assertRaisesRegex(ValueError, 'state roots'):
            upgrade.stage(self.runtime, self.base/'staged', [self.home], True)
        with self.assertRaisesRegex(ValueError, 'separate'):
            upgrade.stage(self.runtime, self.runtime/'staged', [], True)
        with self.assertRaisesRegex(ValueError, 'exist'):
            upgrade.stage(self.runtime, self.runtime, [], True)
        self.assertEqual(tree(self.base), before)
        self.stage()
        with self.assertRaisesRegex(ValueError, 'offline'):
            upgrade.restore(self.base/'staged', self.base/'restored', False)
        with self.assertRaisesRegex(ValueError, 'exist'):
            upgrade.restore(self.base/'staged', self.runtime, True)

    def test_symlinks_native_auth_and_uncheckpointed_sqlite_are_not_copied(self):
        root_link = self.base/'tasks'; root_link.symlink_to(self.tasks, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            upgrade.stage(self.runtime, self.base/'staged', [root_link], True)
        root_link.unlink()
        destination = self.base/'staged'; destination.symlink_to(self.base/'elsewhere', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.stage()
        destination.unlink()
        link = self.tasks/'external'; link.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.stage()
        link.unlink()
        secret = self.tasks/'auth.json'; secret.write_text('invented secret only')
        with self.assertRaisesRegex(ValueError, 'credentials'):
            self.stage()
        secret.unlink()
        sidecar = self.interaction/'control/state.sqlite-wal'; sidecar.touch()
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            self.stage()
        self.assertFalse((self.base/'staged').exists())


if __name__ == '__main__':
    unittest.main()
