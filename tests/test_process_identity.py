"""UTC start identities and comparison-only host-local migration, without daemons."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import ato_core
import ato_process
import ato_release_lib
import hub_sandbox
import pi_session_coordinator
import recovery
from scripts import release_snapshot as release

UTC_ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'}
LOCAL_ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}


def start_identity(pid, environment):
    value = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                                    env=environment, text=True, timeout=2).strip()
    return hashlib.sha256(value.encode()).hexdigest()


class ProcessIdentityTests(unittest.TestCase):
    def test_batch2_real_macos_zombie_is_absent_before_wait(self):
        child = subprocess.Popen(["/bin/sh", "-c", "sleep .25"])
        self.addCleanup(child.wait, 5)
        birth = ato_process.identity(child.pid)
        self.assertIsNotNone(birth)
        until = time.monotonic() + 3
        while time.monotonic() < until:
            state = subprocess.check_output(["/bin/ps", "-p", str(child.pid), "-o", "stat="],
                                            env=UTC_ENV, text=True).strip()
            if state.startswith("Z"):
                break
            time.sleep(.03)
        self.assertTrue(state.startswith("Z"), state)
        self.assertIsNone(child.returncode)
        with patch.object(ato_process, "_observe", side_effect=ProcessLookupError):
            self.assertEqual(ato_process.disposition(child.pid, birth), "absent")

    def test_real_current_pid_has_one_UTC_identity_across_timezones_and_locales(self):
        pid = os.getpid()
        expected = start_identity(pid, UTC_ENV)
        observer = release.System()
        for environment in (
                {'TZ': 'America/Los_Angeles', 'LC_ALL': 'C'},
                {'TZ': 'Asia/Shanghai', 'LC_ALL': 'C'},
                {'TZ': 'UTC', 'LC_ALL': 'C'},
                {'TZ': 'America/Los_Angeles', 'LC_ALL': 'zh_CN.UTF-8',
                 'LC_TIME': 'zh_CN.UTF-8', 'LANG': 'zh_CN.UTF-8'}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                self.assertEqual(ato_core.process_identity(pid), expected)
                self.assertEqual(hub_sandbox.identity(pid), expected)
                self.assertEqual(observer.identity(pid), expected)

    def test_real_old_process_json_matches_core_recovery_and_observer_without_rewrite(self):
        pid = os.getpid()
        legacy = start_identity(pid, LOCAL_ENV)
        canonical = start_identity(pid, UTC_ENV)
        with tempfile.TemporaryDirectory(prefix='ato-identity-') as folder:
            root = Path(folder).resolve()
            state = root/'state';(state/'control').mkdir(parents=True)
            source = dict(python=str(root/'python'), root=str(root/'source'), surface='legacy')
            process = dict(pid=pid, command=source['python']+' '+source['root']+'/runner.py _controller')
            path = state/'control/process.json'
            row = dict(pid=pid, pid_identity=legacy, heartbeat=time.time(), state='watching', consecutive_failures=0)
            path.write_text(json.dumps(row));before = path.read_bytes()
            observer = release.System()
            with patch.dict(os.environ, {'TZ': 'America/Los_Angeles', 'LC_ALL': 'zh_CN.UTF-8'}), \
                    patch.object(observer, 'processes', return_value=[process]), \
                    patch.object(ato_core, '_legacy_localtime_identity', wraps=ato_core._legacy_localtime_identity) as fallback:
                info = ato_core.read(path)
                self.assertTrue(ato_core.pid_alive(info))
                self.assertTrue(recovery.pid_alive(info))
                self.assertEqual(observer.observer(state, source)['pid_identity'], legacy)
                self.assertEqual(fallback.call_count, 2 if legacy != canonical else 0)
                self.assertEqual(path.read_bytes(), before)
                for expected in (canonical, legacy):
                    self.assertTrue(observer.matches_identity(pid, expected))
                reused = hashlib.sha256(b'another process start').hexdigest()
                self.assertFalse(ato_core.pid_alive(dict(pid=pid, pid_identity=reused)))
                self.assertFalse(recovery.pid_alive(dict(pid=pid, pid_identity=reused)))
                self.assertFalse(observer.matches_identity(pid, reused))
                path.write_text(json.dumps(dict(row, pid_identity=reused)))
                with self.assertRaisesRegex(release.Refused, 'pid_identity mismatch'):
                    observer.observer(state, source)
                path.write_bytes(before)
            self.assertEqual(path.read_bytes(), before)

    def test_UTC_match_never_calls_legacy_and_mocked_identity_never_reaches_real_ps(self):
        pid = os.getpid();canonical = start_identity(pid, UTC_ENV)
        with patch.object(ato_core, '_legacy_localtime_identity', side_effect=AssertionError('no fallback')):
            self.assertTrue(ato_core.pid_alive(dict(pid=pid, pid_identity=canonical)))
        for target in ('injected', 'public', 'recovery_facade'):
            with self.subTest(target=target), ExitStack() as stack:
                stack.enter_context(patch.object(ato_core, '_legacy_localtime_identity', side_effect=AssertionError('mock bypass')))
                stack.enter_context(patch.object(ato_core.subprocess, 'run', side_effect=AssertionError('real ps forbidden')))
                if target == 'injected':
                    identity = Mock(return_value='new-start')
                    matched = ato_core.pid_alive(dict(pid=123, pid_identity='old-start'),
                                                 alive_fn=lambda _: True, identity_fn=identity)
                elif target == 'public':
                    identity = stack.enter_context(patch.object(ato_core, 'process_identity', return_value='new-start'))
                    matched = ato_core.pid_alive(dict(pid=123, pid_identity='old-start'), alive_fn=lambda _: True)
                else:
                    identity = stack.enter_context(patch('runner.process_identity', return_value='new-start'))
                    stack.enter_context(patch('recovery.alive', return_value=True))
                    matched = recovery.pid_alive(dict(pid=123, pid_identity='old-start'))
                self.assertFalse(matched)
                identity.assert_called_once_with(123)

    def test_dead_or_unversioned_records_preserve_short_circuit_without_ps(self):
        with patch.object(ato_core, 'process_identity', side_effect=AssertionError('identity not needed')), \
                patch.object(ato_core, '_legacy_localtime_identity', side_effect=AssertionError('fallback not needed')):
            self.assertFalse(ato_core.pid_alive(dict(pid=123, pid_identity='old'), alive_fn=lambda _: False))
            self.assertTrue(ato_core.pid_alive(dict(pid=123), alive_fn=lambda _: True))

    def test_core_fallback_only_matches_host_local_start_and_never_rewrites_identity(self):
        utc = 'Sun Nov 22 01:00:00 2026';local = 'Sun Nov 22 09:00:00 2026'
        legacy = hashlib.sha256(local.encode()).hexdigest()
        def ps(argv, *, env, **options):
            self.assertEqual(argv, ['/bin/ps', '-p', '123', '-o', 'lstart='])
            self.assertIn(env, (UTC_ENV, LOCAL_ENV))
            return SimpleNamespace(stdout=utc if 'TZ' in env else local)
        with patch.object(ato_core.subprocess, 'run', side_effect=ps) as calls:
            self.assertTrue(ato_core.pid_alive(dict(pid=123, pid_identity=legacy), alive_fn=lambda _: True))
            self.assertEqual([c.kwargs['env'] for c in calls.call_args_list], [UTC_ENV, LOCAL_ENV])
            calls.reset_mock()
            self.assertEqual(ato_core.process_identity(123), hashlib.sha256(utc.encode()).hexdigest())
            self.assertEqual([c.kwargs['env'] for c in calls.call_args_list], [UTC_ENV])
            self.assertFalse(ato_core.pid_alive(dict(pid=123, pid_identity='reused'), alive_fn=lambda _: True))

    def test_observer_identity_overrides_never_bypass_mock_to_real_ps(self):
        class OfflineObserver(release.System):
            def identity(self, pid):return 'mocked'
        for mode in ('instance', 'subclass', 'class'):
            with self.subTest(mode=mode), ExitStack() as stack:
                stack.enter_context(patch.object(ato_release_lib, 'command', side_effect=AssertionError('real ps forbidden')))
                observer = OfflineObserver() if mode == 'subclass' else release.System()
                if mode == 'instance':stack.enter_context(patch.object(observer, 'identity', return_value='mocked'))
                if mode == 'class':stack.enter_context(patch.object(release.System, 'identity', return_value='mocked'))
                self.assertTrue(observer.matches_identity(123, 'mocked'))
                self.assertFalse(observer.matches_identity(123, 'other'))
                with patch('os.kill') as kill:
                    with self.assertRaisesRegex(release.Refused, 'identity changed'):
                        observer.stop(dict(pid=123, pid_identity='other', command='fixture'))
                    kill.assert_not_called()

    def test_observer_stop_accepts_legacy_before_signal_and_waits_for_exit_without_real_signals(self):
        utc = 'Sun Nov 22 01:00:00 2026';local = 'Sun Nov 22 09:00:00 2026'
        legacy = hashlib.sha256(local.encode()).hexdigest();running = [True]
        def ps(argv, *, env):
            self.assertEqual(argv, ['/bin/ps', '-p', '123', '-o', 'lstart='])
            self.assertIn(env, (UTC_ENV, LOCAL_ENV))
            return (utc if 'TZ' in env else local) if running[0] else ''
        observer = release.System();process = dict(pid=123, command='fixture-controller')
        with patch.object(ato_release_lib, 'command', side_effect=ps), \
                patch.object(observer, 'processes', side_effect=[[process], []]), \
                patch('os.kill', side_effect=lambda *_: running.__setitem__(0, False)) as kill:
            observer.stop(dict(process, pid_identity=legacy))
            kill.assert_called_once_with(123, signal.SIGTERM)
        with patch.object(ato_release_lib, 'command', side_effect=ps), patch('os.kill') as kill:
            with self.assertRaisesRegex(release.Refused, 'identity changed'):
                observer.stop(dict(process, pid_identity='reused'))
            kill.assert_not_called()

    def test_all_owned_group_ps_calls_use_exact_UTC_environment(self):
        with patch.object(hub_sandbox.subprocess, 'check_output', return_value=b'canonical start') as ps:
            hub_sandbox.identity(123)
            self.assertEqual(ps.call_args.kwargs['env'], UTC_ENV)
        with patch.object(hub_sandbox.subprocess, 'check_output', return_value='123 123 S\n') as ps, \
                patch.object(hub_sandbox, 'identity', return_value='canonical-start'):
            self.assertEqual(hub_sandbox.group_members(123), {123: 'canonical-start'})
            self.assertEqual(ps.call_args.kwargs['env'], UTC_ENV)
        host = SimpleNamespace(proc=SimpleNamespace(pid=123))
        with patch('subprocess.check_output', return_value='123 123 Sun Nov 22 01:00:00 2026\n') as ps:
            members = pi_session_coordinator.PiCoordinator._production_members(host)
            self.assertEqual(members, {123: 'Sun Nov 22 01:00:00 2026'})
            self.assertEqual(ps.call_args.kwargs['env'], UTC_ENV)
