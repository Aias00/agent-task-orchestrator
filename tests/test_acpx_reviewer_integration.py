"""Offline ATO journal tests, repeated through the real acpx fork when selected."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from ato import config, executor, process
from ato.adapters import acpx
from ato.adapters.base import WorkerResult
from ato.spec import TaskError, normalize_spec
from ato.store import Store
from test_core_and_store import init_repo, make_spec


FIXTURES = Path(__file__).with_name("fixtures")
RUNTIME = os.environ.get("ATO_TEST_ACPX_RUNTIME")


def _worker(_request):
    return WorkerResult(status="completed", summary="synthetic worker")


def _submit(repo, spec):
    return executor.submit(Store(), repo, spec, foreground=True, worker_runner=_worker)


class _ReviewCases:
    runtime = None

    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory(prefix="ato-acpx-"))).resolve()
        self.enterContext(patch.dict(os.environ, {
            "ATO_HOME": str(self.base / "ato"), "CODEX_HOME": str(self.base / "unused-native-home"),
            "NODE_OPTIONS": "--synthetic-invalid-option", "GITHUB_TOKEN": "synthetic-ambient-secret",
        }))
        self.enterContext(patch.dict(os.environ))
        for key in ("OPENAI_API_KEY", "CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID"):
            os.environ.pop(key, None)
        self.store = Store()
        self.repo = init_repo(self.base / "repo")
        self.spec = make_spec(self.repo, self.base / "spec.json", reviewer={"agent": {"kind": "codex_acpx"}})
        self.launcher = self.base / "launcher with spaces"
        self.launcher.write_text(f"#!{sys.executable}\n" + (FIXTURES / "acpx_peer.py").read_text())
        self.launcher.chmod(0o700)
        if self.runtime:
            runtime = Path(self.runtime).expanduser().resolve()
            node = os.environ.get("ATO_TEST_ACPX_NODE") or shutil.which("node")
            self.assertTrue(runtime.is_file() and node, "The selected fork module and Node must be available.")
        else:
            runtime = self.base / "synthetic-runtime.js"
            runtime.touch()
            node = sys.executable
            self.enterContext(patch.object(acpx, "_bridge_argv", return_value=[
                sys.executable, "-B", str(FIXTURES / "acpx_bridge.py"),
            ]))
        config.save_settings({"acpx_reviewer": {"node": node, "runtime": str(runtime), "launcher": str(self.launcher)}})
        self.addCleanup(self.sweep_fixture)

    def sweep_fixture(self):
        for task in self.store.list_tasks():
            unresolved = self.store.acpx_review(task["id"], unresolved=True)
            if unresolved and not unresolved["bridge_closed"]:
                acpx.reconcile_cleanup(self.store, unresolved, stop=True)

    def scenario(self, value):
        (self.base / "fixture.json").write_text(json.dumps({
            "scenario": value, "db": str(self.store.db_file), "temp": str(self.base),
        }))

    def events(self, name):
        path = self.base / "seen.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [row for row in rows if row["event"] == name]

    def wait_for_prompt(self, count=1):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            events = self.events("prompt")
            if len(events) >= count:
                return events[count - 1]
            time.sleep(0.02)
        self.fail("The synthetic peer did not receive a prompt.")

    def assert_clean(self, view, count=1):
        row = self.store.acpx_review(view["task_id"])
        self.assertTrue(row["bridge_closed"], row)
        self.assertEqual(row["cleanup"], "verified_gone")
        for item in (row["bridge"], row["launch"]):
            self.assertTrue(process.owned_group_exited(item["pid"], item["birth"]), item)
        self.assertEqual(len(self.events("prompt")), count)
        self.assertFalse((config.runtime_dir() / row["task_id"] / row["run_id"] /
                          f"review-{row['round']}" / "native").exists())
        return row

    def test_normal_review_records_before_send_and_keeps_ato_checks(self):
        self.scenario("normal")
        view = _submit(self.repo, self.spec)
        self.assertEqual(view["status"], "completed", view)
        self.assertEqual(view["review"]["verdict"], "approve")
        self.assertEqual([check["state"] for check in view["checks"]], ["passed"])
        self.assertEqual(view["acpx_reviewer"]["delivery"], "finished")
        self.assertTrue(self.events("prompt")[0]["committed"])
        row = self.assert_clean(view)
        self.assertEqual(row["receipt"]["transport"], "response")
        self.assertEqual(row["dispatch"]["launch_id"], row["launch"]["launch_id"])
        with sqlite3.connect(self.store.db_file) as con:
            self.assertNotIn("opaque-peer-session-with-private-value", "\n".join(con.iterdump()))
        again = executor.recheck(self.store, view["task_id"])
        self.assertEqual(again["status"], "completed", again)
        self.assert_clean(again, count=2)

    def test_stop_revokes_ack_even_after_dispatch_commit(self):
        self.scenario("normal")
        real_ack = Store.ack_acpx

        def revoke_before_ack(store, request_id, record, send):
            if record == "dispatch_json":
                task_id = store.list_tasks()[0]["id"]
                self.assertIsNotNone(store.acpx_review(task_id)["dispatch"])
                store.update_task(task_id, stop_requested=True)
            return real_ack(store, request_id, record, send)

        with patch.object(Store, "ack_acpx", revoke_before_ack):
            view = _submit(self.repo, self.spec)
        self.assertEqual(view["status"], "stopped", view)
        self.assertTrue(view["acpx_reviewer"]["dispatch_recorded"])
        self.assertEqual(view["acpx_reviewer"]["delivery"], "unsent")
        self.assert_clean(view, count=0)

    def test_rejected_authorization_leaves_no_dispatch_record(self):
        self.scenario("normal")
        with patch.object(Store, "authorize_acpx_dispatch", return_value=False):
            view = _submit(self.repo, self.spec)
        self.assertEqual(view["reason"], "ACPX_DISPATCH_REJECTED", view)
        self.assertFalse(view["acpx_reviewer"]["dispatch_recorded"])
        self.assertEqual(view["acpx_reviewer"]["delivery"], "unsent")
        self.assert_clean(view, count=0)

    def test_post_write_drop_is_blocked_even_after_stop_and_has_no_replay(self):
        self.scenario("drop")
        view = _submit(self.repo, self.spec)
        self.assertEqual(view["status"], "blocked", view)
        self.assertEqual(view["reason"], "ACPX_DELIVERY_UNKNOWN", view)
        task_id = view["task_id"]
        self.assert_clean(view)
        stopped = executor.request_stop(self.store, task_id)
        self.assertEqual(stopped["status"], "blocked")
        self.assertEqual(stopped["reason"], "ACPX_DELIVERY_UNKNOWN")
        for action in (
            lambda: executor.resume(self.store, task_id, foreground=True, worker_runner=_worker),
            lambda: executor.recheck(self.store, task_id, checks_only=True),
            lambda: executor.cleanup(self.store, task_id),
        ):
            with self.assertRaises(TaskError) as caught:
                action()
            self.assertEqual(caught.exception.code, "ACPX_DELIVERY_UNKNOWN")
        self.assertEqual(len(self.events("prompt")), 1)
        self.assertEqual(len(self.store.list_runs(task_id)), 1)

    def test_stop_can_acknowledge_cancel_and_separately_verify_exit(self):
        self.scenario("hold")
        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(_submit, self.repo, self.spec)
            self.wait_for_prompt()
            task_id = self.store.list_tasks()[0]["id"]
            try:
                with self.assertRaises(TaskError) as caught:
                    executor.recheck(self.store, task_id)
                self.assertEqual(caught.exception.code, "BUSY")
            finally:
                executor.request_stop(self.store, task_id)
            view = active.result(timeout=8)
        self.assertEqual(view["status"], "stopped", view)
        self.assertEqual(view["acpx_reviewer"]["delivery"], "finished")
        self.assertTrue(view["acpx_reviewer"]["cancel_acknowledged"])
        self.assertTrue(self.events("cancel"))
        self.assert_clean(view)

    def test_stop_during_recheck_settles_that_run(self):
        self.scenario("normal")
        view = _submit(self.repo, self.spec)
        self.assertEqual(view["status"], "completed", view)
        self.scenario("hold")
        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(executor.recheck, self.store, view["task_id"])
            self.wait_for_prompt(count=2)
            stopped = executor.request_stop(self.store, view["task_id"])
            view = active.result(timeout=8)
        self.assertEqual(stopped["status"], "stopped", stopped)
        self.assertEqual(view["status"], "stopped", view)
        self.assertEqual(view["run_history"][-1]["outcome"], "stopped")
        self.assert_clean(view, count=2)

    def _stop_orphan(self, *, remove_root=False):
        self.scenario("orphan_descendant" if remove_root else "orphan")
        owner = multiprocessing.get_context("fork").Process(target=_submit, args=(self.repo, self.spec))
        owner.start()
        try:
            event = self.wait_for_prompt()
            task_id = self.store.list_tasks()[0]["id"]
            row = self.store.acpx_review(task_id)
            self.assertEqual(row["bridge"]["pid"], event["bridge"])
            self.assertEqual(process.identity(event["bridge"]), row["bridge"]["birth"])
            owner.kill()
            owner.join(timeout=3)
            os.kill(event["bridge"], signal.SIGKILL)
            deadline = time.monotonic() + 3
            while not process.owned_group_exited(event["bridge"], row["bridge"]["birth"]):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            self.assertEqual(process.identity(event["pid"]), row["launch"]["birth"])
            if remove_root:
                descendant = self.events("descendant")[0]["pid"]
                birth = row["launch"]["witnesses"][str(descendant)]
                self.assertEqual(process.identity(descendant), birth)
                os.kill(event["pid"], signal.SIGKILL)
                deadline = time.monotonic() + 3
                while process.disposition(event["pid"], row["launch"]["birth"]) == "live":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
            abandoned = executor.task_status_view(self.store, task_id)
            self.assertEqual(abandoned["reason"], "ACPX_DELIVERY_UNKNOWN")
            self.assertFalse(abandoned["acpx_reviewer"]["execution_cleanup_verified"])
            stopped = executor.request_stop(Store(), task_id)
            self.assertEqual(stopped["status"], "blocked", stopped)
            self.assertEqual(stopped["reason"], "ACPX_DELIVERY_UNKNOWN")
            self.assertIsNone(stopped["acpx_reviewer"]["cancel_acknowledged"])
            self.assert_clean(stopped)
            if remove_root:
                self.assertEqual(process.disposition(descendant, birth), "absent")
        finally:
            if owner.is_alive():
                owner.kill()
                owner.join(timeout=3)
            owner.close()

    def test_stop_after_bridge_and_run_owner_exit_sweeps_verified_orphan(self):
        self._stop_orphan()

    def test_mutation_blocks_ato_verdict_even_after_normal_response(self):
        self.scenario("mutate")
        view = _submit(self.repo, self.spec)
        self.assertEqual(view["reason"], "MUTATION_VIOLATION", view)
        self.assertIsNone(view["review"])
        self.assertEqual(view["acpx_reviewer"]["delivery"], "finished")
        self.assert_clean(view)


class AcpxReviewerSyntheticTests(_ReviewCases, unittest.TestCase):
    def test_host_configuration_rejects_relative_and_worktree_paths(self):
        settings = config.load_settings()
        settings["acpx_reviewer"]["launcher"] = "./launcher"
        config.save_settings(settings)
        self.assertIsNone(acpx.host_configuration(self.repo))
        settings["acpx_reviewer"]["launcher"] = str(self.repo / "launcher")
        shutil.copy2(self.launcher, self.repo / "launcher")
        config.save_settings(settings)
        self.assertIsNone(acpx.host_configuration(self.repo))

    def test_spec_rejects_worker_kind_and_nonfresh_reviewer(self):
        raw = json.loads(self.spec.read_text())
        raw["worker_contract"]["agent"] = {"kind": "codex_acpx"}
        self.spec.write_text(json.dumps(raw))
        with self.assertRaises(TaskError) as caught:
            normalize_spec(self.repo, self.spec)
        self.assertEqual(caught.exception.code, "INVALID_SPEC")
        del raw["worker_contract"]["agent"]
        raw["reviewer"]["session_mode"] = "require_resume"
        self.spec.write_text(json.dumps(raw))
        with self.assertRaises(TaskError) as caught:
            normalize_spec(self.repo, self.spec)
        self.assertEqual(caught.exception.code, "INVALID_SPEC")

    def test_cleanup_does_not_signal_reused_pid_or_unverified_group(self):
        pid, old_birth = 431, "old-birth"
        with (patch.object(process, "disposition", return_value="reused"),
              patch.object(process, "group_members", return_value={pid: "new-birth"}),
              patch.object(process, "group_birth", return_value="new-birth"),
              patch.object(process.os, "killpg") as killpg):
            self.assertFalse(process.stop_owned_group(pid, old_birth))
        killpg.assert_not_called()


if RUNTIME:
    class AcpxReviewerForkIntegrationTests(_ReviewCases, unittest.TestCase):
        runtime = RUNTIME

        def test_native_mode_must_be_advertised_before_prompt(self):
            self.scenario("no_mode")
            view = _submit(self.repo, self.spec)
            self.assertEqual(view["reason"], "ACPX_READ_ONLY_UNVERIFIED", view)
            self.assertFalse(view["acpx_reviewer"]["dispatch_recorded"])
            self.assert_clean(view, count=0)

        def test_permission_request_is_denied_and_verdict_rejected(self):
            self.scenario("permission")
            view = _submit(self.repo, self.spec)
            self.assertEqual(view["reason"], "ACPX_PERMISSION_DENIED", view)
            self.assertTrue(self.events("permission_denied"))
            self.assert_clean(view)

        def test_correlated_rpc_error_is_distinct_from_unknown_delivery(self):
            self.scenario("rpc_error")
            view = _submit(self.repo, self.spec)
            self.assertEqual(view["reason"], "ACPX_REVIEW_FAILED", view)
            self.assertEqual(view["acpx_reviewer"]["delivery"], "failed")
            self.assertEqual(view["acpx_reviewer"]["transport"], "rpc_error")
            self.assert_clean(view)

        def test_stop_uses_verified_group_member_after_root_exits(self):
            self._stop_orphan(remove_root=True)
