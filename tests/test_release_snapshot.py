"""Offline release transactions: only disposable homes/bin/process facades."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import release_snapshot as release


class FakeSystem:
    def __init__(self, source):
        self.source = source
        self.pid = 120
        self.calls = []
        self.running = True
        self.fail_start = None

    def processes(self):
        return [dict(pid=self.pid, command='fake-controller')] if self.running else []

    def observer(self, state, source, *, healthy=True):
        release.require(self.running and source == self.source, 'fake source/identity mismatch')
        return dict(pid=self.pid, pid_identity='fake-'+str(self.pid), command='fake-controller', heartbeat=2)

    def stop(self, observer):
        release.require(observer['pid'] == self.pid, 'fake reused pid')
        self.calls.append(('stop', self.source['root']))
        self.running = False

    def start(self, source, state, env):
        self.calls.append(('start', source['root']))
        if source == self.fail_start:
            raise release.Refused('injected start failure')
        self.pid += 1
        self.source = source
        self.running = True
        return self.observer(state, source)


def writable_remove(path):
    if path.exists():
        for directory, folders, files in os.walk(path, followlinks=False):
            Path(directory).chmod(0o700)
            for name in files:
                p = Path(directory)/name
                if not p.is_symlink():
                    p.chmod(0o600)
        shutil.rmtree(path)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-release-test ')
        self.base = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.snapshot = self.base/'new snapshot'
        self.snapshot.mkdir()
        self.addCleanup(writable_remove, self.snapshot)
        for name in ('runner.py', 'coordination.py', 'scripts/install.py', '.venv/bin/python'):
            path = self.snapshot/name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text('synthetic offline material')
        (self.snapshot/'.venv/bin/python').chmod(0o755)
        self.commit = 'a'*40
        self.freeze()
        self.bin = self.base/'bin';self.bin.mkdir()
        self.old = self.base/'old snapshot';self.old.mkdir()
        for name in ('runner.py', 'coordination.py', 'python'):
            (self.old/name).write_text('old offline source')
        self.source = dict(root=str(self.old), python=str(self.old/'python'))
        self.after = self.launchers(self.snapshot, self.snapshot/'.venv/bin/python')
        for name, row in self.launchers(self.old, self.old/'python').items():
            release.atomic_entry(self.bin/name, row)
        self.before = release.entrypoints(self.bin)
        self.ato = self.base/'ato';self.native = self.base/'native'
        self.state = release.context(self.ato, self.native)
        (self.state/'control').mkdir(parents=True)
        release.save(self.state/'hub.json', dict(thread_id='offline-hub'))
        release.save(self.state/'control/config.json', dict(enabled=True, hub_thread='offline-hub', max_reminders=0))
        release.save(self.state/'notification-policy.json', dict(mode='user_input_only', completion='once'))
        release.save(self.state/'time-policy.json', dict(mode='advisory'))
        (self.state/'mailbox').mkdir()
        self.db = self.state/'mailbox/state.sqlite'
        with sqlite3.connect(self.db) as con:
            con.execute('pragma user_version=2')
            con.execute('create table messages(id text,state text,busy integer not null default 0)')
            con.execute('create table execution_leases(id text, fence integer)')
            con.execute("insert into execution_leases values('never-rewind',17)")
        self.tasks = self.ato/'task-that-must-not-change';self.tasks.mkdir()
        (self.tasks/'STOP').touch()
        release.save(self.tasks/'state.json', dict(attempts=7, elapsed_seconds=900))
        self.env = release.environment(self.ato, self.native, self.bin)
        self.system = FakeSystem(self.source)
        self.transaction = self.base/'transaction'
        self.patch_entries = patch.object(release, 'generated_entries', return_value=self.after)
        self.patch_entries.start();self.addCleanup(self.patch_entries.stop)

    def launchers(self, root, python):
        result = {}
        import shlex
        for name in release.ENTRIES:
            script = root/('coordination.py' if name == 'ato-read-handoff' else 'runner.py')
            data = ('#!/bin/sh\nexec '+shlex.quote(str(python))+' '+shlex.quote(str(script))+' "$@"\n').encode()
            result[name] = dict(data=base64.b64encode(data).decode(), mode=0o755, sha256=hashlib.sha256(data).hexdigest())
        return result

    def freeze(self):
        release.seal(self.snapshot)
        self.snapshot.chmod(0o700)
        release.save(self.snapshot/release.MANIFEST, dict(version=1, qualified=True,
            root=str(self.snapshot), commit=self.commit, tree='b'*40, files=release.inventory(self.snapshot)))
        (self.snapshot/release.MANIFEST).chmod(0o400)
        self.snapshot.chmod(0o500)

    def activate(self):
        return release.activate(self.snapshot, self.commit, self.bin, self.state,
                                self.transaction, self.env, self.system)

    def safety_bytes(self):
        return {str(p.relative_to(self.ato)):p.read_bytes() for p in self.ato.rglob('*') if p.is_file()}

    def test_activate_and_rollback_preserve_policy_tasks_and_ledger_exactly(self):
        before = self.safety_bytes()
        result = self.activate()
        self.assertEqual(result['state'], 'activated')
        self.assertEqual(release.entrypoints(self.bin), self.after)
        self.assertEqual(self.system.calls, [('stop',str(self.old)),('start',str(self.snapshot))])
        self.assertEqual(self.safety_bytes(), before)
        # New business progress after activation must survive rollback.
        with sqlite3.connect(self.db) as con:
            con.execute("update execution_leases set fence=18")
        release.save(self.tasks/'state.json', dict(attempts=8, elapsed_seconds=950))
        after = self.safety_bytes()
        result = release.rollback(self.transaction,self.bin,self.state,self.env,self.system)
        self.assertEqual(result['state'],'rolled_back')
        self.assertEqual(release.entrypoints(self.bin), self.before)
        self.assertEqual(self.safety_bytes(), after)
        self.assertEqual(self.system.source, self.source)
        self.assertEqual((self.transaction/'transaction.json').stat().st_mode & 0o777,0o600)

    def test_every_nonidle_mailbox_state_refuses_before_stop_or_entrypoint_write(self):
        for state in sorted(release.BUSY):
            with self.subTest(state=state):
                with sqlite3.connect(self.db) as con:
                    con.execute('delete from messages');con.execute('insert into messages(id,state) values(?,?)',('one',state))
                before = self.safety_bytes()
                with self.assertRaisesRegex(release.Refused,'not idle'):
                    self.activate()
                self.assertEqual(release.entrypoints(self.bin),self.before)
                self.assertEqual(self.system.calls,[])
                self.assertEqual(self.safety_bytes(),before)
                self.assertFalse(self.transaction.exists())

    def test_inflight_slots_refuse_even_when_state_is_delivered_or_terminal(self):
        for state in ('delivered', 'accepted', 'cancelled', 'expired'):
            with self.subTest(state=state):
                with sqlite3.connect(self.db) as con:
                    con.execute('delete from messages')
                    con.execute('insert into messages values(?,?,1)', ('inflight',state))
                with self.assertRaisesRegex(release.Refused, 'not idle'):
                    self.activate()
                self.assertEqual(self.system.calls, [])
                self.assertEqual(release.entrypoints(self.bin), self.before)

    def test_message_queued_during_graceful_stop_refuses_cutover(self):
        original = self.system.stop
        def stop(observer):
            original(observer)
            with sqlite3.connect(self.db) as con:
                con.execute("insert into messages(id,state) values('new','queued')")
        with patch.object(self.system,'stop',side_effect=stop):
            with self.assertRaisesRegex(release.Refused,'not idle'):
                self.activate()
        self.assertEqual(release.entrypoints(self.bin), self.before)
        self.assertEqual(release.read(self.transaction/'transaction.json')['state'],'recovery_required')
        self.assertEqual(release.mailbox(self.state)['busy'],{'queued':1})

    def test_partial_entrypoint_failure_restores_all_three_and_old_observer(self):
        original = release.atomic_entry
        failed = [False]
        def replace(path,row):
            if path.name == 'ctask-notify' and row == self.after[path.name] and not failed[0]:
                failed[0]=True
                raise OSError('injected rename failure')
            original(path,row)
        before=self.safety_bytes()
        with patch.object(release,'atomic_entry',side_effect=replace):
            with self.assertRaisesRegex(OSError,'rename failure'):self.activate()
        self.assertEqual(release.entrypoints(self.bin),self.before)
        self.assertEqual(self.system.source,self.source)
        self.assertTrue(self.system.running)
        self.assertEqual(self.safety_bytes(),before)
        self.assertEqual(release.read(self.transaction/'transaction.json')['state'],'rolled_back')

    def test_failed_new_observer_restores_old_without_ledger_restoration(self):
        self.system.fail_start = dict(root=str(self.snapshot),python=str(self.snapshot/'.venv/bin/python'))
        before=self.safety_bytes()
        with self.assertRaisesRegex(release.Refused,'start failure'):self.activate()
        self.assertEqual(release.entrypoints(self.bin),self.before)
        self.assertEqual(self.safety_bytes(),before)
        self.assertEqual(self.system.source,self.source)

    def test_keyboard_interrupt_records_recovery_and_propagates(self):
        with patch.object(self.system,'start',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.activate()
        self.assertEqual(release.entrypoints(self.bin),self.before)
        self.assertEqual(release.read(self.transaction/'transaction.json')['state'],'recovery_required')

    def test_rollback_rejects_busy_mailbox_and_external_entrypoint_replacement(self):
        self.activate()
        with sqlite3.connect(self.db) as con:con.execute("insert into messages(id,state) values('unknown','delivery_unknown')")
        with self.assertRaisesRegex(release.Refused,'not idle'):
            release.rollback(self.transaction,self.bin,self.state,self.env,self.system)
        self.assertEqual(release.entrypoints(self.bin),self.after)
        with sqlite3.connect(self.db) as con:con.execute('delete from messages')
        (self.bin/'ctask').write_text('third-party installation')
        with self.assertRaisesRegex(release.Refused,'outside this transaction'):
            release.rollback(self.transaction,self.bin,self.state,self.env,self.system)
        self.assertEqual((self.bin/'ctask').read_text(),'third-party installation')

    def test_policy_change_is_never_overwritten_on_failed_cutover(self):
        original = self.system.stop
        def stop(observer):
            original(observer)
            release.save(self.state/'time-policy.json',dict(mode='enforced',source='new actual user choice'))
        with patch.object(self.system,'stop',side_effect=stop):
            with self.assertRaisesRegex(release.Refused,'policy changed'):self.activate()
        self.assertEqual(release.entrypoints(self.bin),self.before)
        self.assertEqual(release.read(self.state/'time-policy.json')['mode'],'enforced')

    def test_wrong_identity_and_multiple_controllers_refuse_without_changes(self):
        for reason in ('PID reuse','multiple controllers'):
            with self.subTest(reason=reason), patch.object(self.system,'observer',side_effect=release.Refused(reason)):
                with self.assertRaisesRegex(release.Refused,reason):self.activate()
                self.assertEqual(self.system.calls,[])
                self.assertFalse(self.transaction.exists())

    def test_snapshot_rejects_changed_bytes_modes_links_extra_files_and_commit(self):
        with self.assertRaises(release.Refused):release.verify_snapshot(self.snapshot,'c'*40)
        p=self.snapshot/'runner.py'
        p.chmod(0o644)
        with self.assertRaisesRegex(release.Refused,'changed'):release.verify_snapshot(self.snapshot,self.commit)
        p.chmod(0o444)
        self.snapshot.chmod(0o700);(self.snapshot/'unexpected').write_text('extra');self.snapshot.chmod(0o500)
        with self.assertRaisesRegex(release.Refused,'changed'):release.verify_snapshot(self.snapshot,self.commit)
        self.snapshot.chmod(0o700);(self.snapshot/'unexpected').unlink();self.snapshot.chmod(0o500)
        p.chmod(0o644);p.write_text('tampered');p.chmod(0o444)
        with self.assertRaisesRegex(release.Refused,'changed'):release.verify_snapshot(self.snapshot,self.commit)

    def test_external_links_are_forbidden_and_sealing_does_not_chmod_interpreter(self):
        external=self.base/'external';external.write_text('host');external.chmod(0o755)
        self.snapshot.chmod(0o700)
        (self.snapshot/'escape').symlink_to(external)
        with self.assertRaisesRegex(release.Refused,'external symlink'):release.inventory(self.snapshot)
        (self.snapshot/'escape').unlink()
        p=self.snapshot/'.venv/bin/python';p.parent.chmod(0o755);p.unlink();p.symlink_to(external)
        release.seal(self.snapshot)
        self.assertEqual(external.stat().st_mode & 0o777,0o755)
        manifest=release.inventory(self.snapshot)
        self.assertEqual(manifest['.venv/bin/python']['external_sha256'],release.sha(external))

    def test_mailbox_inspection_uses_wal_without_changing_original_files(self):
        con=sqlite3.connect(self.db)
        try:
            con.execute('pragma journal_mode=WAL')
            con.execute("insert into messages(id,state) values('in-wal','delivery_unknown')");con.commit()
            before=self.safety_bytes()
            self.assertEqual(release.mailbox(self.state)['busy'],{'delivery_unknown':1})
            self.assertEqual(self.safety_bytes(),before)
        finally:con.close()

    def test_idle_boundary_excludes_concurrent_writers_but_does_not_change_rows(self):
        before=self.safety_bytes()
        with release.idle_boundary(self.state):
            con=sqlite3.connect(self.db,timeout=.01)
            try:
                with self.assertRaises(sqlite3.OperationalError):con.execute("insert into messages(id,state) values('racer','queued')")
            finally:con.close()
        self.assertEqual(self.safety_bytes(),before)

    def test_dry_run_reads_only_and_reports_busy_plan(self):
        with sqlite3.connect(self.db) as con:con.execute("insert into messages(id,state) values('queued','queued')")
        before=self.safety_bytes();bins=release.entrypoints(self.bin)
        args=['--dry-run','--repo',str(self.base),'--commit',self.commit,'--snapshot',str(self.snapshot),
              '--bin-dir',str(self.bin),'--ato-home',str(self.ato),'--codex-home',str(self.native)]
        with patch.object(release,'System',return_value=self.system),patch.object(release,'git_commit',return_value=(self.commit,'b'*40)),patch('sys.stdout') as out:
            self.assertEqual(release.main(args),0)
            text=''.join(call.args[0] for call in out.write.call_args_list)
        self.assertFalse(json.loads(text)['activation_ready'])
        self.assertEqual(self.system.calls,[])
        self.assertEqual(release.entrypoints(self.bin),bins)
        self.assertEqual(self.safety_bytes(),before)
        self.assertFalse((self.bin/'.ato-release.lock').exists())

    def test_dry_run_missing_snapshot_reports_staging_required_without_writes(self):
        args=['--dry-run','--repo',str(self.base),'--commit',self.commit,'--snapshot',str(self.base/'not-staged'),
              '--bin-dir',str(self.bin),'--ato-home',str(self.ato),'--codex-home',str(self.native)]
        before=self.safety_bytes()
        with patch.object(release,'System',return_value=self.system),patch.object(release,'git_commit',return_value=(self.commit,'b'*40)),patch('sys.stdout') as out:
            self.assertEqual(release.main(args),0)
            result=json.loads(''.join(call.args[0] for call in out.write.call_args_list))
        self.assertTrue(result['stage_required'])
        self.assertFalse(result['activation_ready'])
        self.assertFalse((self.base/'not-staged').exists())
        self.assertEqual(self.safety_bytes(),before)

    def test_explicit_change_flag_and_context_paths_are_required(self):
        args=['--activate','--snapshot',str(self.snapshot),'--commit',self.commit,'--bin-dir',str(self.bin),
              '--ato-home',str(self.ato),'--codex-home',str(self.native),'--transaction',str(self.transaction)]
        with patch('sys.stderr'),self.assertRaises(SystemExit) as result:release.main(args)
        self.assertEqual(result.exception.code,2)
        with patch('sys.stderr'):
            self.assertEqual(release.main(args+['--allow-runtime-change','--transaction',str(self.tasks/'bad-backup')]),2)
        self.assertFalse((self.tasks/'bad-backup').exists())

    def test_minimal_environment_and_legacy_layout_match_runtime_locator(self):
        with patch.dict(os.environ,dict(NODE_OPTIONS='evil',NODE_PATH='evil',PYTHONPATH='evil',DYLD_INSERT_LIBRARIES='evil',CTASK_SESSION_TOKEN='secret')):
            env=release.environment(self.ato,self.native,self.bin)
        for key in ('NODE_OPTIONS','NODE_PATH','PYTHONPATH','DYLD_INSERT_LIBRARIES','CTASK_SESSION_TOKEN'):
            self.assertNotIn(key,env)
        self.assertEqual(env['PATH'],release.PATH)
        self.assertEqual(release.context(self.native,self.native,legacy=True),self.native/'ctask-interaction')
        with self.assertRaises(release.Refused):release.context(self.ato,self.native,legacy=True)


class ProcessSafetyTests(unittest.TestCase):
    def test_real_facade_checks_identity_and_exact_command_before_sigterm(self):
        system=release.System()
        observer=dict(pid=12345,pid_identity='original',command='/safe/python /safe/runner.py _controller')
        with patch.object(system,'identity',return_value='reused'),patch('os.kill') as kill:
            with self.assertRaisesRegex(release.Refused,'identity changed'):system.stop(observer)
            kill.assert_not_called()
        with patch.object(system,'identity',return_value='original'),patch.object(system,'processes',return_value=[dict(pid=12345,command='unrelated')]),patch('os.kill') as kill:
            with self.assertRaisesRegex(release.Refused,'command changed'):system.stop(observer)
            kill.assert_not_called()
        with patch.object(system,'identity',side_effect=['original',None]),patch.object(system,'processes',side_effect=[[dict(pid=12345,command=observer['command'])],[]]),patch('os.kill') as kill:
            system.stop(observer)
            kill.assert_called_once_with(12345,signal.SIGTERM)

    def test_observer_verifies_pid_identity_unique_source_health_and_heartbeat(self):
        system=release.System();source=dict(root='/safe',python='/safe/python')
        row=dict(pid=12345,pid_identity='verified',state='watching',consecutive_failures=0,heartbeat=time.time())
        process=dict(pid=12345,command='/safe/python /safe/runner.py _controller')
        with patch.object(release,'read',return_value=row),patch.object(system,'identity',return_value='verified'),patch.object(system,'processes',return_value=[process]):
            self.assertEqual(system.observer(Path('/fake-state'),source)['pid'],12345)
            for change in (dict(pid_identity='reused'),dict(heartbeat=1),dict(state='launching'),dict(consecutive_failures=1)):
                with patch.object(release,'read',return_value=dict(row,**change)),self.assertRaises(release.Refused):system.observer(Path('/fake-state'),source)
            with patch.object(system,'processes',return_value=[process,process]),self.assertRaises(release.Refused):system.observer(Path('/fake-state'),source)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ato-stage-offline ')
        self.base=Path(self.tmp.name).resolve();self.addCleanup(self.tmp.cleanup)
        self.repo=self.base/'repo';self.repo.mkdir()
        self.dest=self.base/'release'
        self.addCleanup(writable_remove,self.dest)
        for name in ('runner.py','coordination.py','scripts/install.py','requirements-acp.lock'):
            p=self.repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# trusted synthetic source\n')
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','--all'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=Offline Test','-c','user.email=offline@example.invalid',
                        '-c','commit.gpgsign=false','commit','-qm','offline fixture'],check=True)
        self.commit,self.tree=release.git_commit(self.repo,'HEAD')

    def install(self,root,env):
        self.assertTrue(Path(env['ATO_HOME']).is_relative_to(Path(env['npm_config_cache']).parent))
        self.assertFalse(Path(env['ATO_HOME']).exists())
        p=root/'.venv/bin/python';p.parent.mkdir(parents=True);p.write_text('isolated test interpreter');p.chmod(0o755)
        (root/'integrations/pi-acp/node_modules').mkdir(parents=True)

    def test_stage_archives_exact_tree_qualifies_isolated_target_and_seals_all_material(self):
        with patch.object(release,'install_dependencies',side_effect=self.install),patch.object(release,'qualify') as qualify:
            result=release.stage(self.repo,self.commit,self.dest)
        self.assertFalse(result['activated'])
        self.assertEqual(result['tree'],self.tree)
        qualify.assert_called_once()
        self.assertEqual(qualify.call_args.args[0],self.dest)
        self.assertFalse((self.dest/'.git').exists())
        manifest=release.verify_snapshot(self.dest,self.commit)
        self.assertEqual(manifest['source_files']['runner.py'],release.sha(self.repo/'runner.py'))
        for name,row in manifest['files'].items():
            self.assertFalse(row.get('mode',0) & 0o222,name)
        with self.assertRaisesRegex(release.Refused,'already exists'):release.stage(self.repo,self.commit,self.dest)

    def test_stage_dependency_failure_leaves_evidence_unqualified_and_never_activates(self):
        with patch.object(release,'install_dependencies',side_effect=release.Refused('offline dependency failure')),patch.object(release,'activate') as activate:
            with self.assertRaisesRegex(release.Refused,'dependency failure'):release.stage(self.repo,self.commit,self.dest)
        self.assertTrue((self.dest/'runner.py').exists())
        self.assertFalse((self.dest/release.MANIFEST).exists())
        activate.assert_not_called()

    def test_stage_rejects_source_mutation_by_checks(self):
        def mutate(root,env):(root/'runner.py').write_text('changed by checks')
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
            release.qualify(self.repo,{})
        self.assertEqual([call.args[3] for call in run.call_args_list],['tests','architecture','compile'])
        self.assertIn('runner.py',run.call_args_list[-1].args[0])
        def skipped(argv,root,env,name):
            logged(argv,root,env,name)
            if name=='tests':(root/'.release-logs/tests.log').write_text('Ran 11 tests in 0.1s\n\nOK (skipped=1)\n')
        with patch.object(release,'run_logged',side_effect=skipped),self.assertRaisesRegex(release.Refused,'clean full-test receipt'):
            release.qualify(self.repo,{})


class LauncherGenerationTests(unittest.TestCase):
    def test_generated_launchers_use_selected_source_and_quote_final_reader_path(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='ato-release-bin with spaces ') as folder:
            base=Path(folder).resolve();bin_dir=base/'bin with spaces';bin_dir.mkdir()
            result=release.generated_entries(root,bin_dir)
            for name,row in result.items():release.atomic_entry(bin_dir/name,row)
            source=release.launcher_source(result)
            self.assertEqual(source['root'],str(root))
            env=release.environment(base/'ato',base/'native',bin_dir)
            env.pop('CTASK_HANDOFF_READER')
            for name in ('ctask','ato-read-handoff'):
                completed=subprocess.run([str(bin_dir/name),'--help'],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(completed.returncode,0,completed.stderr)
            rejected=subprocess.run([str(bin_dir/'ctask-notify'),'start','untrusted'],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(rejected.returncode,2)
            self.assertFalse((base/'ato').exists())
            self.assertFalse((base/'native').exists())


class ObserverStartupTests(unittest.TestCase):
    def test_start_uses_preserving_facade_and_requires_two_advancing_same_identity_heartbeats(self):
        system=release.System();source=dict(root='/offline-source',python='/offline-source/python')
        rows=[dict(pid=100,pid_identity='first',heartbeat=1),
              dict(pid=100,pid_identity='reused',heartbeat=2),
              dict(pid=100,pid_identity='reused',heartbeat=3)]
        with patch.object(release,'observer_environment',side_effect=lambda source,env: env),patch.object(system,'processes',return_value=[]),patch.object(system,'observer',side_effect=rows) as observe,patch.object(release.subprocess,'run') as run,patch.object(release.time,'sleep'):
            result=system.start(source,Path('/isolated-state'),{'ATO_HOME':'/isolated'})
        self.assertEqual(observe.call_count,3)
        self.assertEqual(result['heartbeat'],3)
        self.assertEqual(run.call_args.args[0],['/offline-source/python','-c','import control; control.start()'])
        self.assertNotIn('enable',run.call_args.args[0][-1])
        self.assertEqual(run.call_args.kwargs['env'],{'ATO_HOME':'/isolated'})

    def test_uncooperative_observer_gets_only_sigterm_and_never_sigkill(self):
        system=release.System()
        observer=dict(pid=12345,pid_identity='same',command='/safe/python /safe/runner.py _controller')
        with patch.object(system,'identity',return_value='same'),patch.object(system,'processes',return_value=[dict(pid=12345,command=observer['command'])]),patch.object(release.time,'monotonic',side_effect=[0,46]),patch('os.kill') as kill:
            with self.assertRaisesRegex(release.Refused,'refusing SIGKILL'):system.stop(observer)
        kill.assert_called_once_with(12345,signal.SIGTERM)

    def test_release_script_is_standard_library_only_and_shipped_as_inert_data(self):
        import ast
        import tomllib
        root=Path(__file__).resolve().parents[1]
        tree=ast.parse((root/'scripts/release_snapshot.py').read_text())
        imports=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):imports.extend(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node,ast.ImportFrom):imports.append(node.module.split('.')[0])
        self.assertTrue(set(imports)<=sys.stdlib_module_names)
        data=tomllib.loads((root/'pyproject.toml').read_text())
        self.assertIn('scripts/release_snapshot.py',data['tool']['setuptools']['data-files']['share/agent-task-orchestrator/scripts'])


class DependencyToolTests(unittest.TestCase):
    def test_nvm_style_tools_are_explicit_pinned_and_loader_environment_is_clean(self):
        with tempfile.TemporaryDirectory(prefix='ato-host-tools-') as folder:
            base=Path(folder).resolve();tool_dir=base/'host-node/bin';tool_dir.mkdir(parents=True)
            for name in ('node','npm'):
                p=tool_dir/name;p.write_text('#!/bin/sh\nexit 0\n');p.chmod(0o755)
            root=base/'snapshot';root.mkdir()
            env=release.environment(base/'ato',base/'native',base/'bin')
            env.update(ATO_RELEASE_SOURCE=str(base/'source'),ATO_RELEASE_NODE=str(tool_dir/'node'),ATO_RELEASE_NPM=str(tool_dir/'npm'))
            with patch.object(release,'run_logged') as run:
                tools=release.install_dependencies(root,env)
            self.assertEqual(env['PATH'],release.PATH)
            self.assertEqual(env['CTASK_NODE_BIN'],str(tool_dir/'node'))
            self.assertEqual(run.call_args_list[-1].args[0][:3],[str(tool_dir/'node'),str(tool_dir/'npm'),'ci'])
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
