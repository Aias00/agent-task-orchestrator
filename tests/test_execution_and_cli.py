"""End-to-end CLI tests covering full task lifecycle, repair, advisory checks, stop/resume, and cleanup."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ato import cli
from ato.adapters.base import WorkerResult
from ato.store import Store
from test_core_and_store import init_repo, make_spec


def run_cli(argv: list[str], *, store: Store, worker_runner=None, reviewer_runner=None) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv, store=store, worker_runner=worker_runner, reviewer_runner=reviewer_runner)
    return code, json.loads(buf.getvalue())


class ExecutionAndCLITests(unittest.TestCase):
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

    def test_advisory_checks_failed_and_repair_loop(self):
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

                # Advisory failure settles completed with reason=CHECKS_FAILED (skipping reviewer)
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
                self.assertEqual(res2["data"]["status"], "completed")
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
