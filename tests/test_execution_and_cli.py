"""End-to-end CLI tests covering task lifecycle, verification, stop/resume, and cleanup."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ato import cli, executor
from ato.adapters.base import WorkerResult
from ato.spec import TaskError
from ato.store import Store
from test_core_and_store import init_repo, make_spec


def run_cli(argv: list[str], *, store: Store, worker_runner=None, reviewer_runner=None) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv, store=store, worker_runner=worker_runner, reviewer_runner=reviewer_runner)
    return code, json.loads(buf.getvalue())


class ExecutionAndCLITests(unittest.TestCase):
    def test_timeout_receipts_and_single_run_resume_override(self):
        for spec_timeout in (None, 600, 42):
            with self.subTest(spec_timeout=spec_timeout), TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                with patch.dict(os.environ, {"ATO_HOME": str(base / "ato"), "CODEX_HOME": str(base / "codex"),
                                             "ATO_SUBMISSION_KIND": "agy"}):
                    repo = init_repo(base / "repo")
                    sp = make_spec(repo, base / "spec.json")
                    raw = json.loads(sp.read_text())
                    if spec_timeout is not None:
                        raw["worker_contract"]["timeout_seconds"] = spec_timeout
                    raw["reviewer"] = {"agent": {"kind": "codex"}, "timeout_seconds": 45}
                    sp.write_text(json.dumps(raw))
                    seconds = spec_timeout if spec_timeout is not None else 600
                    source = "spec" if spec_timeout is not None else "default"
                    store = Store()
                    code, preview = run_cli(["project", "validate", "--repo", str(repo), "--spec", str(sp)], store=store)
                    self.assertEqual(code, 0)
                    self.assertEqual((preview["data"]["timeout_seconds"], preview["data"]["timeout_source"]), (seconds, source))
                    self.assertEqual((preview["data"]["reviewer_timeout_seconds"], preview["data"]["reviewer_timeout_source"]), (45, "spec"))
                    requests = []

                    def worker(req):
                        requests.append(req)
                        return WorkerResult(status="blocked", summary="Synthetic timeout", blocker="TIMEOUT")

                    code, initial = run_cli(["run", "--repo", str(repo), "--spec", str(sp), "--foreground"],
                                            store=store, worker_runner=worker)
                    self.assertEqual(code, 0)
                    tid = initial["data"]["task_id"]
                    frozen = store.get_task(tid)
                    self.assertEqual(frozen["spec_timeout_source"], source)
                    self.assertEqual(initial["data"]["spec_sha256"], preview["data"]["spec_sha256"])
                    # Keep a prior acceptance receipt through the resumed blocked runs too.
                    acceptance = {"disposition": "not_recorded", "criteria": [{"id": "check-1"}]}
                    store.update_task(tid, acceptance=acceptance)
                    receipts = [initial["data"]]
                    for override in (3600, None, 5, 7200):
                        args = ["resume", tid, "--foreground"]
                        if override is not None:
                            args += ["--timeout-seconds", str(override)]
                        code, resumed = run_cli(args, store=store, worker_runner=worker)
                        self.assertEqual(code, 0, resumed)
                        receipts.append(resumed["data"])
                        current = store.get_task(tid)
                        for key in ("id", "spec", "spec_sha256", "worktree_path", "base_commit", "branch", "spec_timeout_source"):
                            self.assertEqual(current[key], frozen[key], key)
                        self.assertEqual(current["acceptance"], acceptance)
                        self.assertEqual(current["spec"]["worker_contract"]["acceptance"], raw["worker_contract"]["acceptance"])
                    expected = [(seconds, source), (3600, "resume_override"), (seconds, source),
                                (5, "resume_override"), (7200, "resume_override")]
                    self.assertEqual([r.timeout_seconds for r in requests], [s for s, _ in expected])
                    self.assertEqual({str(r.worktree) for r in requests}, {frozen["worktree_path"]})
                    for receipt, pair in zip(receipts, expected):
                        self.assertEqual((receipt["execution"]["timeout_seconds"], receipt["execution"]["timeout_source"]), pair)
                    code, status = run_cli(["status", tid], store=store)
                    self.assertEqual(code, 0)
                    for rows in (status["data"]["run_history"], store.list_runs(tid)):
                        self.assertEqual([(r["timeout_seconds"], r["timeout_source"]) for r in rows], expected)
                    code, trace = run_cli(["project", "trace", tid], store=store)
                    self.assertEqual(code, 0)
                    events = [e for e in trace["data"]["events"] if e["kind"] in ("task_submitted", "task_resumed")]
                    self.assertEqual([(e["detail"]["timeout_seconds"], e["detail"]["timeout_source"]) for e in events], expected)
                    self.assertEqual([e["detail"]["previous_timeout_seconds"] for e in events[1:]],
                                     [seconds, 3600, seconds, 5])
                    self.assertEqual([e["detail"]["timeout_overridden"] for e in events[1:]], [True, False, True, True])
                    self.assertEqual([e["run_id"] for e in events], [r["id"] for r in store.list_runs(tid)])

    def test_background_entry_reads_persisted_timeout_and_legacy_fallback(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato"), "CODEX_HOME": str(base / "codex"),
                                         "ATO_SUBMISSION_KIND": "agy"}):
                repo = init_repo(base / "repo")
                sp = make_spec(repo, base / "spec.json")
                store = Store()
                with patch.object(executor, "_spawn_background_run") as spawn:
                    code, submitted = run_cli(["run", "--repo", str(repo), "--spec", str(sp)], store=store)
                    self.assertEqual(code, 0)
                    tid = submitted["data"]["task_id"]
                    rid = submitted["data"]["execution"]["run_id"]
                    spawn.assert_called_once_with(store, tid, rid)
                self.assertEqual(submitted["data"]["execution"]["timeout_seconds"], 600)
                # Simulate rows from before migration, including a contract with no timeout key.
                with store.tx() as con:
                    spec = store.get_task(tid)["spec"]
                    spec["worker_contract"].pop("timeout_seconds")
                    con.execute("UPDATE tasks SET spec_timeout_source = NULL, spec_json = ? WHERE id = ?", (json.dumps(spec), tid))
                    con.execute("UPDATE runs SET timeout_seconds = NULL, timeout_source = NULL WHERE id = ?", (rid,))
                requests = []

                def worker(req):
                    requests.append(req.timeout_seconds)
                    return WorkerResult(status="blocked", summary="Synthetic timeout", blocker="TIMEOUT")

                with patch.object(executor, "get_worker_runner", return_value=worker):
                    with patch.object(executor.sys, "argv", ["ato.executor", "_run", tid, rid, str(store.db_file)]):
                        self.assertEqual(executor._main(), 0)
                    legacy = executor.task_status_view(store, tid)
                    self.assertEqual((legacy["execution"]["timeout_seconds"], legacy["execution"]["timeout_source"]), (600, "legacy"))
                    with patch.object(executor, "_spawn_background_run") as spawn:
                        code, resumed = run_cli(["resume", tid, "--timeout-seconds", "3600"], store=store)
                        self.assertEqual(code, 0)
                        rid = resumed["data"]["execution"]["run_id"]
                        spawn.assert_called_once_with(store, tid, rid)
                    with patch.object(executor.sys, "argv", ["ato.executor", "_run", tid, rid, str(store.db_file)]):
                        self.assertEqual(executor._main(), 0)
                code, resumed = run_cli(["resume", tid, "--foreground"], store=store, worker_runner=worker)
                self.assertEqual(code, 0)
                self.assertEqual(requests, [600, 3600, 600])
                self.assertEqual([(r["timeout_seconds"], r["timeout_source"]) for r in resumed["data"]["run_history"]],
                                 [(600, "legacy"), (3600, "resume_override"), (600, "legacy")])

    def test_invalid_resume_timeouts_are_usage_errors_without_mutation(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp).resolve() / "ato.db")
            tid = "task-" + "a" * 32
            invalid = ["", "0", "4", "7201", "-5", "+5", "5.0", "3.6e3", " 5", "5 ", "5\n",
                       "0x10", "1_000", "NaN", "５", "٣٦٠٠", "9" * 5000]
            for value in invalid:
                with self.subTest(value=value[:30]):
                    code, result = run_cli(["resume", tid, "--timeout-seconds=" + value], store=store)
                    self.assertEqual(code, 2)
                    self.assertEqual(result["error"]["code"], "USAGE")
            for options in (["--timeout-seconds"], ["--timeout-seconds", "5", "--timeout-seconds", "6"]):
                code, result = run_cli(["resume", tid, *options], store=store)
                self.assertEqual((code, result["error"]["code"]), (2, "USAGE"))
            for value in (True, False, 5.0, "3600", 4, 7201, -5):
                with self.subTest(api_value=value), self.assertRaises(TaskError) as ctx:
                    executor.resume(store, tid, timeout_seconds=value)
                self.assertEqual((ctx.exception.exit_code, ctx.exception.code), (2, "USAGE"))
            self.assertEqual(store.list_tasks(), [])
            self.assertEqual(store.list_runs(tid), [])
            self.assertEqual(store.list_events(tid), [])

    def test_full_cli_lifecycle_validate_run_diff_recheck_accept_cleanup(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato"), "ATO_SUBMISSION_KIND": "agy"}):
                repo = init_repo(base / "repo")
                sp = make_spec(
                    repo, base / "spec.json",
                    worker_contract={
                        "objective": "Create hello.txt",
                        "scope": "hello.txt",
                        "authorization": "local",
                        "acceptance": [
                            {"id": "check-hello", "kind": "command", "description": "hello exists",
                             "checks": [["sh", "-c", 'test "$(cat hello.txt)" = "hi"']]},
                            {"id": "user-ok", "kind": "manual", "description": "User sign-off"},
                        ],
                    },
                )
                store = Store()

                code, val = run_cli(["project", "validate", "--repo", str(repo), "--spec", str(sp)], store=store)
                self.assertEqual(code, 0)
                self.assertTrue(val["ok"])

                def fake_worker(req):
                    (req.worktree / "hello.txt").write_text("hi\n", encoding="utf-8")
                    return WorkerResult(status="completed", summary="created hello.txt")

                code, res = run_cli(
                    ["run", "--repo", str(repo), "--spec", str(sp), "--foreground"],
                    store=store, worker_runner=fake_worker,
                )
                self.assertEqual(code, 0)
                data = res["data"]
                tid = data["task_id"]
                self.assertEqual(data["status"], "completed")
                self.assertIsNone(data["reason"])
                self.assertEqual(data["checks"][0]["state"], "passed")
                self.assertEqual(data["completion_notification"]["origin"], "agy")
                self.assertEqual(data["completion_notification"]["state"], "unsupported")

                # List, diff --stat, trace, recheck, accept, cleanup
                code, listed = run_cli(["list", "--repo", str(repo)], store=store)
                self.assertEqual(code, 0)
                self.assertEqual([t["task_id"] for t in listed["data"]["tasks"]], [tid])

                code, diff = run_cli(["project", "diff", tid, "--stat"], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(diff["data"]["summary"]["files_added"], ["hello.txt"])
                self.assertIsNone(diff["data"]["patch"])

                code, trace = run_cli(["project", "trace", tid], store=store)
                self.assertEqual(code, 0)
                self.assertTrue(any(e["kind"] == "checks_finished" for e in trace["data"]["events"]))

                code, rechecked = run_cli(["project", "recheck", tid, "--checks-only"], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(rechecked["data"]["status"], "completed")

                code, accepted = run_cli(["accept", tid], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(accepted["data"]["acceptance"]["disposition"], "accepted")

                code, cleaned = run_cli(["project", "cleanup", tid, "--confirm"], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(cleaned["data"]["worktree_state"], "removed")

    def test_checks_failed_and_repair_loop(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato")}):
                repo = init_repo(base / "repo")
                sp = make_spec(
                    repo, base / "spec.json",
                    worker_contract={
                        "objective": "Pass check on attempt 2",
                        "scope": "flag.txt",
                        "authorization": "local",
                        "max_attempts": 2,
                        "acceptance": [
                            {"id": "c1", "kind": "command", "description": "flag ok",
                             "checks": [["sh", "-c", 'test "$(cat flag.txt)" = "ok"']]}
                        ],
                    },
                )
                store = Store()
                calls = {"n": 0}

                def flaky_worker(req):
                    calls["n"] += 1
                    (req.worktree / "flag.txt").write_text("ok\n" if calls["n"] == 2 else "nope\n", encoding="utf-8")
                    return WorkerResult(status="completed", summary=f"attempt {calls['n']}")

                code, res = run_cli(["run", "--repo", str(repo), "--spec", str(sp), "--foreground"],
                                    store=store, worker_runner=flaky_worker)
                self.assertEqual(code, 0)
                self.assertEqual(calls["n"], 2)
                self.assertEqual(res["data"]["status"], "completed")
                self.assertIsNone(res["data"]["reason"])

                # Exhausted checks block and skip review.
                sp2 = make_spec(
                    repo, base / "spec2.json",
                    worker_contract={
                        "objective": "Always fails check",
                        "scope": "none",
                        "authorization": "local",
                        "max_attempts": 1,
                        "acceptance": [{"id": "fail", "kind": "command", "description": "false", "checks": [["false"]]}],
                    },
                    reviewer={"agent": {"kind": "codex"}},
                )
                reviewer_called = {"called": False}

                def never_called_reviewer(req):
                    reviewer_called["called"] = True
                    raise AssertionError("reviewer must be skipped when checks fail")

                code, res2 = run_cli(
                    ["run", "--repo", str(repo), "--spec", str(sp2), "--foreground"],
                    store=store,
                    worker_runner=lambda req: WorkerResult(status="completed", summary="tried"),
                    reviewer_runner=never_called_reviewer,
                )
                self.assertEqual(code, 0)
                self.assertFalse(reviewer_called["called"])
                self.assertEqual(res2["data"]["status"], "blocked")
                self.assertEqual(res2["data"]["reason"], "CHECKS_FAILED")
                self.assertEqual(res2["data"]["checks"][0]["state"], "failed")

    def test_stop_and_resume_and_guardrail_block(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato")}):
                repo = init_repo(base / "repo")
                sp = make_spec(
                    repo, base / "spec.json",
                    guardrails={"protected_paths": ["README.md"]},
                    worker_contract={
                        "objective": "Edit app.py without touching README.md",
                        "scope": "app.py",
                        "authorization": "local",
                        "max_attempts": 1,
                        "acceptance": [{"id": "ok", "kind": "command", "description": "ok", "checks": [["true"]]}],
                    },
                )
                store = Store()

                def violator(req):
                    (req.worktree / "README.md").write_text("# Broken\n", encoding="utf-8")
                    return WorkerResult(status="completed", summary="edited readme")

                code, res = run_cli(["run", "--repo", str(repo), "--spec", str(sp), "--foreground"],
                                    store=store, worker_runner=violator)
                self.assertEqual(code, 0)
                tid = res["data"]["task_id"]
                self.assertEqual(res["data"]["status"], "blocked")
                self.assertEqual(res["data"]["reason"], "GUARDRAIL_VIOLATION")

                # Stop then resume cleanly with a compliant worker
                code, stopped = run_cli(["stop", tid], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(stopped["data"]["status"], "stopped")

                def fixer(req):
                    (req.worktree / "README.md").write_text("# Demo\n", encoding="utf-8")
                    (req.worktree / "app.py").write_text("def add(a, b):\n    return a + b\n# fixed\n", encoding="utf-8")
                    return WorkerResult(status="completed", summary="restored readme")

                code, resumed = run_cli(["resume", tid, "--foreground"], store=store, worker_runner=fixer)
                self.assertEqual(code, 0)
                self.assertEqual(resumed["data"]["status"], "completed")
                self.assertIsNone(resumed["data"]["reason"])

    def test_system_commands_agent_commands_and_background_run_stop(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato"), "CODEX_HOME": str(base / "codex")}):
                repo = init_repo(base / "repo")
                sp = make_spec(repo, base / "spec.json")
                store = Store()

                code, sys_st = run_cli(["system", "status"], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(sys_st["data"]["schema_version"], 2)

                code, pol = run_cli(["system", "notification-policy", "--repo", str(repo)], store=store)
                self.assertEqual(code, 0)
                self.assertTrue(pol["data"]["notifications_enabled"])
                self.assertFalse(pol["data"]["repo_muted"])

                code, doc = run_cli(["agent", "doctor"], store=store)
                self.assertEqual(code, 0)
                self.assertIn("codex", doc["data"]["worker"])

                # Background run spawns detached runner; stop cleanly settles it.
                with patch.dict(os.environ, {"ATO_CODEX_BIN": "/bin/sleep"}):
                    code, submitted = run_cli(["run", "--repo", str(repo), "--spec", str(sp)], store=store)
                    self.assertEqual(code, 0)
                    tid = submitted["data"]["task_id"]
                    code, ask = run_cli(["agent", "ask", tid, "--kind", "clarification",
                                         "--question", "Confirm?", "--reason", "test"], store=store)
                    self.assertEqual(code, 0)
                    self.assertEqual(ask["data"]["state"], "open")
                    code, stopped = run_cli(["stop", tid], store=store)
                    self.assertEqual(code, 0)
                    self.assertEqual(stopped["data"]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
