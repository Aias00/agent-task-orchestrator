"""Offline regression coverage for independent repair budgets and progress tracking."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from ato import executor, reviewer, watchdog
from ato.adapters.base import ReviewResult, WorkerResult
from ato.spec import TaskError, normalize_spec
from ato.store import Store
from test_core_and_store import init_repo, make_spec
from test_execution_and_cli import run_cli
from test_reviewer_and_inbox import _verdict


def blocker(title="Fix bug", **changes):
    return {"id": "F1", "severity": "blocker", "file": "app.py", "lines": "L1",
            "category": "correctness", "title": title, "evidence": "bad", "required_fix": "fix", **changes}


def sample(stage, *, status="running", reason=None):
    return {"status": status, "reason": reason, "stage": stage, "run_history": [],
            "execution": {"state": "active" if status == "running" else "idle"},
            "violations": [], "evidence_complete": True}


class RetryTracksTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {"ATO_HOME": str(self.base / "ato"), "CODEX_HOME": str(self.base / "codex"),
                                     "ATO_SUBMISSION_KIND": "agy"})
        env.start()
        self.addCleanup(env.stop)
        self.repo = init_repo(self.base / "repo")
        self.sp = make_spec(self.repo, self.base / "spec.json")
        self.store = Store()
        self.stages = []
        self.prompts = []
        self.dirs = []

    def configure(self, *, contract=None, **changes):
        raw = json.loads(self.sp.read_text())
        raw["worker_contract"].update(contract or {})
        raw.update(changes)
        self.sp.write_text(json.dumps(raw))

    def run_task(self, worker=None, rev=None):
        update_task = self.store.update_task

        def capture(task_id, **kwargs):
            if "stage" in kwargs:
                self.stages.append(deepcopy(kwargs["stage"]))
            return update_task(task_id, **kwargs)

        def work(req):
            self.prompts.append(req.prompt)
            self.dirs.append(req.runtime_dir.name)
            return worker(req) if worker else WorkerResult(status="completed", summary="done")

        with patch.object(self.store, "update_task", side_effect=capture):
            status = executor.submit(self.store, self.repo, self.sp, foreground=True,
                                     worker_runner=work, reviewer_runner=rev)
        self.assertTrue(self.stages)
        required = {"stage_id", "category", "turn", "attempts", "max_attempts", "execution_failures",
                    "max_execution_failures", "verification_failures", "review_rework_rounds", "max_review_rounds",
                    "no_progress_streak", "max_no_progress", "progress_made", "repeated_kind", "repeated_items",
                    "elapsed_seconds", "remaining_stage_ids", "steps"}
        for st in self.stages:
            self.assertTrue(required <= st.keys())
        self.assertEqual(self.dirs, [f"attempt-{n}" for n in range(1, len(self.dirs) + 1)])
        return status

    def test_five_progressing_reworks_complete_beyond_worker_attempt_cap(self):
        self.configure(reviewer={"agent": {"kind": "claude"}})
        rounds = []

        def work(req):
            (req.worktree / "app.py").write_text(f"# progress {len(self.prompts)}\n")
            return WorkerResult(status="completed", summary="done")

        def review(req):
            rounds.append(req)
            fs = [blocker(f"Issue {len(rounds)}")] if len(rounds) <= 5 else []
            return ReviewResult(ok=True, code="OK", verdict=_verdict("request_changes" if fs else "approve", fs))

        with patch("ato.executor.reviewer.run_review_turn", wraps=reviewer.run_review_turn) as review_turn:
            result = self.run_task(work, review)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rounds), 6)
        self.assertEqual(self.stages[-1]["review_rework_rounds"], 5)
        self.assertEqual(self.stages[-1]["max_review_rounds"], 10)
        self.assertTrue(all(s["execution_failures"] == 0 for s in self.stages))
        for index, call in enumerate(review_turn.call_args_list):
            self.assertEqual(call.kwargs["previous_findings"], [blocker(f"Issue {index}")] if index else [])
        rework = [s for s in self.stages if s["stage_id"] == "review_rework"]
        self.assertTrue(rework)
        self.assertEqual(max(s["turn"] for s in rework), 6)
        for st in rework:
            self.assertTrue(st["progress_made"])
            self.assertEqual(watchdog.findings(sample(st)), {})

    def test_execution_failures_use_independent_cap(self):
        def worker(req):
            return WorkerResult(status="failed", summary=f"different failure {len(self.prompts)}")

        result = self.run_task(worker)
        self.assertEqual(result["reason"], "WORKER_FAILED")
        self.assertEqual(result["status"], "blocked")
        st = self.stages[-1]
        self.assertEqual(st["execution_failures"], 3)
        self.assertEqual(st["review_rework_rounds"], 0)
        self.assertEqual(st["category"], "execution_failure")
        self.assertTrue(any("EXECUTION_FAILURE_RETRY" in watchdog.findings(sample(s)) for s in self.stages))
        self.assertEqual(watchdog.findings(sample(st, status="blocked", reason=result["reason"]))
                         ["UNHANDLED_FAILURE"]["category"], "execution_failure")

    def test_review_and_execution_budgets_are_independent_when_interleaved(self):
        self.configure(reviewer={"agent": {"kind": "claude"}})
        rounds = []

        def worker(req):
            n = len(self.prompts)
            (req.worktree / "app.py").write_text(f"# turn {n}\n")
            return WorkerResult(status="failed" if n in (2, 4, 6) else "completed", summary=f"turn {n}")

        def review(req):
            rounds.append(req)
            return ReviewResult(ok=True, code="OK", verdict=_verdict("request_changes", [blocker(str(len(rounds)))]))

        result = self.run_task(worker, review)
        self.assertEqual(result["reason"], "WORKER_FAILED")
        self.assertEqual(len(self.prompts), 6)
        self.assertEqual(self.stages[-1]["execution_failures"], 3)
        self.assertEqual(self.stages[-1]["review_rework_rounds"], 3)

    def test_unresolved_review_blockers_warn_then_pause_despite_tree_changes(self):
        self.configure(reviewer={"agent": {"kind": "claude"}})
        rounds = []

        def worker(req):
            (req.worktree / "app.py").write_text(f"# unrelated change {len(self.prompts)}\n")
            return WorkerResult(status="completed", summary="done")

        def review(req):
            rounds.append(req)
            fs = [blocker("  FIX   Bug " if len(rounds) == 2 else "Fix bug", id=f"F{len(rounds)}")]
            if len(rounds) >= 2:
                fs.append(blocker("Another bug", id="extra"))
            return ReviewResult(ok=True, code="OK", verdict=_verdict("request_changes", fs))

        result = self.run_task(worker, review)
        self.assertEqual(result["reason"], "REPEATED_NO_PROGRESS")
        self.assertEqual(len(rounds), 3)
        self.assertIn("WARNING: Repeated issue", self.prompts[2])
        warned = next(s for s in self.stages if s["no_progress_streak"] == 1)
        alert = watchdog.findings(sample(warned))["REPEATED_NO_PROGRESS"]
        self.assertEqual(alert["kind"], "review_blocker")
        self.assertEqual(alert["items"], [{"file": "app.py", "title": "fix bug"}])
        self.assertEqual(self.stages[-1]["no_progress_streak"], 2)
        self.assertEqual(self.stages[-1]["category"], "repeated_no_progress")
        self.assertIsNotNone(result["product"])
        run = self.store.get_run(self.store.get_task(result["task_id"])["active_run_id"])
        self.assertEqual((run["phase"], run["outcome"], run["error_code"]),
                         ("settled", "blocked", "REPEATED_NO_PROGRESS"))
        self.assertIn("Resolve these issues", self.store.get_task(result["task_id"])["next_step"])
        events = [e for e in self.store.list_events(result["task_id"]) if e["kind"] == "repeated_no_progress"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["kind"], "review_blocker")
        self.assertEqual(events[0]["detail"]["streak"], 2)
        self.assertTrue(any(n["kind"] == "failure" for n in self.store.list_notifications(result["task_id"])))

    def test_identical_execution_failures_pause(self):
        result = self.run_task(lambda req: WorkerResult(status="failed", summary="same error", blocker="CALL_FAILED"))
        self.assertEqual(result["reason"], "REPEATED_NO_PROGRESS")
        self.assertEqual(len(self.prompts), 3)
        self.assertEqual(self.stages[-1]["repeated_kind"], "execution_failure")
        self.assertEqual(self.stages[-1]["execution_failures"], 3)
        self.assertIn("WARNING: Repeated issue", self.prompts[-1])

    def test_identical_checks_pause_with_product_before_repair_cap(self):
        self.configure(contract={"max_attempts": 4, "acceptance": [
            {"id": "bad", "kind": "command", "description": "fails", "checks": [["false"]]}]})
        result = self.run_task()
        self.assertEqual(result["reason"], "REPEATED_NO_PROGRESS")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.stages[-1]["repeated_kind"], "checks")
        self.assertEqual(self.stages[-1]["verification_failures"], 3)
        self.assertEqual(self.stages[-1]["execution_failures"], 0)
        self.assertIsNotNone(result["product"])

    def test_identical_guardrails_pause(self):
        self.configure(guardrails={"protected_paths": ["README.md"]})

        def worker(req):
            (req.worktree / "README.md").write_text("bad change\n")
            return WorkerResult(status="completed", summary="done")

        result = self.run_task(worker)
        self.assertEqual(result["reason"], "REPEATED_NO_PROGRESS")
        self.assertEqual(self.stages[-1]["repeated_kind"], "guardrail")
        self.assertEqual(self.stages[-1]["execution_failures"], 0)

    def test_verification_budget_resets_after_passing_before_each_review(self):
        self.configure(contract={"max_attempts": 2, "acceptance": [
            {"id": "ready", "kind": "command", "description": "ready file", "checks": [["test", "-f", "ready"]]}]},
            reviewer={"agent": {"kind": "claude"}})
        rounds = []

        def worker(req):
            if len(self.prompts) % 2 == 0:
                (req.worktree / "ready").touch()
            else:
                (req.worktree / "ready").unlink(missing_ok=True)
            (req.worktree / "app.py").write_text(f"# progress {len(self.prompts)}\n")
            return WorkerResult(status="completed", summary="done")

        def review(req):
            rounds.append(req)
            return ReviewResult(ok=True, code="OK", verdict=_verdict(
                "approve" if len(rounds) == 3 else "request_changes",
                [] if len(rounds) == 3 else [blocker(f"issue {len(rounds)}")]))

        result = self.run_task(worker, review)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.prompts), 6)
        self.assertTrue(all(s["verification_failures"] == 0 for s in self.stages if s["stage_id"] == "review"))

    def test_effective_reviewer_limit_and_kind_change_reset_session(self):
        self.configure(reviewer={"agent": {"kind": "claude"}, "max_review_rounds": 1,
                                 "session_mode": "resume_if_supported"})
        seen = []

        def worker(req):
            (req.worktree / "app.py").write_text(f"# progress {len(self.prompts)}\n")
            effective = {"agent": {"kind": "claude" if len(self.prompts) == 1 else "codex"},
                         "max_review_rounds": 3, "session_mode": "resume_if_supported"}
            self.store.update_task(req.task_id, reviewer=effective)
            return WorkerResult(status="completed", summary="done")

        def review(req):
            seen.append((req.kind, req.resume_session_id))
            return ReviewResult(ok=True, code="OK", session_id=f"session-{len(seen)}",
                                verdict=_verdict("request_changes", [blocker(f"issue {len(seen)}")]))

        result = self.run_task(worker, review)
        self.assertEqual(result["reason"], "REVIEW_CHANGES_REQUESTED")
        self.assertEqual(seen, [("claude", None), ("codex", None), ("codex", "session-2")])
        self.assertEqual(self.stages[-1]["max_review_rounds"], 3)
        self.assertEqual(self.stages[-1]["execution_failures"], 0)
        self.assertTrue(any(n["kind"] == "failure" for n in self.store.list_notifications(result["task_id"])))

    def test_resolving_one_blocker_resets_review_streak(self):
        self.configure(reviewer={"agent": {"kind": "claude"}})
        rounds = []

        def worker(req):
            (req.worktree / "app.py").write_text(f"# progress {len(self.prompts)}\n")
            return WorkerResult(status="completed", summary="done")

        def review(req):
            rounds.append(req)
            n = len(rounds)
            fs = ([blocker("A"), blocker("B", id="F2")] if n <= 2
                  else [blocker("B", id="F2")] if n <= 4 else [])
            return ReviewResult(ok=True, code="OK", verdict=_verdict("request_changes" if fs else "approve", fs))

        result = self.run_task(worker, review)
        self.assertEqual(result["status"], "completed")
        starts = [s for s in self.stages if s["stage_id"] == "review"]
        self.assertEqual([s["no_progress_streak"] for s in starts], [0, 0, 1, 0, 1])
        # Completion is persisted atomically by settle_owned_run, not update_task.
        # Read the durable stage rather than the last intermediate method call.
        final_stage = self.store.get_task(result["task_id"])["stage"]
        self.assertEqual(result["stage"], final_stage)
        self.assertEqual(final_stage["steps"][0]["state"], "completed")
        self.assertEqual(final_stage["no_progress_streak"], 0)
        self.assertEqual(final_stage["repeated_items"], [])
        self.assertIsNone(final_stage["repeated_kind"])
        self.assertTrue(final_stage["progress_made"])

    def test_changed_failure_tree_resets_consecutive_streak(self):
        self.configure(contract={"max_attempts": 6})

        def worker(req):
            if len(self.prompts) == 3:
                (req.worktree / "app.py").write_text("# changed\n")
            return WorkerResult(status="failed", summary="same failure")

        result = self.run_task(worker)
        self.assertEqual(result["reason"], "REPEATED_NO_PROGRESS")
        self.assertEqual(len(self.prompts), 5)
        self.assertEqual(self.stages[-1]["execution_failures"], 5)
        reset = [s for s in self.stages if s["turn"] == 3 and s["execution_failures"] == 3]
        self.assertTrue(reset)
        self.assertTrue(all(s["no_progress_streak"] == 0 for s in reset))

    def test_configured_no_progress_limit_one_pauses_on_first_repeat(self):
        self.configure(contract={"max_no_progress_rounds": 1})
        result = self.run_task(lambda req: WorkerResult(status="failed", summary="same failure"))
        self.assertEqual(result["reason"], "REPEATED_NO_PROGRESS")
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(self.stages[-1]["no_progress_streak"], 1)
        self.assertEqual(self.stages[-1]["max_no_progress"], 1)

    def test_cli_reviewer_default(self):
        result = self.run_task()
        code, receipt = run_cli(["agent", "reviewer", result["task_id"], "--kind", "codex"], store=self.store)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["data"]["reviewer"]["max_review_rounds"], 10)

    def test_spec_limits_and_optional_hash_compatibility(self):
        original = normalize_spec(self.repo, self.sp)
        self.assertNotIn("max_no_progress_rounds", original.document["worker_contract"])
        for field, limit in (("max_review_rounds", 50), ("max_no_progress_rounds", 10)):
            for value in (1, limit, 0, limit + 1, True, 1.5, "2", None):
                with self.subTest(field=field, value=value):
                    make_spec(self.repo, self.sp)
                    if field == "max_review_rounds":
                        self.configure(reviewer={"agent": {"kind": "claude"}, field: value})
                    else:
                        self.configure(contract={field: value})
                    if type(value) is int and 1 <= value <= limit:
                        norm = normalize_spec(self.repo, self.sp)
                        self.assertEqual(norm.document["reviewer" if field == "max_review_rounds" else "worker_contract"][field], value)
                    else:
                        with self.assertRaises(TaskError) as error:
                            normalize_spec(self.repo, self.sp)
                        self.assertEqual(error.exception.code, "INVALID_SPEC")
        make_spec(self.repo, self.sp)
        self.assertEqual(normalize_spec(self.repo, self.sp).spec_sha256, original.spec_sha256)
        self.configure(contract={"max_no_progress_rounds": 2})
        self.assertNotEqual(normalize_spec(self.repo, self.sp).spec_sha256, original.spec_sha256)


class WatchdogTrackTests(unittest.TestCase):
    def test_classification_and_legacy_fallback(self):
        healthy = {"category": "review_rework", "execution_failures": 2, "no_progress_streak": 0,
                   "steps": [{"stage_id": "review_rework", "step_id": "worker", "attempts": 8, "state": "running"}]}
        self.assertEqual(watchdog.findings(sample(healthy)), {})
        legacy = {"steps": [{"stage_id": "implement", "step_id": "worker", "attempts": 3, "state": "running"}]}
        self.assertIn("EXECUTION_FAILURE_RETRY", watchdog.findings(sample(legacy)))
        legacy["steps"][0]["stage_id"] = "review_rework"
        self.assertEqual(watchdog.findings(sample(legacy)), {})
        self.assertEqual(watchdog.findings(sample({"category": "verification_repair", "execution_failures": 0,
                                                 "steps": [{"stage_id": "verify", "step_id": "check", "attempts": 5, "state": "running"}]})), {})
        for reason, category in (("WORKER_FAILED", "execution_failure"), ("CHECKS_FAILED", "verification_repair"),
                                 ("REVIEW_CHANGES_REQUESTED", "review_rework"), ("WORKER_BLOCKED", "blocked"),
                                 ("REPEATED_NO_PROGRESS", "repeated_no_progress")):
            alerts = watchdog.findings(sample({}, status="blocked", reason=reason))
            self.assertEqual(alerts["UNHANDLED_FAILURE"]["category"], category)
        for status in watchdog.TERMINAL:
            self.assertEqual(watchdog.findings(sample({"execution_failures": 4, "no_progress_streak": 1}, status=status)), {})

    def test_settled_terminal_public_status_ignores_age_but_requires_matching_clean_sample(self):
        store = Mock()
        task = {"revision": 2, "active_run_id": "run", "status": "completed"}
        rec = {"revision": 2, "run_id": "run", "status": "completed", "sampled_at": 1000, "sample_error": None}
        store.get_task.return_value = task
        store.get_watchdog.return_value = (rec, [])
        for status in watchdog.TERMINAL:
            task["status"] = rec["status"] = status
            for now in (1000, 4600):
                self.assertEqual(watchdog.public_status(store, "task", now=now)["state"], "settled")
        rec["sample_error"] = "SAMPLE_UNAVAILABLE"
        self.assertEqual(watchdog.public_status(store, "task", now=4600)["state"], "stale")
        rec["sample_error"] = None
        rec["revision"] = 1
        self.assertEqual(watchdog.public_status(store, "task", now=4600)["state"], "awaiting_sample")
        rec["revision"] = 2
        rec["status"] = "running"
        self.assertEqual(watchdog.public_status(store, "task", now=4600)["state"], "stale")
        rec["status"] = "completed"
        alert = {"id": "alert", "code": "x", "state": "resolved", "run_id": "run", "first_seen": 1,
                 "last_seen": 2, "resolved_at": 2, "evidence": {}, "notification_state": "sent", "notification_error": None}
        store.get_watchdog.return_value = (rec, [alert])
        self.assertEqual(watchdog.public_status(store, "task", now=4600)["state"], "settled")
        for state, notification in (("active", "sent"), ("resolved", "pending")):
            alert.update(state=state, notification_state=notification)
            self.assertEqual(watchdog.public_status(store, "task", now=4600)["state"], "stale")


if __name__ == "__main__":
    unittest.main()
