"""Daemon lifecycle fixtures use only temporary files, mocked launches and clocks."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ato_workflow import control
import recovery
import runner
import test_recovery as fixtures


class DaemonResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.context = patch.dict(os.environ, ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'native'))
        self.context.start()
        self.addCleanup(self.context.stop)
        self.controller = self.root/'controller'
        self.controller.mkdir()
        runner.save(self.root/'recovery.json', dict(enabled=True))

    def test_pid_identity_is_optional_only_for_legacy_records(self):
        with patch('recovery.alive', return_value=True), patch('runner.process_identity', return_value='current') as identity:
            self.assertTrue(recovery.pid_alive({'pid': 123}))
            identity.assert_not_called()
            self.assertTrue(recovery.pid_alive(dict(pid=123, pid_identity='current')))
            self.assertFalse(recovery.pid_alive(dict(pid=123, pid_identity='reused')))
        with patch('recovery.alive', return_value=False), patch('runner.process_identity') as identity:
            self.assertFalse(recovery.pid_alive(dict(pid=123, pid_identity='current')))
            identity.assert_not_called()

    def test_controller_and_supervisor_start_fence_reuse_and_preserve_legacy_files(self):
        for module, path in ((control, self.controller/'process.json'), (recovery, self.root/'supervisor.json')):
            for stored, alive, launches in (({}, True, False), ({'pid_identity': 'current'}, True, False),
                    ({'pid_identity': 'reused'}, True, True), ({}, False, True)):
                with self.subTest(module=module.__name__, stored=stored, alive=alive), ExitStack() as stack:
                    runner.save(path, dict(pid=123, **stored))
                    stack.enter_context(patch('ato_workflow.control.directory', return_value=self.controller))
                    stack.enter_context(patch('ato_workflow.control.status', return_value={}))
                    stack.enter_context(patch('recovery.alive', return_value=alive))
                    stack.enter_context(patch('runner.process_identity', return_value='current'))
                    stack.enter_context(patch('runner.runtime_command', return_value=['synthetic-never-executed']))
                    launch = stack.enter_context(patch('recovery.subprocess.Popen', return_value=Mock(pid=456)))
                    module.start() if module is control else module.start(self.root)
                    self.assertEqual(launch.call_count, int(launches))
                    if launches:
                        info = runner.read(path)
                        self.assertEqual(info['pid'], 456)
                        self.assertEqual(info['pid_identity'], 'current')

    def watch(self, module, effects):
        path = self.controller/'process.json' if module is control else self.root/'supervisor.json'
        states = []
        save = runner.save
        def record(target, value):
            if Path(target) == path:
                states.append(dict(value))
            return save(target, value)
        with ExitStack() as stack:
            stack.enter_context(patch('ato_workflow.control.directory', return_value=self.controller))
            stack.enter_context(patch('runner.process_identity', return_value='fixture-start'))
            stack.enter_context(patch('runner.save', side_effect=record))
            stack.enter_context(patch('recovery.signal.signal'))
            sleep = stack.enter_context(patch('recovery.time.sleep'))
            tick = stack.enter_context(patch.object(module, 'tick', side_effect=effects))
            module.watch(interval=0) if module is control else module.watch(self.root, interval=0)
        return runner.read(path), states, sleep, tick

    def test_transient_errors_back_off_then_reset_on_success_in_both_loops(self):
        for module in (control, recovery):
            with self.subTest(module=module.__name__):
                final, states, sleep, tick = self.watch(module, [OSError('temporary lock'), True, OSError('temporary IO'), False])
                self.assertEqual(tick.call_count, 4)
                self.assertEqual([s['consecutive_failures'] for s in states], [1, 0, 1, 0, 0])
                self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 0, 1])
                self.assertTrue(all(s['pid_identity'] == 'fixture-start' for s in states[:-1]))
                self.assertIsNone(final['pid'])
                self.assertIsNone(final['error'])

    def test_continuous_errors_exit_at_limit_with_state_and_log(self):
        for module, logfile in ((control, self.controller/'worker.log'), (recovery, self.root/'supervisor.log')):
            with self.subTest(module=module.__name__):
                final, states, sleep, tick = self.watch(module, OSError('persistent fixture lock'))
                self.assertEqual(tick.call_count, recovery.WATCH_FAILURE_LIMIT)
                self.assertEqual(final['consecutive_failures'], recovery.WATCH_FAILURE_LIMIT)
                self.assertIn('persistent fixture lock', final['error'])
                self.assertEqual(final.get('state', final.get('status')), 'exited')
                self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4, 8])
                self.assertEqual(logfile.read_text().count('persistent fixture lock'), recovery.WATCH_FAILURE_LIMIT)

    def test_keyboard_interrupt_and_system_exit_propagate_in_both_loops(self):
        for module in (control, recovery):
            for interrupt in (KeyboardInterrupt, SystemExit):
                with self.subTest(module=module.__name__, interrupt=interrupt), self.assertRaises(interrupt):
                    self.watch(module, interrupt())

    def test_status_and_summary_use_identity_fenced_liveness(self):
        runner.save(self.controller/'process.json', dict(pid=123, pid_identity='reused'))
        with patch('ato_workflow.control.directory', return_value=self.controller), patch('ato_workflow.control.settings', return_value=None), \
                patch('ato_workflow.control.rows', return_value=[]), patch('recovery.alive', return_value=True), \
                patch('runner.process_identity', return_value='current'):
            self.assertFalse(control.status()['process_alive'])

    def test_transient_heartbeat_write_is_retried_and_not_a_permanent_exit(self):
        save = runner.save
        path = self.controller/'process.json'
        failed = False
        def write(target, value):
            nonlocal failed
            if target == path and value.get('state') == 'watching' and not failed:
                failed = True
                raise OSError('temporary heartbeat IO failure')
            return save(target, value)
        with patch('ato_workflow.control.directory', return_value=self.controller), \
                patch('runner.save', side_effect=write), patch('runner.process_identity', return_value='fixture'), \
                patch('recovery.signal.signal'), patch('recovery.time.sleep') as sleep, \
                patch('ato_workflow.control.tick', side_effect=[True, False]) as tick:
            control.watch(interval=0)
        self.assertEqual(tick.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1])
        self.assertIsNone(runner.read(path)['error'])


class UnknownDeliveryRetryTests(unittest.TestCase):
    setUp = fixtures.RecoveryTests.setUp
    tearDown = fixtures.RecoveryTests.tearDown
    set_state = fixtures.RecoveryTests.set_state
    ledger = fixtures.RecoveryTests.ledger

    def test_supervisor_summary_preserves_legacy_pid_liveness_but_rejects_reuse(self):
        for identity, expected in (({}, True), ({'pid_identity': 'current'}, True), ({'pid_identity': 'reused'}, False)):
            with self.subTest(identity=identity), patch('recovery.alive', return_value=True), \
                    patch('runner.process_identity', return_value='current'), patch('recovery.handler_status', return_value='fixture'):
                runner.save(self.d/'supervisor.json', dict(pid=123, **identity))
                self.assertEqual(recovery.summary(self.d)['supervisor_alive'], expected)
        (self.d/'supervisor.json').unlink()

    def test_error_after_unknown_delivery_never_replays_or_resets_attempts(self):
        original_tick = recovery.tick
        before = (self.d/'state.json').read_bytes()
        calls = 0
        def tick(directory):
            nonlocal calls
            calls += 1
            original_tick(directory)
            if calls == 1:
                raise OSError('observation interrupted after unknown receipt')
            return calls < 3
        with patch('ato_workflow.session_router.send', side_effect=subprocess.TimeoutExpired('synthetic queue', 1)) as send, \
                patch('recovery.tick', side_effect=tick), patch('recovery.signal.signal'), \
                patch('recovery.time.sleep'), patch('runner.process_identity', return_value='fixture'):
            recovery.watch(self.d, interval=0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.ledger()['requests']), 1)
        self.assertEqual(self.ledger()['requests'][0]['state'], 'delivery_unknown')
        self.assertEqual((self.d/'state.json').read_bytes(), before)
