"""Offline settlement, check receipt migration, and worker timeout history."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from ato import config, executor, observer, watchdog
from ato.adapters.base import ReviewResult, WorkerResult
from ato.spec import TaskError
from ato.store import Store
from test_core_and_store import init_repo, make_spec
from test_execution_and_cli import run_cli
from test_reviewer_and_inbox import _verdict
from test_retry_tracks import blocker


def complete(req):
    return WorkerResult(status="completed", summary="Synthetic completion")


def timeout(req):
    return WorkerResult(status="blocked", blocker="TIMEOUT", summary="Synthetic worker timeout")


def approve(req):
    return ReviewResult(ok=True, code="OK", verdict=_verdict("approve", []))


class VerificationSettlementTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {
            "ATO_HOME": str(self.base / "ato"), "CODEX_HOME": str(self.base / "codex"),
            "ATO_SUBMISSION_KIND": "agy", "ATO_TEST_SECRET": "synthetic-secret-not-public",
        })
        env.start()
        self.addCleanup(env.stop)
        self.repo = init_repo(self.base / "repo")
        self.spec = make_spec(self.repo, self.base / "spec.json")
        self.store = Store()
        self.gate = self.base / "verification-ready"

    def configure(self, *, criteria=None, attempts=1, review=False):
        raw = json.loads(self.spec.read_text())
        if criteria is not None:
            raw["worker_contract"]["acceptance"] = [
                {"id": cid, "kind": "command", "description": cid, "checks": checks}
                for cid, checks in criteria
            ]
        raw["worker_contract"]["max_attempts"] = attempts
        if review:
            raw["reviewer"] = {"agent": {"kind": "codex"}, "max_review_rounds": 1}
        self.spec.write_text(json.dumps(raw))

    def gate_command(self):
        return [sys.executable, "-c",
                "import os, sys; from pathlib import Path; "
                "print(os.environ['ATO_TEST_SECRET']); sys.exit(0 if Path(sys.argv[1]).exists() else 7)",
                str(self.gate)]

    def cli(self, *args, worker=None, review=None, expected=0):
        code, result = run_cli(list(args), store=self.store, worker_runner=worker, reviewer_runner=review)
        self.assertEqual(code, expected, result)
        return result["data"] if expected == 0 else result["error"]

    def submit(self, worker=complete, review=None):
        return self.cli("run", "--repo", str(self.repo), "--spec", str(self.spec), "--foreground",
                        worker=worker, review=review)

    def test_frozen_check_rounds_failed_recheck_and_recovery(self):
        self.configure(attempts=2, criteria=[
            ("artifact", [["true"], ["test", "-f", "artifact"]]),
            ("gate", [self.gate_command()]),
        ])
        config.save_settings({"advisory_checks": True})
        self.assertTrue(self.cli("system", "status")["advisory_checks"])
        frozen_bytes = self.spec.read_bytes()
        turns = []

        def worker(req):
            path = req.worktree / "artifact"
            turns.append(req)
            if len(turns) == 1:
                path.touch()
            else:
                path.unlink()
                (req.worktree / "product.txt").write_text("worker product\n")
            return complete(req)

        initial = self.submit(worker)
        tid = initial["task_id"]
        initial_run = initial["execution"]["run_id"]
        self.assertEqual(len(turns), 2)
        self.assertEqual((initial["status"], initial["reason"]), ("blocked", "CHECKS_FAILED"))
        self.assertEqual(initial["run_history"][-1]["outcome"], "blocked")
        self.assertEqual([c["state"] for c in initial["checks"]], ["failed", "failed"])
        self.assertIn("ato project recheck", initial["next_step"])
        self.assertEqual(initial["product"]["diff_summary"]["files_added"], ["product.txt"])
        self.assertEqual(initial["delivery"][0]["kind"], "failure")
        self.assertEqual(initial["delivery"][0]["state"], "unsupported")
        self.assertEqual(initial["completion_notification"]["state"], "unsupported")

        failed = self.cli("project", "recheck", tid)
        self.assertEqual((failed["status"], failed["reason"]), ("blocked", "CHECKS_FAILED"))
        self.assertEqual(failed["product"], initial["product"])
        self.assertNotEqual(failed["execution"]["run_id"], initial_run)
        self.assertEqual(failed["run_history"][-1]["error_code"], "CHECKS_FAILED")
        self.assertEqual([n["kind"] for n in failed["delivery"]], ["failure", "failure"])
        self.assertEqual(self.cli("accept", tid, expected=4)["code"], "CHECKS_REQUIRED")

        self.gate.touch()
        (Path(failed["product"]["worktree"]) / "artifact").touch()
        passed = self.cli("project", "recheck", tid, "--checks-only")
        self.assertEqual((passed["status"], passed["reason"], passed["next_step"]), ("completed", None, None))
        self.assertEqual(passed["spec_sha256"], initial["spec_sha256"])
        self.assertEqual(passed["product"]["branch"], initial["product"]["branch"])
        self.assertEqual(self.spec.read_bytes(), frozen_bytes)

        history = self.cli("status", tid, "--check-history")
        rounds = history["check_history"]
        self.assertEqual([(r["run_id"], r["attempt"]) for r in rounds], [
            (initial_run, 1), (initial_run, 2), (failed["execution"]["run_id"], 1),
            (passed["execution"]["run_id"], 1),
        ])
        self.assertEqual([[c["state"] for c in r["checks"]] for r in rounds],
                         [["passed", "failed"], ["failed", "failed"], ["failed", "failed"], ["passed", "passed"]])
        self.assertEqual(rounds[1]["checks"][0]["diagnostic"],
                         {"command_index": 2, "code": "PROCESS_ERROR", "returncode": 1})
        self.assertEqual(rounds[1]["checks"][1]["diagnostic"]["returncode"], 7)
        self.assertEqual(history["checks"], passed["checks"])
        self.assertNotEqual(rounds[0]["checks"][0]["receipt_id"], rounds[1]["checks"][0]["receipt_id"])
        trace = self.cli("project", "trace", tid)
        self.assertEqual([e["detail"] for e in trace["events"] if e["kind"] == "checks_finished"], [
            {"attempt": 1, "passed": False, "failed_criteria": ["gate"]},
            {"attempt": 2, "passed": False, "failed_criteria": ["artifact", "gate"]},
            {"attempt": 1, "passed": False, "failed_criteria": ["artifact", "gate"]},
            {"attempt": 1, "passed": True, "failed_criteria": []},
        ])
        self.assertNotIn("synthetic-secret-not-public", json.dumps([history, trace]))
        self.assertNotIn(str(self.gate), json.dumps([history, trace]))

        self.store.save_checks(tid, initial_run, rounds[0]["checks"], attempt=1, passed=False)
        with self.assertRaises(TaskError) as conflicting:
            self.store.save_checks(tid, initial_run, rounds[3]["checks"], attempt=1, passed=True)
        self.assertEqual(conflicting.exception.code, "STATE_CORRUPT")
        self.store = Store(self.store.db_file)
        self.assertEqual(self.cli("status", tid, "--check-history")["check_history"], rounds)
        self.assertEqual(self.cli("project", "trace", tid), trace)

    def test_legacy_rows_remain_readable_and_recoverable(self):
        self.gate.touch()
        self.configure(criteria=[("gate", [self.gate_command()])], review=True)
        initial = self.submit(review=approve)
        resumable = self.submit(review=approve)
        tid, rid = initial["task_id"], initial["execution"]["run_id"]
        acceptance = self.cli("accept", tid)["acceptance"]
        for view in (initial, resumable):
            self.store.update_task(view["task_id"], reason="CHECKS_FAILED")
        notices, runs = self.store.list_notifications(tid), self.store.list_runs(tid)
        with sqlite3.connect(self.store.db_file) as con:
            con.execute("DROP INDEX idx_checks_attempt")
            for table, columns in {
                "checks": ("attempt", "diagnostic_json"),
                "reviews": ("product_id", "reviewer_sha256"),
                "runs": ("worker_timed_out",),
            }.items():
                for column in columns:
                    con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            con.execute(
                "UPDATE checks SET state='failed', receipt_id='old-receipt', output_excerpt='old-private-output'",
            )
            con.execute("""UPDATE events SET detail_json = '{"attempt":1,"passed":false}'
                           WHERE kind = 'checks_finished'""")

        for _ in range(2):
            self.store = Store(self.store.db_file)
            view = self.cli("status", tid, "--check-history")
            self.assertEqual((view["status"], view["reason"]), ("blocked", "CHECKS_FAILED"))
            self.assertTrue(view["legacy_advisory_completion"])
            self.assertEqual(view["legacy_acceptance"], acceptance)
            self.assertEqual(view["acceptance"]["disposition"], "not_recorded")
            self.assertEqual(view["product"], initial["product"])
            self.assertEqual(view["checks"][0]["receipt_id"], "old-receipt")
            self.assertEqual(view["check_history"], [{"run_id": rid, "attempt": None, "checks": [
                {"criterion_id": "gate", "state": "failed", "receipt_id": "old-receipt", "diagnostic": None}]}])
            self.assertNotIn("old-private-output", json.dumps(view))
            self.assertEqual(self.store.get_task(tid)["status"], "completed")
            self.assertIsNone(self.store.latest_review(tid)["product_id"])
            self.assertEqual(self.store.list_runs(tid), runs)
            self.assertEqual(self.store.list_notifications(tid), notices)
            with sqlite3.connect(self.store.db_file) as con:
                self.assertEqual(con.execute("PRAGMA user_version").fetchone(), (2,))
                self.assertEqual(con.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(con.execute("SELECT output_excerpt FROM checks WHERE run_id=?", (rid,)).fetchone(),
                                 ("old-private-output",))

        self.assertTrue(all(t["status"] == "blocked" for t in self.cli("list")["tasks"]))
        self.assertNotIn("UNHANDLED_FAILURE", watchdog.findings(watchdog.collect(self.store, tid)))
        self.assertEqual(self.cli("accept", tid, expected=4)["code"], "CHECKS_REQUIRED")
        self.assertEqual(self.cli("project", "cleanup", tid, "--confirm", expected=4)["code"], "CLEANUP_BLOCKED")
        self.assertEqual(self.cli("project", "recheck", tid, "--checks-only")["reason"], "REVIEW_REQUIRED")
        self.assertEqual(self.cli("project", "recheck", tid, review=approve)["status"], "completed")
        self.assertEqual(self.store.list_runs(tid)[:-2], runs)
        self.assertEqual(self.store.list_notifications(tid), notices)
        self.assertEqual(self.cli("status", tid, "--check-history")["check_history"][0]["attempt"], None)
        self.assertEqual(self.cli("resume", resumable["task_id"], "--foreground", worker=complete,
                                  review=approve)["status"], "completed")

    def test_checks_only_requires_review_for_the_exact_product_and_config(self):
        self.configure(criteria=[("gate", [self.gate_command()])], review=True)
        reviewer = Mock(side_effect=approve)
        initial = self.submit(review=reviewer)
        tid = initial["task_id"]
        self.gate.touch()
        checking = self.cli("project", "recheck", tid, "--checks-only", review=reviewer)
        self.assertEqual((checking["status"], checking["reason"]), ("blocked", "REVIEW_REQUIRED"))
        reviewer.assert_not_called()

        approved = self.cli("project", "recheck", tid, review=reviewer)
        self.assertEqual(approved["status"], "completed")
        reviewer.assert_called_once()
        self.gate.unlink()
        self.assertEqual(self.cli("project", "recheck", tid, "--checks-only")["reason"], "CHECKS_FAILED")
        self.gate.touch()
        self.assertEqual(self.cli("project", "recheck", tid, "--checks-only")["status"], "completed")

        (Path(approved["product"]["worktree"]) / "changed.txt").touch()
        self.assertEqual(self.cli("project", "recheck", tid, "--checks-only")["reason"], "REVIEW_REQUIRED")
        (Path(approved["product"]["worktree"]) / "changed.txt").unlink()
        self.cli("agent", "reviewer", tid, "--kind", "codex", "--model", "new-model")
        self.assertEqual(self.cli("project", "recheck", tid, "--checks-only")["reason"], "REVIEW_REQUIRED")

    def test_recheck_keeps_reviewer_rejection_and_mutation_distinct(self):
        self.configure(review=True)

        def reject(req):
            return ReviewResult(ok=True, code="OK", verdict=_verdict("request_changes", [blocker()]))

        initial = self.submit(review=reject)
        tid = initial["task_id"]
        self.assertEqual(initial["reason"], "REVIEW_CHANGES_REQUESTED")
        rechecked = self.cli("project", "recheck", tid, "--checks-only")
        self.assertEqual((rechecked["status"], rechecked["reason"]), ("blocked", "REVIEW_CHANGES_REQUESTED"))

        def mutate(req):
            (req.worktree / "new-file").touch()
            return approve(req)

        rechecked = self.cli("project", "recheck", tid, review=mutate)
        self.assertEqual((rechecked["status"], rechecked["reason"]), ("blocked", "MUTATION_VIOLATION"))
        self.assertEqual(self.cli("project", "recheck", tid, "--checks-only")["reason"], "REVIEW_REQUIRED")

    def test_stop_wins_before_failed_recheck_settlement(self):
        self.configure(criteria=[("gate", [self.gate_command()])])
        initial = self.submit()
        tid = initial["task_id"]
        settle = self.store.settle_owned_run

        def stop_before_settlement(*args, **kwargs):
            executor.request_stop(self.store, tid)
            return settle(*args, **kwargs)

        with patch.object(self.store, "settle_owned_run", side_effect=stop_before_settlement):
            rechecked = self.cli("project", "recheck", tid)
        self.assertEqual((rechecked["status"], rechecked["reason"]), ("stopped", "STOPPED"))
        self.assertEqual(rechecked["run_history"][-1]["outcome"], "stopped")
        self.assertEqual(rechecked["delivery"], initial["delivery"])

    def test_worker_timeout_streak_is_derived_from_settled_worker_runs(self):
        self.configure(review=True)
        initial = self.submit(worker=timeout)
        tid = initial["task_id"]

        def resume(worker=timeout, *opts, review=None):
            return self.cli("resume", tid, "--foreground", *opts, worker=worker, review=review)

        with patch("ato.product.run_bounded_subprocess", return_value=(-1, b"", b"", "TIMEOUT")):
            rechecked = self.cli("project", "recheck", tid)
        self.assertEqual(rechecked["reason"], "CHECKS_FAILED")
        self.assertIsNone(rechecked["repeated_worker_timeout"])
        self.assertFalse(rechecked["run_history"][-1]["worker_timed_out"])
        detail = self.cli("status", tid, "--check-history")["check_history"][0]["checks"][0]["diagnostic"]
        self.assertEqual(detail["code"], "TIMEOUT")

        repeated = resume(timeout, "--timeout-seconds", "600")
        streak = repeated["repeated_worker_timeout"]
        self.assertEqual((streak["count"], streak["timeout_seconds"], streak["timeout_source"]),
                         (2, 600, "resume_override"))
        self.assertEqual(streak["first_run_id"], initial["execution"]["run_id"])
        self.assertEqual(streak["last_run_id"], repeated["execution"]["run_id"])
        self.assertEqual(streak["timeout_sources"], ["default", "resume_override"])
        self.assertIn(f"ato resume {tid} --timeout-seconds 1200", streak["next_step"])
        self.assertEqual(resume()["repeated_worker_timeout"]["count"], 3)
        self.store = Store(self.store.db_file)
        self.assertEqual(self.cli("status", tid)["repeated_worker_timeout"]["count"], 3)

        self.assertIsNone(resume(timeout, "--timeout-seconds", "1200")["repeated_worker_timeout"])
        self.assertIsNone(resume()["repeated_worker_timeout"])
        rejected = resume(complete, review=lambda req: ReviewResult(ok=False, code="TIMEOUT"))
        self.assertEqual(rejected["reason"], "TIMEOUT")
        self.assertFalse(rejected["run_history"][-1]["worker_timed_out"])
        self.assertIsNone(resume()["repeated_worker_timeout"])
        old_run = self.store.get_task(tid)["active_run_id"]
        with self.store.tx() as con:
            con.execute("UPDATE runs SET worker_timed_out=NULL WHERE id=?", (old_run,))
        self.assertIsNone(resume()["repeated_worker_timeout"])
        self.assertEqual(resume()["repeated_worker_timeout"]["count"], 2)
        self.assertEqual(self.cli("status", tid)["spec_sha256"], initial["spec_sha256"])

    def test_codex_failure_notices_use_existing_policy_without_duplicate_watchdog_notices(self):
        session = "11111111-2222-3333-4444-555555555555"
        with patch.dict(os.environ, {"ATO_SUBMISSION_KIND": "codex", "CODEX_THREAD_ID": session}), \
                patch("ato.codex_notify.session_path", return_value=str(self.base / "synthetic.jsonl")):
            self.configure(criteria=[("gate", [self.gate_command()])])
            initial = self.submit()
            tid = initial["task_id"]
            for args, worker in ((("project", "recheck", tid), None), (("resume", tid, "--foreground"), timeout)):
                result = self.cli(*args, worker=worker)
                sample = watchdog.collect(self.store, tid)
                state, alerts = watchdog.reduce_sample(None, [], sample)
                self.store.save_watchdog(tid, state, alerts)
                watchdog.sync_alert_notifications(self.store, tid)
                watchdog.sync_alert_notifications(self.store, tid)
                self.assertEqual(self.store.get_watchdog(tid)[1][0]["notification_state"], "cancelled")
                self.assertEqual(sample["run_id"], result["execution"]["run_id"])

        notices = self.store.list_notifications(tid)
        self.assertEqual([n["kind"] for n in notices], ["failure", "failure", "failure"])
        self.assertEqual(len({n["dedupe_key"] for n in notices}), 3)
        sender = Mock(return_value=(True, "synthetic-receipt", ""))
        self.assertEqual(observer.dispatch_pending(self.store, sender=sender)["sent"], 3)
        self.assertEqual(observer.dispatch_pending(self.store, sender=sender)["sent"], 0)
        self.assertEqual(sender.call_count, 3)
        self.assertTrue(all(call.args[1] == session for call in sender.call_args_list))

        config.save_settings({"muted_repos": [str(self.repo)]})
        self.assertEqual(self.cli("project", "recheck", tid)["delivery"][-1]["state"], "muted")
        self.assertEqual(self.store.pending_notifications(), [])
