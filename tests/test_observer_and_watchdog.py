"""Tests for Watchdog sampler/alerts and non-blocking Observer notification dispatch."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from ato import config, executor, observer, watchdog
from ato.adapters.base import WorkerResult
from ato.store import Store
from test_core_and_store import init_repo, make_spec


class ObserverAndWatchdogTests(unittest.TestCase):
    def test_watchdog_reduce_sample_alerts_diff_error_and_baseline(self):
        base_sample = {
            "task_id": "task-" + "a" * 32,
            "revision": 2,
            "run_id": "run-" + "b" * 32,
            "sampled_at": 1000.0,
            "status": "running",
            "reason": None,
            "execution": {"state": "unknown", "run_id": "run-" + "b" * 32},
            "stage": {"steps": [{"stage_id": "implement", "step_id": "worker", "state": "running", "attempts": 3}],
                      "remaining_stage_ids": [], "elapsed_seconds": 10},
            "run_history": [{"run_id": "run-" + "b" * 32, "kind": "initial", "phase": "running", "outcome": None}],
            "created_at": 900.0,
            "evidence_complete": False,
            "diff_error": "DIFF_UNAVAILABLE",
            "violations": [],
            "progress": "p1",
            "baseline": False,
        }
        # First sample: REPEATED_ATTEMPTS fires; EXECUTION_UNVERIFIED needs 2 consecutive samples.
        # DIFF_UNAVAILABLE does NOT set sample_error (P1-4).
        st1, al1 = watchdog.reduce_sample(None, [], base_sample)
        self.assertIsNone(st1["sample_error"])
        self.assertEqual(st1["diff_error"], "DIFF_UNAVAILABLE")
        self.assertEqual([a["code"] for a in al1], ["REPEATED_ATTEMPTS"])

        # Second consecutive sample: EXECUTION_UNVERIFIED confirms!
        sample2 = dict(base_sample, sampled_at=1060.0)
        st2, al2 = watchdog.reduce_sample(st1, al1, sample2)
        self.assertEqual({a["code"] for a in al2}, {"REPEATED_ATTEMPTS", "EXECUTION_UNVERIFIED"})

        # Historical blocked task at observer startup gets baseline_only (no alert spam).
        hist = dict(base_sample, status="blocked", reason="WORKER_BLOCKED",
                    execution={"state": "idle", "run_id": "run-" + "b" * 32},
                    stage={"steps": [], "remaining_stage_ids": []},
                    run_history=[{"run_id": "run-" + "b" * 32, "kind": "initial", "phase": "settled", "outcome": "blocked"}],
                    baseline=True)
        _, al_hist = watchdog.reduce_sample(None, [], hist)
        self.assertEqual(len(al_hist), 1)
        self.assertEqual(al_hist[0]["code"], "UNHANDLED_FAILURE")
        self.assertEqual(al_hist[0]["notification_state"], "baseline_only")

    def test_p0_2_observer_dispatches_watchdog_and_completion_without_blocking(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            tid = "11111111-2222-3333-4444-555555555555"
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato"), "CODEX_HOME": str(base / "codex"),
                                         "CODEX_THREAD_ID": tid}), \
                    patch("ato.codex_notify.session_path", return_value=str(base / "rollout.jsonl")):
                repo = init_repo(base / "repo")
                sp = make_spec(repo, base / "spec.json")
                store = Store()
                status = executor.submit(
                    store, repo, sp, foreground=True,
                    worker_runner=lambda req: WorkerResult(status="completed", summary="done"),
                )
                task_id = status["task_id"]
                # Enqueue a watchdog alert on the SAME Codex session alongside the completion notice.
                store.enqueue_notification(
                    dedupe_key=f"watchdog:alert-{task_id}",
                    task_id=task_id, kind="watchdog", target_session=tid, body="watchdog alert",
                )
                sent_bodies = []

                def fake_sender(codex_home, target, body, *, queue_bin=None):
                    sent_bodies.append((target, body))
                    return True, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", ""

                stats = observer.dispatch_pending(store, sender=fake_sender)
                self.assertEqual(stats["sent"], 2)
                self.assertEqual(len(sent_bodies), 2)
                self.assertEqual(store.pending_notifications(), [])

                # Muted repo suppresses future notifications cleanly.
                config.save_settings({"notifications_enabled": True, "muted_repos": [str(repo)]})
                store.enqueue_notification(
                    dedupe_key="watchdog:muted-1", task_id=task_id, kind="watchdog", target_session=tid, body="muted",
                )
                stats2 = observer.dispatch_pending(store, sender=fake_sender)
                self.assertEqual(stats2["muted"], 1)

    def test_sampler_collects_real_task_and_caches_settled_terminal_tasks(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato"), "CODEX_HOME": str(base / "codex")}):
                repo = init_repo(base / "repo")
                sp = make_spec(repo, base / "spec.json")
                store = Store()
                res = executor.submit(
                    store, repo, sp, foreground=True,
                    worker_runner=lambda req: WorkerResult(status="completed", summary="done"),
                )
                tid = res["task_id"]
                sampler = watchdog.Sampler(store)
                try:
                    # Start sampling child
                    self.assertIsNone(sampler.poll())
                    self.assertIsNotNone(sampler.job)
                    deadline = time.monotonic() + 10.0
                    sampled = None
                    while sampled is None and time.monotonic() < deadline:
                        sampled = sampler.poll()
                        time.sleep(0.05)
                    self.assertEqual(sampled, tid)
                    self.assertIsNone(sampler.last_error)
                    pub = watchdog.public_status(store, tid)
                    self.assertEqual(pub["state"], "observing")
                    # Terminal settled task is now cached and skipped on next due cycle (P1-3).
                    self.assertIn(tid, sampler.terminal_scope)
                    sampler.due[tid] = 0.0
                    self.assertIsNone(sampler.poll())
                    self.assertIsNone(sampler.job)
                finally:
                    sampler.close()

    def test_observer_daemon_start_status_and_stop(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato"), "CODEX_HOME": str(base / "codex")}):
                store = Store()
                started = observer.start_daemon(store.db_file)
                try:
                    self.assertEqual(started["state"], "watching")
                    self.assertIsNotNone(started["pid"])
                finally:
                    stopped = observer.stop_daemon()
                self.assertEqual(stopped["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
