"""Sealing, read-only diagnostics and journal safeguards with local fixtures."""
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from release_support import CURRENT, FakeSystem, legacy_entry, seal, snapshot, unseal, write
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
            db.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db)) as con:
                con.executescript("pragma user_version=2;"
                    "create table messages(id text primary key,state text,busy int default 0);"
                    "create table execution_leases(state text);")
            db.chmod(0o600)

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
        self.assertFalse(report['activation_ready'])
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

    def test_active_mailbox_and_turn_slots_refuse_both_directions_before_stop(self):
        self.old_observer()
        for state in (self.legacy_state, self.unified_state):
            for status, busy in [('queued', 0), ('claimed', 0), ('dispatching', 0),
                                 ('delivery_unknown', 0), ('delivered', 1), ('cancelled', 1)]:
                with self.subTest(state=state, status=status):
                    self.message(state, status, busy)
                    with self.assertRaisesRegex(release.Refused, 'mailbox is busy'):
                        self.activate()
                    with closing(sqlite3.connect(state/'mailbox/state.sqlite')) as con, con:
                        con.execute('delete from messages')
        self.assertEqual(self.system.calls, [])
        self.activate()
        self.system.calls.clear()
        self.message(self.legacy_state, 'delivery_unknown')
        with self.assertRaisesRegex(release.Refused, 'mailbox is busy'):
            self.rollback()
        self.assertEqual(self.system.calls, [])

    def test_active_execution_lease_is_not_assumed_quiescent_by_time(self):
        db = self.unified_state/'mailbox/state.sqlite'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute("insert into execution_leases values('active')")
        with self.assertRaisesRegex(release.Refused, 'mailbox is busy'):
            self.activate()
        self.assertFalse(self.transaction.exists())

    def test_busy_after_old_observer_stops_preserves_entries_and_records_recovery(self):
        self.old_observer()
        original = release.entrypoints(self.bin)
        self.system.after_stop = lambda: self.message(self.legacy_state, 'delivery_unknown')
        with self.assertRaisesRegex(release.Refused, 'not idle'):
            self.activate()
        self.assertEqual(release.entrypoints(self.bin), original)
        self.assertEqual(release.read(self.transaction/'transaction.json')['state'], 'recovery_required')
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

    def test_changed_old_seal_and_policy_are_recovery_required_without_overwrite(self):
        self.old_observer()
        self.activate()
        current = release.entrypoints(self.bin)
        self.old.chmod(0o700)
        with self.assertRaisesRegex(release.Refused, 'read-only'):
            self.rollback()
        self.old.chmod(0o500)
        policy = write(self.legacy_state, 'notification-policy.json', '{"mode":"user_input_only"}')
        with self.assertRaisesRegex(release.Refused, 'policies changed'):
            self.rollback()
        self.assertEqual(policy.read_text(), '{"mode":"user_input_only"}')
        self.assertEqual(release.entrypoints(self.bin), current)
        self.assertEqual(release.read(self.transaction/'transaction.json')['state'], 'recovery_required')

    def test_active_selected_process_and_legacy_workflow_block_before_stop(self):
        self.old_observer()
        other = dict(pid=8, command=str(self.old/'runner.py') + ' _worker fixture')
        with patch.object(self.system, 'processes', return_value=[other]):
            with self.assertRaisesRegex(release.Refused, 'active commands'):
                self.activate()
        flow = self.base/'fixture repo/.codex/tasks/workflows/test'
        write(flow, 'state.json', '{"status":"running","owner_pid":1234}')
        with closing(sqlite3.connect(self.legacy_state/'control/state.sqlite')) as con, con:
            con.execute('create table workflows(path text)')
            con.execute('insert into workflows values(?)', (str(flow),))
        with self.assertRaisesRegex(release.Refused, 'workflow has unresolved activity'):
            self.activate()
        self.assertEqual(self.system.calls, [])

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
        with patch.object(release, 'read_private', return_value=row), \
                patch.object(system, 'identity', return_value='verified'), \
                patch.object(system, 'processes', return_value=[process]):
            self.assertEqual(system.observer(Path('/fixture-state'), source)['pid'], 12345)
            for change in (dict(pid_identity='reused'), dict(heartbeat=1),
                           dict(state='launching'), dict(consecutive_failures=1)):
                with patch.object(release, 'read_private', return_value=dict(row, **change)):
                    with self.assertRaises(release.Refused):
                        system.observer(Path('/fixture-state'), source)
            with patch.object(system, 'processes', return_value=[process, process]), self.assertRaises(release.Refused):
                system.observer(Path('/fixture-state'), source)

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
            release.qualify(self.repo,{})
        self.assertEqual([call.args[3] for call in run.call_args_list],['tests','architecture','compile'])
        self.assertIn('ato.py',run.call_args_list[-1].args[0])
        def skipped(argv,root,env,name):
            logged(argv,root,env,name)
            if name=='tests':(root/'.release-logs/tests.log').write_text('Ran 11 tests in 0.1s\n\nOK (skipped=1)\n')
        with patch.object(release,'run_logged',side_effect=skipped),self.assertRaisesRegex(release.Refused,'clean full-test receipt'):
            release.qualify(self.repo,{})


class DependencyToolTests(unittest.TestCase):
    def test_nvm_style_tools_are_explicit_pinned_and_loader_environment_is_clean(self):
        with tempfile.TemporaryDirectory(prefix='ato-host-tools-') as folder:
            base=Path(folder).resolve();tool_dir=base/'host-node/bin';tool_dir.mkdir(parents=True)
            for name in ('node','npm'):
                p=tool_dir/name;p.write_text('#!/bin/sh\nexit 0\n');p.chmod(0o755)
            root=base/'snapshot';root.mkdir()
            env=release.environment(base/'ato',base/'native')
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
