"""Sealing, read-only diagnostics and journal safeguards with local fixtures."""
from contextlib import closing, redirect_stdout, redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from release_support import CURRENT, FakeSystem, legacy_entry, seal, snapshot, unseal, write
from ato_common.ato_core import ensure_private_dir
from ato_release import support as ato_release_lib
from ato_release import seal as ato_seal
from ato_task.ato_task_store import TaskError
from scripts import release_snapshot as release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ato-release-fixture-'))).resolve()
        self.paths = release.scoped_paths(self.base/'bin', self.base/'ato', self.base/'native',
                                         legacy_hub_home=self.base/'old hub')
        self.bin = self.paths['bin_dir']
        self.bin.mkdir()
        self.snapshot = snapshot(self.base/'target')
        self.old = snapshot(self.base/'prior', legacy=True)
        self.addCleanup(unseal, self.snapshot)
        self.addCleanup(unseal, self.old)
        self.system = FakeSystem()
        self.transaction = self.base/'transaction'
        self.legacy_state = self.paths['legacy_state']
        self.unified_state = self.paths['unified_state']
        for state in (self.legacy_state, self.unified_state):
            db = state/'mailbox/state.sqlite'
            ensure_private_dir(db.parent)
            with closing(sqlite3.connect(db)) as con:
                con.executescript("pragma user_version=2;"
                    "create table messages(id text primary key,state text,busy int default 0);"
                    "create table execution_leases(state text);")
            db.chmod(0o600)

    def test_fixup_installed_boundary_preserves_body_errors_and_fences_entry(self):
        script = write(self.bin, 'ato', 'synthetic launcher', 0o700)
        payload = script.read_bytes()
        with patch.dict(os.environ, ATO_LAUNCHER=str(script)), \
                patch.object(ato_seal, 'launcher_bytes', return_value=payload):
            for error in (OSError('body'), RuntimeError('body'), BlockingIOError('body'), release.Refused('body')):
                with self.subTest(error=type(error)), self.assertRaises(type(error)) as caught:
                    with ato_seal.installed_boundary('fixture'):
                        raise error
                self.assertIs(caught.exception, error)
            with release.release_lock(self.bin), self.assertRaises(TaskError) as caught:
                with ato_seal.installed_boundary('fixture'):
                    self.fail('locked boundary entered')
            self.assertEqual(caught.exception.code, 'BUSY')
            script.write_bytes(b'changed')
            with self.assertRaises(TaskError) as caught:
                with ato_seal.installed_boundary('fixture'):
                    self.fail('changed launcher entered')
            self.assertEqual(caught.exception.code, 'RUNTIME_MISMATCH')

    def test_fixup_fast_snapshot_validates_venv_interpreters_and_config(self):
        root = self.snapshot
        unseal(root)
        binary = root/'.venv/bin/python'
        binary.chmod(0o700)
        alias = binary.with_name('python3')
        alias.symlink_to('python')
        config = write(root, '.venv/pyvenv.cfg', 'synthetic config')
        seal(root, CURRENT)
        release.verify_snapshot(root, CURRENT, full=False)
        for directory in (root/'.venv', binary.parent):
            with self.subTest(directory=directory), self.assertRaisesRegex(release.Refused, 'interpreter directory'):
                directory.chmod(0o755)
                try:
                    release.verify_snapshot(root, CURRENT, full=False)
                finally:
                    directory.chmod(0o555)
            parent = directory.parent
            relocated = directory.with_name(directory.name + '-relocated')
            parent_mode = parent.stat().st_mode & 0o777
            parent.chmod(0o700)
            directory.rename(relocated)
            directory.symlink_to(relocated.name, target_is_directory=True)
            parent.chmod(parent_mode)
            try:
                with self.subTest(link=directory), self.assertRaisesRegex(release.Refused, 'interpreter directory'):
                    release.verify_snapshot(root, CURRENT, full=False)
            finally:
                parent.chmod(0o700)
                directory.unlink()
                relocated.rename(directory)
                parent.chmod(parent_mode)
        binary.parent.chmod(0o700)
        alias.unlink()
        alias.write_bytes(binary.read_bytes())
        alias.chmod(0o555)
        binary.parent.chmod(0o555)
        with self.assertRaisesRegex(release.Refused, 'interpreter link'):
            release.verify_snapshot(root, CURRENT, full=False)
        binary.parent.chmod(0o700)
        alias.unlink()
        alias.symlink_to('python')
        binary.parent.chmod(0o555)
        for file in (binary, config):
            original = file.read_bytes()
            mode = file.stat().st_mode & 0o777
            with self.subTest(file=file), self.assertRaisesRegex(release.Refused, 'interpreter file'):
                file.chmod(mode | 0o200)
                try:
                    release.verify_snapshot(root, CURRENT, full=False)
                finally:
                    file.write_bytes(original + b'tampered')
                    file.chmod(mode)
            with self.subTest(content=file), self.assertRaisesRegex(release.Refused, 'interpreter file'):
                try:
                    release.verify_snapshot(root, CURRENT, full=False)
                finally:
                    file.chmod(mode | 0o200)
                    file.write_bytes(original)
                    file.chmod(mode)
        release.verify_snapshot(root, CURRENT, full=False)

    def message(self, state, status, busy=0):
        with closing(sqlite3.connect(state/'mailbox/state.sqlite')) as con, con:
            con.execute('insert into messages values(?,?,?)', ('offline-message', status, busy))

    def contents(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def old_observer(self):
        for name in release.LEGACY:
            legacy_entry(self.bin, name, self.old)
        write(self.legacy_state, 'control/config.json', json.dumps(dict(enabled=True, hub_thread='fixture')))
        write(self.legacy_state, 'hub.json', json.dumps(dict(thread_id='fixture')))
        write(self.paths['ato_home'], 'unified-v1/host/observation.json', '{"enabled":true}')
        source = release.launcher_sources(release.entrypoints(self.bin))['ctask-notify']
        return self.system.seed(self.legacy_state, source)

    def activate(self):
        return release.activate(self.snapshot, CURRENT, self.paths, self.transaction, self.system)

    def rollback(self):
        return release.rollback(self.transaction, self.paths, self.system)

    def args(self, mode):
        args = ['--'+mode, '--bin-dir', str(self.bin), '--ato-home', str(self.paths['ato_home']),
                '--codex-home', str(self.paths['codex_home']),
                '--legacy-hub-home', str(self.paths['legacy_hub_home'])]
        if mode == 'dry-run':
            args += ['--repo', str(self.base)]
        if mode != 'rollback':
            args += ['--commit', CURRENT, '--snapshot', str(self.snapshot)]
        if mode != 'dry-run':
            args += ['--transaction', str(self.transaction)]
        return args

    def test_dry_run_reports_both_namespaces_and_is_side_effect_free(self):
        self.old_observer()
        self.message(self.unified_state, 'delivery_unknown')
        snapshot_before = self.contents(self.base)
        with patch.object(release, 'System', return_value=self.system), \
                patch.object(release, 'git_commit', return_value=(CURRENT, 'fixture')), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(release.main(self.args('dry-run')), 0)
        report = json.loads(out.getvalue())
        self.assertTrue(report['activation_ready'])
        self.assertEqual(report['observer_source']['root'], str(self.old))
        self.assertEqual(report['mailboxes']['unified_state']['busy'], {'delivery_unknown': 1})
        self.assertEqual(report['entrypoints']['ato'], {'present': False})
        self.assertEqual(snapshot_before, self.contents(self.base))
        self.assertEqual(self.system.calls, [])
        self.assertFalse(self.transaction.exists())

    def test_dry_run_missing_candidate_does_not_create_it(self):
        missing = self.base/'missing'
        args = self.args('dry-run')
        args[-1] = str(missing)
        with patch.object(release, 'System', return_value=self.system), \
                patch.object(release, 'git_commit', return_value=(CURRENT, 'fixture')), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(release.main(args), 0)
        self.assertIn('snapshot must be staged and qualified', json.loads(out.getvalue())['blockers'])
        self.assertFalse(missing.exists())

    def test_cutover_and_rollback_preserve_busy_mailbox_rows(self):
        self.old_observer()
        self.message(self.legacy_state, 'claimed')
        self.message(self.unified_state, 'delivery_unknown')
        report = release.inspect(self.paths, self.system)
        self.assertEqual(report['blockers'], [])
        self.assertFalse(report['mailboxes']['legacy_state']['idle'])
        self.assertFalse(report['mailboxes']['unified_state']['idle'])
        self.assertEqual(self.activate()['state'], 'activated')
        self.assertEqual(self.rollback()['state'], 'rolled_back')
        self.assertEqual(release.mailbox(self.legacy_state)['busy'], {'claimed': 1})
        self.assertEqual(release.mailbox(self.unified_state)['busy'], {'delivery_unknown': 1})

    def test_cutover_preserves_active_execution_lease(self):
        db = self.unified_state/'mailbox/state.sqlite'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute("insert into execution_leases values('active')")
        self.assertEqual(self.activate()['state'], 'activated')
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute('select * from execution_leases').fetchall(), [('active',)])

    def test_message_arriving_at_observer_shutdown_is_retained(self):
        self.old_observer()
        self.system.after_stop = lambda: self.message(self.legacy_state, 'delivery_unknown')
        self.assertEqual(self.activate()['state'], 'activated')
        self.assertEqual(release.mailbox(self.legacy_state)['busy'], {'delivery_unknown': 1})

    def test_cutover_and_repeat_rollback_restore_exact_source_observer_and_original_namespace(self):
        first = self.old_observer()
        prior = release.entrypoints(self.bin)
        runtime_before = self.contents(self.paths['ato_home'])
        self.assertEqual(self.activate()['observer']['surface'], release.SURFACE)
        self.assertEqual([p.name for p in self.bin.iterdir()], ['ato'])
        self.assertEqual(self.rollback()['observer']['surface'], 'legacy')
        self.assertEqual(release.entrypoints(self.bin), prior)
        self.assertEqual(runtime_before, self.contents(self.paths['ato_home']))
        self.assertIn(('stop', 'legacy', first['pid']), self.system.calls)
        self.assertEqual(self.system.calls[-1][2]['ATO_HOME'], str(self.paths['legacy_home']))
        calls_before = list(self.system.calls)
        self.rollback()
        self.assertEqual(self.system.calls, calls_before)

    def test_existing_unified_observer_rolls_back_to_its_original_seal(self):
        prior = snapshot(self.base/'prior unified', commit='d'*40)
        self.addCleanup(unseal, prior)
        source = release.snapshot_source(prior)
        release.atomic_entry(self.bin/'ato', release.entry_set(source)['ato'])
        source = release.launcher_sources(release.entrypoints(self.bin))['ato']
        original = self.system.seed(self.unified_state, source)
        write(self.paths['ato_home'], 'unified-v1/host/observation.json', '{"enabled":true}')
        activated = self.activate()
        self.assertNotEqual(activated['observer']['command'], original['command'])
        restored = self.rollback()
        self.assertEqual(restored['observer']['command'], original['command'])
        self.assertEqual(release.launcher_sources(release.entrypoints(self.bin))['ato'], source)

    def test_same_selection_is_refused_before_transaction_or_observer_stop(self):
        source = release.snapshot_source(self.snapshot)
        release.atomic_entry(self.bin/'ato', release.entry_set(source)['ato'])
        selected = release.launcher_sources(release.entrypoints(self.bin))['ato']
        observer = self.system.seed(self.unified_state, selected)
        write(self.paths['ato_home'], 'unified-v1/host/observation.json', '{"enabled":true}')
        original = self.contents(self.base)
        with patch.object(release, 'System', return_value=self.system), \
                patch.object(release, 'git_commit', return_value=(CURRENT, 'fixture')), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(release.main(self.args('dry-run')), 0)
        report = json.loads(out.getvalue())
        self.assertFalse(report['activation_ready'])
        self.assertIn('snapshot is already selected', report['blockers'])
        with self.assertRaisesRegex(release.Refused, 'snapshot is already selected'):
            self.activate()
        self.assertEqual(self.system.calls, [])
        self.assertEqual(self.system.observer(self.unified_state, selected), observer)
        self.assertEqual(self.contents(self.base), original)
        self.assertFalse(self.transaction.exists())

        legacy_entry(self.bin, 'ctask', self.old)
        with self.assertRaisesRegex(release.Refused, 'explicit convergence'):
            self.activate()
        result = release.activate(self.snapshot, CURRENT, self.paths, self.transaction, self.system,
                                  converge_mixed=True)
        self.assertEqual(result['state'], 'activated')
        self.assertEqual([path.name for path in self.bin.iterdir()], ['ato'])

    def test_both_mailbox_writes_are_excluded_during_each_replacement(self):
        self.old_observer()
        original = release.atomic_entry

        def locked(path, row):
            for state in (self.legacy_state, self.unified_state):
                with closing(sqlite3.connect(state/'mailbox/state.sqlite', timeout=.01)) as con:
                    with self.assertRaises(sqlite3.OperationalError):
                        con.execute("insert into messages values('racer','queued',0)")
            original(path, row)

        with patch.object(release, 'atomic_entry', side_effect=locked):
            self.activate()
            self.rollback()

    def test_failed_target_observer_is_recovered_with_recorded_legacy_identity(self):
        self.old_observer()
        prior = release.entrypoints(self.bin)
        self.system.fail_after_spawn = True
        with self.assertRaisesRegex(release.Refused, 'after observer admission'):
            self.activate()
        journal = release.read(self.transaction/'transaction.json')
        self.assertEqual(journal['state'], 'rolled_back')
        self.assertEqual(release.entrypoints(self.bin), prior)
        self.assertIn(('stop', release.SURFACE, journal['new_observer']['pid']), self.system.calls)
        self.assertEqual(journal['rollback_observer']['surface'], 'legacy')

    def test_changed_old_seal_and_policy_are_refused_without_writing_journal(self):
        self.old_observer()
        self.activate()
        current = release.entrypoints(self.bin)
        journal = self.transaction/'transaction.json'
        original = journal.read_bytes()
        self.old.chmod(0o700)
        with self.assertRaisesRegex(release.Refused, 'read-only'):
            self.rollback()
        self.old.chmod(0o500)
        policy = write(self.legacy_state, 'notification-policy.json', '{"mode":"user_input_only"}')
        with self.assertRaisesRegex(release.Refused, 'policies changed'):
            self.rollback()
        self.assertEqual(policy.read_text(), '{"mode":"user_input_only"}')
        self.assertEqual(release.entrypoints(self.bin), current)
        self.assertEqual(journal.read_bytes(), original)

    def test_dry_run_reports_busy_lock_missing_legacy_and_h1_states(self):
        self.old_observer()
        flow = self.base/'fixture repo/.codex/tasks/workflows/test'
        lock = write(flow.parent.parent, '.worker.lock')
        self.base.joinpath('fixture repo/.codex/tasks/workflows').mkdir()
        with closing(sqlite3.connect(self.legacy_state/'control/state.sqlite')) as con, con:
            con.execute('create table workflows(path text)')
            con.execute('insert into workflows values(?)', (str(flow),))
        hub = self.paths['legacy_hub_home']/'hubs/hub-fixture/tasks'
        write(hub/'clean', 'state.json', json.dumps(dict(status='cleaned', workflow_history=[str(flow/'old')])))
        for status in ('running_worker', 'fixing'):
            write(hub/status, 'state.json', json.dumps(dict(status=status)))
        with lock.open() as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(release, 'System', return_value=self.system), \
                    patch.object(release, 'git_commit', return_value=(CURRENT, 'fixture')), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(release.main(self.args('dry-run')), 0)
            report = json.loads(out.getvalue())
        self.assertTrue(report['activation_ready'])
        activity = report['activity']
        self.assertIn('legacy H1 task has unresolved activity', activity)
        self.assertIn('legacy workflow lock is busy: ' + str(lock), activity)
        self.assertIn('missing legacy workflow state: ' + str(flow), activity)

    def test_active_selected_process_is_reported_and_left_running(self):
        self.old_observer()
        other = dict(pid=8, command=str(self.old/'runner.py') + ' _worker fixture')
        processes = self.system.processes
        with patch.object(self.system, 'processes', side_effect=lambda: [other, *processes()]):
            self.assertIn('selected runtime has active commands', release.inspect(self.paths, self.system)['activity'])
            self.assertEqual(self.activate()['state'], 'activated')
        self.assertFalse(any(call[0] == 'stop' and call[2] == 8 for call in self.system.calls))

    def test_explicit_change_scope_and_cross_version_reference(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(release.main(self.args('activate')), 2)
        binding = legacy_entry(self.bin, 'ctask', self.old)
        data = binding.read_text().replace(str(self.old), str(self.snapshot))
        binding.write_text(data)
        with self.assertRaises(release.Refused):
            self.activate()
        self.assertFalse(self.transaction.exists())

    def test_snapshot_rejects_changed_bytes_modes_links_extra_files_and_commit(self):
        with self.assertRaises(release.Refused):
            release.verify_snapshot(self.snapshot, 'c'*40)
        source = self.snapshot/'ato.py'
        source.chmod(0o644)
        with self.assertRaisesRegex(release.Refused, 'changed'):
            release.verify_snapshot(self.snapshot, CURRENT)
        source.chmod(0o444)
        self.snapshot.chmod(0o700)
        extra = write(self.snapshot, 'unexpected')
        self.snapshot.chmod(0o500)
        with self.assertRaisesRegex(release.Refused, 'changed'):
            release.verify_snapshot(self.snapshot, CURRENT)
        self.snapshot.chmod(0o700)
        extra.unlink()
        source.chmod(0o644)
        source.write_text('tampered')
        source.chmod(0o444)
        self.snapshot.chmod(0o500)
        with self.assertRaisesRegex(release.Refused, 'changed'):
            release.verify_snapshot(self.snapshot, CURRENT)

    def test_external_links_are_forbidden_and_sealing_does_not_chmod_interpreter(self):
        outside = write(self.base, 'external', mode=0o755)
        self.snapshot.chmod(0o700)
        escape = self.snapshot/'escape'
        escape.symlink_to(outside)
        with self.assertRaisesRegex(release.Refused, 'external symlink'):
            release.inventory(self.snapshot)
        escape.unlink()
        python = self.snapshot/'.venv/bin/python'
        python.parent.chmod(0o700)
        python.unlink()
        python.symlink_to(outside)
        seal(self.snapshot, CURRENT)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
        self.assertEqual(release.inventory(self.snapshot)['.venv/bin/python']['external_sha256'], release.sha(outside))
        outside.write_text('updated executable')
        with self.assertRaisesRegex(release.Refused, 'changed'):
            release.verify_snapshot(self.snapshot, CURRENT)

    def test_runtime_checks_sealed_source_and_full_check_covers_dependencies_and_interpreter(self):
        root = self.base/'fast-check'
        interpreter = write(self.base, 'python3.13', '#!/bin/sh\nexit 0\n', 0o700)
        build_tool = write(self.base, 'npm', '#!/bin/sh\nexit 0\n', 0o700)
        source = write(root, 'ato.py')
        for name in ('ato_runtime.py', 'scripts/install.py', 'skills/ato/SKILL.md'):
            write(root, name)
        dependency = write(root, 'integrations/pi-acp/node_modules/fixture/index.js')
        python = root/'.venv/bin/python'
        ensure_private_dir(python.parent)
        python.symlink_to(interpreter)
        python.with_name('python3.13').symlink_to(interpreter)
        seal(root, CURRENT, tools={'npm': release.host_tool('npm', build_tool)})
        self.addCleanup(unseal, root)
        build_tool.unlink()
        release.verify_snapshot(root, CURRENT)
        dependency.chmod(0o600)
        dependency.write_text('changed')
        dependency.chmod(0o444)
        release.verify_snapshot(root, CURRENT, full=False)
        with self.assertRaisesRegex(release.Refused, 'content'):
            release.verify_snapshot(root, CURRENT)
        dependency.chmod(0o600)
        dependency.write_text('offline fixture')
        dependency.chmod(0o444)
        interpreter.write_text('#!/bin/sh\nexit 1\n')
        with self.assertRaisesRegex(release.Refused, 'content'):
            release.verify_snapshot(root, CURRENT)
        release.verify_snapshot(root, CURRENT, allow_interpreter_change=True)
        with self.assertRaisesRegex(release.Refused, 'interpreter'):
            release.verify_snapshot(root, CURRENT, full=False)
        source.chmod(0o600)
        source.write_text('changed')
        source.chmod(0o444)
        with self.assertRaisesRegex(release.Refused, 'source changed'):
            release.verify_snapshot(root, CURRENT, full=False)

    def test_mailbox_inspection_uses_wal_without_changing_original_files(self):
        path = self.unified_state/'mailbox/state.sqlite'
        with closing(sqlite3.connect(path)) as con:
            con.execute('pragma journal_mode=WAL')
            self.message(self.unified_state, 'delivery_unknown')
            before = self.contents(self.unified_state)
            self.assertEqual(release.mailbox(self.unified_state)['busy'], {'delivery_unknown': 1})
            self.assertEqual(before, self.contents(self.unified_state))

    def test_minimal_environment_and_legacy_layout_match_the_operator_selection(self):
        with patch.dict(os.environ, dict(NODE_OPTIONS='ambient', NODE_PATH='ambient', PYTHONPATH='ambient',
                DYLD_INSERT_LIBRARIES='ambient', ATO_ACTOR_FILE='ambient', CTASK_SESSION_TOKEN='ambient')):
            env = release.environment(self.paths['ato_home'], self.paths['codex_home'])
        self.assertFalse(set(env) & {'NODE_OPTIONS','NODE_PATH','PYTHONPATH','DYLD_INSERT_LIBRARIES',
                                    'ATO_ACTOR_FILE','CTASK_SESSION_TOKEN'})
        self.assertEqual(release.context(self.paths['codex_home'], self.paths['codex_home'], legacy=True),
                         self.paths['codex_home']/'ctask-interaction')
        with self.assertRaises(release.Refused):
            release.context(self.paths['ato_home'], self.paths['codex_home'], legacy=True)


class ProcessSafetyTests(unittest.TestCase):
    def test_signal_requires_exact_command_and_birth_and_only_sends_sigterm(self):
        system = release.System()
        observer = dict(pid=12345, pid_identity='original', command='/fixture/python /fixture/runner.py _controller')
        process = dict(pid=12345, command=observer['command'])
        with patch.object(system, 'identity', return_value='reused'), patch('os.kill') as kill:
            with self.assertRaisesRegex(release.Refused, 'identity changed'):
                system.stop(observer)
            kill.assert_not_called()
        with patch.object(system, 'identity', return_value='original'), \
                patch.object(system, 'processes', return_value=[dict(process, command='unrelated')]), patch('os.kill') as kill:
            with self.assertRaisesRegex(release.Refused, 'command changed'):
                system.stop(observer)
            kill.assert_not_called()
        with patch.object(system, 'identity', side_effect=['original', None]), \
                patch.object(system, 'processes', side_effect=[[process], []]), patch('os.kill') as kill:
            system.stop(observer)
            kill.assert_called_once_with(12345, signal.SIGTERM)
        with patch.object(system, 'identity', return_value='original'), \
                patch.object(system, 'processes', return_value=[process]), \
                patch.object(release.time, 'monotonic', side_effect=[0, 31]), patch('os.kill') as kill:
            with self.assertRaisesRegex(release.Refused, 'did not exit gracefully'):
                system.stop(observer)
            kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_observer_verifies_unique_process_identity_and_fresh_heartbeat(self):
        system = release.System()
        source = dict(root='/fixture', python='/fixture/python')
        row = dict(pid=12345, pid_identity='verified', state='watching', consecutive_failures=0, heartbeat=time.time())
        process = dict(pid=12345, command=' '.join(release.observer_argv(source)))
        with patch.object(ato_release_lib, 'read_private', return_value=row), \
                patch.object(system, 'identity', return_value='verified'), \
                patch.object(system, 'processes', return_value=[process]):
            self.assertEqual(system.observer(Path('/fixture-state'), source)['pid'], 12345)
            for change in (dict(pid_identity='reused'), dict(heartbeat=1),
                           dict(state='launching'), dict(consecutive_failures=1)):
                with patch.object(ato_release_lib, 'read_private', return_value=dict(row, **change)):
                    with self.assertRaises(release.Refused):
                        system.observer(Path('/fixture-state'), source)
            with patch.object(system, 'processes', return_value=[process, process]), self.assertRaises(release.Refused):
                system.observer(Path('/fixture-state'), source)

    def test_legacy_exit_with_cleared_pid_still_requires_no_matching_process(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            source = dict(root='/fixture', python='/fixture/python')
            row = dict(pid=None, pid_identity=None, state='exited', consecutive_failures=0,
                       error=None, heartbeat=time.time())
            write(state, 'control/process.json', json.dumps(row))
            system = release.System()
            process = dict(pid=12345, command=' '.join(release.observer_argv(source)))
            with patch.object(system, 'processes', return_value=[]):
                self.assertIsNone(system.observer(state, source))
                self.assertIsNone(system.observer(state, None))
                with self.assertRaisesRegex(release.Refused, 'invalid observer process record'):
                    system.observer(state, dict(source, surface=release.SURFACE, seal='fixture'))
                write(state, 'control/process.json', json.dumps(dict(row, pid_identity='unexpected')))
                with self.assertRaisesRegex(release.Refused, 'invalid legacy observer exit'):
                    system.observer(state, source)
            write(state, 'control/process.json', json.dumps(row))
            with patch.object(system, 'processes', return_value=[process]):
                with self.assertRaisesRegex(release.Refused, 'invalid legacy observer exit'):
                    system.observer(state, source)

    def test_legacy_exit_rejects_unselected_live_controller(self):
        system = release.System()
        source = dict(root='/fixture', python='/fixture/python')
        row = dict(pid=None, state='exited')
        with patch.object(ato_release_lib, 'read_private', return_value=row), \
                patch.object(system, 'processes', return_value=[]):
            self.assertIsNone(system.observer(Path('/fixture-state'), source))
            self.assertIsNone(system.observer(Path('/fixture-state'), None))
            for command in ('/other/python /other/runner.py _controller', 'python runner.py _controller'):
                with patch.object(system, 'processes', return_value=[dict(pid=77, command=command)]):
                    with self.assertRaisesRegex(release.Refused, 'invalid legacy observer exit'):
                        system.observer(Path('/fixture-state'), None)
            with patch.object(ato_release_lib, 'read_private', return_value=dict(row, pid_identity='unexpected')):
                with self.assertRaisesRegex(release.Refused, 'invalid legacy observer exit'):
                    system.observer(Path('/fixture-state'), source)

    def test_stopped_unified_observer_refuses_live_or_unknown_identity(self):
        system = release.System()
        source = dict(root='/fixture', python='/fixture/python', surface=release.SURFACE, seal='seal')
        row = dict(pid=77, pid_identity='original', state='stopped', release='seal')
        with patch.object(ato_release_lib, 'read_private', return_value=row), \
                patch.object(system, 'processes', return_value=[dict(pid=77, command='new process')]):
            for disposition in ('reused', 'absent', 'live', 'unknown'):
                with patch('ato_task.ato_process.disposition', return_value=disposition) as identity:
                    if disposition in ('reused', 'absent'):
                        self.assertIsNone(system.observer(Path('/fixture-state'), source))
                    else:
                        with self.assertRaisesRegex(release.Refused, 'stop is unverified'):
                            system.observer(Path('/fixture-state'), source)
                    identity.assert_called_once_with(77, 'original')

    def test_failed_legacy_bootstrap_records_the_only_matching_process(self):
        system = release.System()
        source = dict(root='/fixture', python='/fixture/python')
        process = dict(pid=12345, command=' '.join(release.observer_argv(source)))
        observed = []
        with patch.object(system, 'processes', side_effect=[[], [process]]), \
                patch.object(system, 'identity', return_value='verified'), \
                patch.object(release, 'observer_environment', return_value={}), \
                patch.object(release.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 25)):
            with self.assertRaises(subprocess.TimeoutExpired):
                system.start(source, Path('/fixture-state'), {}, observed.append)
        self.assertEqual(observed, [dict(process, pid_identity='verified', surface='legacy')])

    def test_start_requires_two_advancing_heartbeats_from_same_process(self):
        system = release.System()
        source = dict(root='/fixture-source', python='/fixture-source/python')
        rows = [dict(pid=100, pid_identity='first', heartbeat=1),
                dict(pid=100, pid_identity='reused', heartbeat=2),
                dict(pid=100, pid_identity='reused', heartbeat=3)]
        with patch.object(release, 'observer_environment', side_effect=lambda _, env: env), \
                patch.object(system, 'processes', return_value=[]), \
                patch.object(system, 'observer', side_effect=rows) as inspect, \
                patch.object(release.subprocess, 'run') as run, patch.object(release.time, 'sleep'):
            result = system.start(source, Path('/fixture-state'), {'ATO_HOME':'/fixture-state'}, lambda _: None)
        self.assertEqual(inspect.call_count, 3)
        self.assertEqual(result['heartbeat'], 3)
        self.assertEqual(run.call_args.args[0][-1], 'import control; control.start()')

    def test_unified_observer_uses_the_pinned_node_and_trusted_queue_override(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            node = write(base, 'node', '#!/bin/sh\nexit 0\n', 0o700)
            queue = write(base, 'queue', '#!/bin/sh\nexit 0\n', 0o700)
            release.save(base/release.MANIFEST, dict(tools=dict(node=release.host_tool('node', node))))
            source = dict(root=str(base), python='/fixture/python', surface=release.SURFACE, seal='test-seal')
            env = release.environment(base/'state', base/'native', home=base/'home')
            with patch.dict(os.environ, CTASK_QUEUE_BIN=str(queue)), \
                    patch('ato_task.ato_process.identity', return_value='birth'), \
                    patch.object(release.subprocess, 'Popen', return_value=Mock(pid=77)) as launch:
                release.System()._start_unified(source, env, Mock(), {})
            child = launch.call_args.kwargs['env']
            self.assertEqual(child['CTASK_NODE_BIN'], str(node))
            self.assertEqual(child['CTASK_QUEUE_BIN'], str(queue))
            self.assertEqual(child['HOME'], str(base/'home'))
            self.assertNotIn('CTASK_QUEUE_BIN', env)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ato-stage-offline ')
        self.base=Path(self.tmp.name).resolve();self.addCleanup(self.tmp.cleanup)
        self.repo=self.base/'repo';self.repo.mkdir()
        self.dest=self.base/'release'
        self.addCleanup(unseal,self.dest)
        for name in ('ato.py','ato_runtime.py','scripts/install.py','skills/ato/SKILL.md','requirements-acp.lock'):
            p=self.repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# trusted synthetic source\n')
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','--all'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=Offline Test','-c','user.email=offline@example.invalid',
                        '-c','commit.gpgsign=false','commit','-qm','offline fixture'],check=True)
        self.commit,self.tree=release.git_commit(self.repo,'HEAD')

    def install(self, root, env, *, with_pi):
        self.assertTrue(Path(env['ATO_HOME']).is_relative_to(Path(env['HOME'])))
        self.assertFalse(Path(env['ATO_HOME']).exists())
        p=root/'.venv/bin/python';p.parent.mkdir(parents=True);p.write_text('isolated test interpreter');p.chmod(0o755)
        if with_pi:
            (root/'integrations/pi-acp/node_modules').mkdir(parents=True)
        return {}

    def test_stage_archives_exact_tree_qualifies_isolated_target_and_seals_all_material(self):
        with patch.object(release,'install_dependencies',side_effect=self.install),patch.object(release,'qualify') as qualify:
            result=release.stage(self.repo,self.commit,self.dest)
        self.assertFalse(result['activated'])
        self.assertFalse(result['with_pi'])
        self.assertEqual(result['tests'], 'not_run')
        self.assertEqual(result['tree'],self.tree)
        qualify.assert_called_once()
        self.assertEqual(qualify.call_args.args[0],self.dest)
        self.assertFalse((self.dest/'.git').exists())
        manifest=release.verify_snapshot(self.dest,self.commit)
        self.assertFalse(manifest['with_pi'])
        self.assertFalse((self.dest/'integrations/pi-acp/node_modules').exists())
        self.assertEqual(manifest['source_files']['ato.py'],release.sha(self.repo/'ato.py'))
        for name,row in manifest['files'].items():
            self.assertFalse(row.get('mode',0) & 0o222,name)
        with self.assertRaisesRegex(release.Refused,'already exists'):release.stage(self.repo,self.commit,self.dest)

    def test_stage_dependency_failure_leaves_evidence_unqualified_and_never_activates(self):
        with patch.object(release,'install_dependencies',side_effect=release.Refused('offline dependency failure')),patch.object(release,'activate') as activate:
            with self.assertRaisesRegex(release.Refused,'dependency failure'):release.stage(self.repo,self.commit,self.dest)
        self.assertTrue((self.dest/'ato.py').exists())
        self.assertFalse((self.dest/release.MANIFEST).exists())
        activate.assert_not_called()

    def test_stage_rejects_source_mutation_by_checks(self):
        def mutate(root,env):(root/'ato.py').write_text('changed by checks')
        with patch.object(release,'install_dependencies',side_effect=self.install),patch.object(release,'qualify',side_effect=mutate):
            with self.assertRaisesRegex(release.Refused,'modified source'):release.stage(self.repo,self.commit,self.dest)
        self.assertFalse((self.dest/release.MANIFEST).exists())

    def test_stage_rejects_archive_tree_mismatch(self):
        original=release.command
        def output(argv,**kw):
            return '0'*40 if list(argv)==['git','write-tree'] else original(argv,**kw)
        with patch.object(release,'command',side_effect=output),patch.object(release,'install_dependencies') as install:
            with self.assertRaisesRegex(release.Refused,'tree differs'):release.stage(self.repo,self.commit,self.dest)
        install.assert_not_called()

    def test_qualifier_requires_clean_test_receipt_and_runs_architecture_and_all_sources(self):
        def logged(argv,root,env,name):
            if name=='tests':
                logs=root/'.release-logs';logs.mkdir(exist_ok=True)
                (logs/'tests.log').write_text('Ran 11 tests in 0.1s\n\nOK\n')
        with patch.object(release,'run_logged',side_effect=logged) as run:
            release.run_release_tests(self.repo,{})
            release.qualify(self.repo,{})
        self.assertEqual([call.args[3] for call in run.call_args_list],['tests','architecture','compile'])
        self.assertIn('ato.py',run.call_args_list[-1].args[0])
        def skipped(argv,root,env,name):
            logged(argv,root,env,name)
            if name=='tests':(root/'.release-logs/tests.log').write_text('Ran 11 tests in 0.1s\n\nOK (skipped=1)\n')
        with patch.object(release,'run_logged',side_effect=skipped),self.assertRaisesRegex(release.Refused,'clean full-test receipt'):
            release.run_release_tests(self.repo,{})

    def test_stage_records_explicit_test_run(self):
        with patch.object(release, 'install_dependencies', side_effect=self.install), patch.object(
                release, 'run_release_tests') as suite, patch.object(release, 'qualify'):
            result = release.stage(self.repo, self.commit, self.dest, run_tests=True,
                                   wheelhouse=self.base/'wheels', npm_cache=self.base/'npm-cache')
        suite.assert_called_once()
        self.assertTrue(result['with_pi'])
        self.assertEqual(result['tests'], 'passed')
        self.assertEqual(release.verify_snapshot(self.dest, self.commit)['tests'], 'passed')


class DependencyToolTests(unittest.TestCase):
    def test_core_staging_and_observer_do_not_resolve_node(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            release.save(root/release.MANIFEST, dict(tools={}))
            env = release.environment(root/'ato', root/'native')
            with patch.object(release, 'host_tool') as host_tool, patch.object(release, 'run_logged') as run:
                self.assertEqual(release.install_dependencies(root, env, with_pi=False), {})
                selected = release.observer_environment(dict(root=str(root), surface=release.SURFACE), env)
            host_tool.assert_not_called()
            run.assert_called_once_with([sys.executable, '-m', 'venv', '--without-pip', root/'.venv'],
                                        root, env, 'venv')
            self.assertNotIn('CTASK_NODE_BIN', selected)

    def test_nvm_style_tools_are_explicit_pinned_and_loader_environment_is_clean(self):
        with tempfile.TemporaryDirectory(prefix='ato-host-tools-') as folder:
            base=Path(folder).resolve();tool_dir=base/'host-node/bin';tool_dir.mkdir(parents=True)
            for name in ('node','npm'):
                p=tool_dir/name;p.write_text('#!/bin/sh\nexit 0\n');p.chmod(0o755)
            root=base/'snapshot';root.mkdir()
            env=release.environment(base/'ato',base/'native')
            env.update(ATO_RELEASE_SOURCE=str(base/'source'),ATO_RELEASE_NODE=str(tool_dir/'node'),ATO_RELEASE_NPM=str(tool_dir/'npm'))
            with patch.object(release,'run_logged') as run:
                tools=release.install_dependencies(root, env, with_pi=True)
            self.assertEqual(env['PATH'],release.PATH)
            self.assertEqual(env['CTASK_NODE_BIN'],str(tool_dir/'node'))
            argv, _, npm_env, _ = run.call_args_list[-1].args
            self.assertEqual(argv[:2], [str(tool_dir/'npm'), 'ci'])
            self.assertEqual(npm_env['PATH'].split(os.pathsep)[0], str(tool_dir))
            npm_js = tool_dir/'npm-cli.js'
            npm_js.write_text('#!/usr/bin/env node\n')
            npm_js.chmod(0o755)
            js_env = dict(env, ATO_RELEASE_SOURCE=str(base/'source'), ATO_RELEASE_NODE=str(tool_dir/'node'),
                          ATO_RELEASE_NPM=str(npm_js))
            with patch.object(release, 'run_logged') as js_run:
                release.install_dependencies(root, js_env, with_pi=True)
            self.assertEqual(js_run.call_args.args[0][:3], [str(tool_dir/'node'), str(npm_js), 'ci'])
            release.verify_tools(tools)
            release.save(root/release.MANIFEST,dict(tools=tools))
            selected=release.observer_environment(dict(root=str(root)),env)
            self.assertEqual(selected['CTASK_NODE_BIN'],str(tool_dir/'node'))
            (tool_dir/'node').write_text('#!/bin/sh\nchanged\n')
            with self.assertRaisesRegex(release.Refused,'executable changed'):release.verify_tools(tools)

    def test_tool_selection_rejects_workspace_writable_and_relative_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder).resolve();p=base/'node';p.write_text('#!/bin/sh\n');p.chmod(0o755)
            with self.assertRaisesRegex(release.Refused,'enters source'):release.host_tool('node',p,forbidden=(base,))
            p.chmod(0o777)
            with self.assertRaisesRegex(release.Refused,'permissions'):release.host_tool('node',p)
            with self.assertRaisesRegex(release.Refused,'absolute'):release.host_tool('node','relative/node')
