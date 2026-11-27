"""Real local subprocess regressions; all state and executables are temporary fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from ato import executor, process, product
from ato.adapters.base import run_bounded_subprocess
from ato.spec import TaskError
from ato.store import Store
from test_core_and_store import init_repo, make_spec
from test_execution_and_cli import run_cli

ROOT = Path(__file__).resolve().parents[1]


class SubprocessRegressions(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, {"ATO_HOME": str(self.root / "ato"), "CODEX_HOME": str(self.root / "codex"),
                                     "ATO_SUBMISSION_KIND": "agy"})
        env.start()
        self.addCleanup(env.stop)

    def test_large_stdin_consumed_ignored_exited_and_timed_out(self):
        prompt = "x" * (256 * 1024)
        for code, expected in (("import sys; print(len(sys.stdin.read()))", "OK"),
                               ("import time; time.sleep(.1)", "OK"), ("pass", "OK"),
                               ("import time; time.sleep(60)", "TIMEOUT")):
            with self.subTest(code=code):
                children = []
                started = time.monotonic()
                rc, out, err, result = run_bounded_subprocess(
                    [sys.executable, "-c", code], cwd=self.root, env=dict(os.environ), prompt=prompt,
                    timeout=.4, on_child=lambda pid, birth: children.append((pid, birth)))
                self.assertEqual(result, expected)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(children[-1], (None, None))
                self.assertIsInstance(children[0][0], int)
                if "stdin.read" in code:
                    self.assertEqual(out.strip(), b"262144")

    def test_exit_probe_and_cleanup_precede_release_of_child_record(self):
        events = []
        proc = Mock(pid=123, returncode=0)
        proc.poll.side_effect = AssertionError("poll must not reap before group sweep")
        def probe(child):
            self.assertIs(child, proc)
            events.append("probe")
            return True
        def cleanup(child):
            self.assertIs(child, proc)
            events.append("cleanup")
        def on_child(pid, birth):
            events.append("spawn" if pid is not None else "clear")
        with patch("ato.adapters.base.subprocess.Popen", return_value=proc), \
             patch.object(process, "identity", return_value="birth"), \
             patch.object(process, "probe_exited", side_effect=probe), \
             patch("ato.adapters.base.terminate_popen", side_effect=cleanup):
            result = run_bounded_subprocess(["fixture"], cwd=self.root, env={}, on_child=on_child)
        self.assertEqual(result[-1], "OK")
        self.assertEqual(events, ["spawn", "probe", "cleanup", "clear"])

    def test_stop_callback_exception_and_interrupt_always_cleanup(self):
        for exc_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(exc=exc_type):
                events = []
                with self.assertRaises(exc_type):
                    run_bounded_subprocess(
                        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.root, env=dict(os.environ),
                        stop_check=Mock(side_effect=exc_type("stop poll failed")),
                        on_child=lambda pid, birth: events.append((pid, birth)))
                self.assertEqual(events[-1], (None, None))
                with self.assertRaises(ProcessLookupError):
                    os.kill(events[0][0], 0)

    def test_normal_exit_sweeps_lingering_descendants(self):
        marker = self.root / "descendant"
        script = (
            "import subprocess, sys; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(marker)!r}).write_text(str(p.pid))"
        )
        child_pid = None
        try:
            result = run_bounded_subprocess([sys.executable, "-c", script], cwd=self.root, env=dict(os.environ), timeout=5)
            child_pid = int(marker.read_text())
            self.assertEqual(result[-1], "OK")
            self.assertFalse(self.live(child_pid), "normal exit must sweep descendants before reaping leader")
        finally:
            if child_pid:
                self.kill(child_pid)

    def test_cleanup_failure_keeps_record_and_fails_closed(self):
        events = []
        real_terminate = process.terminate_popen
        processes = []
        def fail(proc):
            processes.append(proc)
            raise PermissionError("synthetic cleanup failure")
        try:
            with patch("ato.adapters.base.terminate_popen", side_effect=fail), self.assertRaises(TaskError) as caught:
                run_bounded_subprocess([sys.executable, "-c", "import time; time.sleep(60)"],
                                       cwd=self.root, env=dict(os.environ), stop_check=lambda: True,
                                       on_child=lambda pid, birth: events.append((pid, birth)))
            self.assertEqual(caught.exception.code, "CLEANUP_BLOCKED")
            self.assertEqual(len(events), 1)
        finally:
            for proc in processes:
                real_terminate(proc)

    @staticmethod
    def live(pid):
        # Allow the OS to finish delivery/reaping after a successful group sweep.
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            try:
                if process.observe(pid)[1]:
                    return False
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return True
            time.sleep(.02)

    @staticmethod
    def kill(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def background(self, kind):
        repo = init_repo(self.root / ("repo-" + kind))
        marker = self.root / (kind + ".pids")
        child = self.root / (kind + "-child.py")
        child.write_text(
            "import os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])\n"
            f"Path({str(marker)!r}).write_text(str(os.getpid()) + ' ' + str(p.pid))\n"
            "time.sleep(60)\n")
        spec_path = make_spec(repo, self.root / (kind + "-spec.json"), worker_contract={
            "objective": "Fixture", "scope": "local", "authorization": "local", "max_attempts": 1,
            "acceptance": [{"id": "check", "kind": "command", "description": "Fixture",
                            "checks": [[sys.executable, str(child)]] if kind == "check" else [["true"]]}],
        }, **({"reviewer": {"agent": {"kind": "codex"}}} if kind == "reviewer" else {}))
        store = Store()
        with patch.object(executor, "_spawn_background_run"):
            status = executor.submit(store, repo, spec_path)
        task = store.get_task(status["task_id"])
        script = """
import os, sys
from pathlib import Path
from ato import executor
from ato.store import Store
from ato.adapters.base import WorkerResult, ReviewResult, run_bounded_subprocess
kind, child, task_id, run_id, db = sys.argv[1:]
def invoke(req):
    return run_bounded_subprocess([sys.executable,child], cwd=req.worktree, env=dict(os.environ),
                                 stop_check=req.stop_check,on_child=req.on_child,timeout=60)
def worker(req):
    if kind == 'worker':
        invoke(req)
    return WorkerResult('completed','fixture')
def reviewer(req):
    invoke(req)
    return ReviewResult(False,'STOPPED')
executor.execute_run(Store(Path(db)),task_id,run_id,worker_runner=worker,reviewer_runner=reviewer)
"""
        runner = subprocess.Popen([sys.executable, "-c", script, kind, str(child), task["id"],
                                   task["active_run_id"], str(store.db_file)], cwd=ROOT,
                                  env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        def cleanup():
            if marker.exists():
                for pid in map(int, marker.read_text().split()):
                    self.kill(pid)
            process.terminate_popen(runner, grace_seconds=.1)
        self.addCleanup(cleanup)
        end = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < end and runner.poll() is None:
            time.sleep(.05)
        self.assertTrue(marker.exists(), "background fixture did not launch child")
        pids = list(map(int, marker.read_text().split()))
        return store, task, runner, pids

    def test_background_stop_kills_worker_reviewer_and_check_groups(self):
        for kind in ("worker", "reviewer", "check"):
            with self.subTest(kind=kind):
                store, task, runner, pids = self.background(kind)
                row = store.get_run(task["active_run_id"])
                self.assertEqual(row["child_pid"], pids[0])
                self.assertEqual(os.getpgid(pids[0]), pids[0])
                self.assertNotEqual(os.getpgid(pids[0]), os.getpgid(runner.pid))
                code, result = run_cli(["stop", task["id"]], store=store)
                self.assertEqual(code, 0, result)
                runner.wait(timeout=10)
                self.assertEqual(result["data"]["status"], "stopped")
                self.assertTrue(all(not self.live(pid) for pid in pids))
                row = store.get_run(task["active_run_id"])
                self.assertIsNone(row["child_pid"])
                self.assertIsNone(row["child_pid_identity"])

    def test_sigterm_gracefully_cleans_child_group(self):
        store, task, runner, pids = self.background("worker")
        runner.send_signal(signal.SIGTERM)
        runner.wait(timeout=10)
        self.assertEqual(store.get_task(task["id"])["status"], "stopped")
        self.assertTrue(all(not self.live(pid) for pid in pids))

    def test_stop_rechecks_late_child_and_fails_closed(self):
        repo = init_repo(self.root / "repo")
        sp = make_spec(repo, self.root / "spec.json")
        store = Store()
        with patch.object(executor, "_spawn_background_run"):
            result = executor.submit(store, repo, sp)
        task = store.get_task(result["task_id"])
        rid = task["active_run_id"]
        store.update_run(rid, pid=10, pid_identity="executor", child_pid=11, child_pid_identity="first")
        seen = []
        def stop(pid, birth):
            seen.append(pid)
            if pid == 10:
                store.update_run(rid, child_pid=12, child_pid_identity="late")
            return True
        with patch.object(process, "stop_pid_group", side_effect=stop):
            executor.request_stop(store, task["id"])
        self.assertEqual(seen, [11, 10, 12])
        store.update_run(rid, phase="running", pid=10, pid_identity="executor", child_pid=11, child_pid_identity="first")
        with patch.object(process, "stop_pid_group", return_value=False), self.assertRaises(TaskError) as caught:
            executor.request_stop(store, task["id"])
        self.assertEqual(caught.exception.code, "CLEANUP_BLOCKED")
        self.assertEqual(store.get_task(task["id"])["reason"], "CLEANUP_BLOCKED")
        self.assertEqual(store.get_run(rid)["child_pid"], 11)


if __name__ == "__main__":
    unittest.main()
