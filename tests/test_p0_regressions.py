"""Offline regression coverage for process ownership, retention, and review receipts."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from ato import executor, process, product, reviewer, worktree
from ato.adapters import agy, base, claude, codex
from ato.spec import CATEGORIES, REVIEW_VERDICT_SCHEMA, SEVERITIES, TaskError, validate_review_verdict
from ato.store import Store, _DDL
from test_core_and_store import init_repo, make_spec
from test_execution_and_cli import run_cli

ROOT = Path(__file__).resolve().parents[1]


def verdict(kind="approve"):
    return {"verdict": kind, "summary": "Reviewed", "findings": [],
            "checked_items": ["correctness"], "residual_risks": []}


def finding():
    return {"id": "F1", "severity": "blocker", "category": "correctness", "file": "app.py",
            "lines": "L1-L2", "title": "Bug", "evidence": "Reproduced", "required_fix": "Fix bug"}


class IsolatedRegressionTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {"ATO_HOME": str(self.root / "ato"),
                                     "CODEX_HOME": str(self.root / "codex"), "ATO_SUBMISSION_KIND": "agy"})
        env.start()
        self.addCleanup(env.stop)
        self.repo = init_repo(self.root / "repo")
        self.store = Store()

    def task(self, **overrides):
        sp = make_spec(self.repo, self.root / "spec.json", **overrides)
        with patch.object(executor, "_spawn_background_run"):
            status = executor.submit(self.store, self.repo, sp)
        task = self.store.get_task(status["task_id"])
        return task, self.store.get_run(task["active_run_id"])

    def test_guardrails_see_files_beyond_public_limit(self):
        # Both violations sort after the first 200 public rows.
        (self.repo / "zz_deleted").write_text("retained\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.repo, check=True)
        task, _ = self.task(guardrails={"protected_paths": ["zz_protected"], "forbid_delete_paths": ["zz_deleted"]})
        wt = Path(task["worktree_path"])
        for i in range(205):
            (wt / f"a{i:03}").write_text("new\n")
        (wt / "zz_protected").write_text("new\n")
        (wt / "zz_deleted").unlink()
        diff = product.collect_diff(task)
        self.assertTrue(diff["summary"]["truncated"])
        self.assertEqual(len(diff["files"]), 200)
        self.assertEqual(len(product._collect_diff_rows(task)), 207)
        expected = {("protected_paths", "zz_protected"), ("forbid_delete_paths", "zz_deleted")}
        for supplied in (None, diff):
            self.assertEqual({(v["rule"], v["path"]) for v in product.check_guardrails(task, task["spec"], diff=supplied)}, expected)

    def test_retention_preserves_real_submodule(self):
        sub = init_repo(self.root / "submodule-source")
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "module"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qam", "submodule"], cwd=self.repo, check=True)
        task, _ = self.task()
        wt = Path(task["worktree_path"])
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init"],
                       cwd=wt, check=True, capture_output=True)
        (wt / "app.py").write_text("retained = True\n")
        expected = worktree.git_text(sub, "rev-parse", "HEAD")
        retained = worktree.commit_worktree_product(task)
        self.assertEqual(worktree.git_text(wt, "ls-tree", retained, "module"), f"160000 commit {expected}\tmodule")
        # Even an unpopulated/missing submodule must not silently drop the gitlink.
        shutil.rmtree(wt / "module")
        retained = worktree.commit_worktree_product(task)
        self.assertIn(f"160000 commit {expected}", worktree.git_text(wt, "ls-tree", retained, "module"))

    def test_retention_keeps_detached_and_switched_commits_clean_and_dirty(self):
        for switched in (False, True):
            for dirty in (False, True):
                with self.subTest(switched=switched, dirty=dirty):
                    task, _ = self.task()
                    wt = Path(task["worktree_path"])
                    checkout = ["checkout", "-qb", "other-" + task["id"]] if switched else ["checkout", "--detach", "-q"]
                    worktree.git_run(wt, *checkout)
                    (wt / "committed.txt").write_text("worker commit\n")
                    subprocess.run(["git", "add", "."], cwd=wt, check=True)
                    subprocess.run(["git", "commit", "-qm", "worker"], cwd=wt, check=True)
                    head = worktree.git_text(wt, "rev-parse", "HEAD")
                    if dirty:
                        (wt / "dirty.txt").write_text("uncommitted\n")
                    retained = worktree.commit_worktree_product(task)
                    self.assertEqual(worktree.git_text(wt, "rev-parse", "refs/heads/" + task["branch"]), retained)
                    self.assertEqual(worktree.git_text(wt, "show", retained + ":committed.txt"), "worker commit")
                    if dirty:
                        self.assertEqual(worktree.git_text(wt, "rev-parse", retained + "^"), head)
                        self.assertEqual(worktree.git_text(wt, "show", retained + ":dirty.txt"), "uncommitted")
                    else:
                        self.assertEqual(retained, head)

    def test_atomic_startup_cannot_resurrect_stopped_or_settled_runs(self):
        task, run = self.task()
        self.assertTrue(self.store.mark_run_running(task["id"], run["id"], 123, "birth"))
        self.assertTrue(self.store.claim_run_execution(task["id"], run["id"], 124, "new"))
        self.assertFalse(self.store.mark_run_running(task["id"], run["id"], 123, "birth"))
        self.assertEqual(self.store.get_run(run["id"])["pid"], 124)
        self.store.update_task(task["id"], stop_requested=True, status="stopped")
        worker = Mock()
        executor.execute_run(self.store, task["id"], run["id"], worker_runner=worker)
        worker.assert_not_called()
        self.assertEqual(self.store.get_task(task["id"])["status"], "stopped")
        self.assertFalse(self.store.mark_run_running(task["id"], run["id"], 123, "birth"))
        self.store.update_run(run["id"], phase="settled", outcome="stopped")
        self.store.update_task(task["id"], stop_requested=False)
        self.assertFalse(self.store.claim_run_execution(task["id"], run["id"], 123, "birth"))
        new = self.store.create_run(task["id"], "resume")
        self.assertFalse(self.store.claim_run_execution(task["id"], run["id"], 123, "birth"))
        self.assertTrue(self.store.claim_run_execution(task["id"], new["id"], 123, "birth"))

    def test_background_parent_does_not_overwrite_fast_exit(self):
        for birth in (None, "birth"):
            with self.subTest(birth=birth):
                task, run = self.task()
                def spawn(*args, **kwargs):
                    self.store.update_run(run["id"], phase="settled", outcome="completed")
                    self.store.update_task(task["id"], status="completed")
                    return Mock(pid=123)
                with patch.object(executor.subprocess, "Popen", side_effect=spawn), patch.object(process, "identity", return_value=birth):
                    executor._spawn_background_run(self.store, task["id"], run["id"])
                self.assertEqual(self.store.get_task(task["id"])["status"], "completed")
                self.assertEqual(self.store.get_run(run["id"])["phase"], "settled")

    def test_child_columns_migrate_idempotently(self):
        db = self.root / "legacy.db"
        with sqlite3.connect(db) as con:
            con.executescript(_DDL.replace("  child_pid INTEGER,\n", "").replace("  child_pid_identity TEXT,\n", ""))
        Store(db)
        Store(db)
        with sqlite3.connect(db) as con:
            columns = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
        self.assertTrue({"child_pid", "child_pid_identity"} <= columns)

    def test_missing_check_executable_is_failed_receipt(self):
        task, _ = self.task()
        task["spec"]["worker_contract"]["acceptance"][0]["checks"] = [[str(self.root / "missing")]]
        rows, passed = product.run_checks(task, task["spec"])
        self.assertFalse(passed)
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("invocation failed", rows[0]["output_excerpt"])

    def test_review_retries_format_without_reimplementing_product(self):
        for mode in ("fresh_only", "resume_if_supported"):
            for kind in ("approve", "request_changes"):
                with self.subTest(mode=mode, kind=kind):
                    task, run = self.task(reviewer={"agent": {"kind": "codex"}, "session_mode": mode})
                    requests = []
                    detail = "findings[0].lines: must use L1 or L1-L2 format with positive line numbers"
                    corrected = verdict(kind)
                    if kind == "request_changes":
                        corrected["findings"] = [finding()]
                    callback = Mock()
                    def runner(req):
                        requests.append(req)
                        if len(requests) == 1:
                            return base.ReviewResult(False, "INVALID_PROTOCOL", session_id="session", detail=detail)
                        return base.ReviewResult(True, "OK", verdict=corrected)
                    result = reviewer.run_review_turn(task, task["spec"], run["id"], runner_override=runner, on_child=callback)
                    self.assertTrue(result.ok)
                    self.assertEqual(result.verdict["verdict"], kind)
                    self.assertEqual(len(requests), 2)
                    self.assertIn(detail, requests[1].prompt)
                    self.assertIs(requests[1].on_child, callback)
                    self.assertEqual(requests[1].resume_session_id, None if mode == "fresh_only" else "session")

    def test_format_repairs_do_not_consume_worker_attempts_or_review_rounds(self):
        task, run = self.task(reviewer={"agent": {"kind": "codex"}, "max_review_rounds": 2})
        changes = verdict("request_changes")
        changes["findings"] = [finding()]
        runner = Mock(side_effect=[
            base.ReviewResult(False, "INVALID_PROTOCOL", detail="findings[0].lines: use L1"),
            base.ReviewResult(True, "OK", verdict=changes),
            base.ReviewResult(False, "INVALID_PROTOCOL", detail="verdict: approve forbids blockers"),
            base.ReviewResult(True, "OK", verdict=verdict()),
        ])
        worker = Mock(return_value=base.WorkerResult("completed", "Done"))
        result = executor.execute_run(self.store, task["id"], run["id"],
                                      worker_runner=worker, reviewer_runner=runner)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(runner.call_count, 4)
        self.assertEqual(self.store.latest_review(task["id"])["round"], 2)

    def test_exhausted_review_records_actionable_detail(self):
        task, run = self.task(reviewer={"agent": {"kind": "codex"}})
        detail = "findings[0].severity: must be one of: blocker, major, minor, nit"
        runner = Mock(return_value=base.ReviewResult(False, "INVALID_PROTOCOL", detail=detail))
        result = executor.execute_run(self.store, task["id"], run["id"],
                                      worker_runner=lambda req: base.WorkerResult("completed", "Done"), reviewer_runner=runner)
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(result["reason"], "INVALID_PROTOCOL")
        self.assertEqual(result["next_step"], detail)
        events = [e for e in self.store.list_events(task["id"]) if e["kind"] == "review_failed"]
        self.assertEqual(events[0]["detail"]["detail"], detail)

    def test_review_override_tripwire_stop_and_redaction_survive_retries(self):
        task, run = self.task()
        task["reviewer"] = {"agent": {"kind": "codex"}, "checklist": ["Override checklist"]}
        self.assertIn("Override checklist", reviewer.build_review_prompt(task, task["spec"], round_num=1))
        def mutate(req):
            (req.worktree / "rogue").write_text("bad")
            return base.ReviewResult(False, "INVALID_PROTOCOL", detail="wrong")
        runner = Mock(side_effect=mutate)
        result = reviewer.run_review_turn(task, task["spec"], run["id"], runner_override=runner)
        self.assertEqual(result.code, "MUTATION_VIOLATION")
        self.assertEqual(runner.call_count, 1)
        runner.reset_mock()
        result = reviewer.run_review_turn(task, task["spec"], run["id"], runner_override=runner, stop_check=lambda: True)
        self.assertEqual(result.code, "STOPPED")
        runner.assert_not_called()
        with patch.dict(os.environ, {"TEST_API_KEY": "synthetic-secret-value"}):
            runner = Mock(return_value=base.ReviewResult(False, "INVALID_PROTOCOL", detail="synthetic-secret-value bad format"))
            result = reviewer.run_review_turn(task, task["spec"], run["id"], runner_override=runner)
        self.assertNotIn("synthetic-secret-value", result.detail)
        self.assertNotIn("synthetic-secret-value", runner.call_args.args[0].prompt)

    def test_adapter_errors_preserve_details_session_and_redact(self):
        for module in (codex, claude, agy):
            for malformed in (False, True):
                with self.subTest(adapter=module.__name__, malformed=malformed):
                    rt = self.root / (module.__name__ + str(malformed))
                    rt.mkdir()
                    doc = verdict()
                    doc["findings"] = [finding()]
                    doc["findings"][0]["lines"] = "1-2"
                    (rt / "review-output.json").write_text("{" if malformed else json.dumps(doc))
                    if module is codex:
                        output = json.dumps({"type": "thread.started", "thread_id": "session"}).encode()
                    elif module is agy:
                        output = b"{" if malformed else json.dumps({"status": "SUCCESS", "conversation_id": "session", "structured_output": doc}).encode()
                    else:
                        output = b"{" if malformed else json.dumps({"type": "result", "structured_output": doc}).encode()
                    req = base.ReviewRequest("task", module.__name__, self.repo, rt, "review")
                    with patch.object(module.config, "resolve_executable", return_value="fake"), \
                         patch.object(Path, "is_file", return_value=True), \
                         patch.object(module, "run_bounded_subprocess", return_value=(0, output, b"", "OK")):
                        result = module.run_review(req)
                    self.assertEqual(result.code, "INVALID_PROTOCOL")
                    self.assertTrue(result.detail)
                    if not malformed:
                        self.assertIn("findings[0].lines", result.detail)
                        self.assertTrue(result.session_id)
        with patch.dict(os.environ, {"TEST_API_KEY": "synthetic-secret-value"}):
            self.assertNotIn("synthetic-secret-value", base.safe_review_detail(ValueError("bad synthetic-secret-value"), base.secret_values()))


class ReceiptValidationTests(unittest.TestCase):
    def test_schema_constraints_and_validator_details(self):
        props = REVIEW_VERDICT_SCHEMA["properties"]["findings"]["items"]["properties"]
        self.assertEqual(props["severity"]["enum"], list(SEVERITIES))
        self.assertEqual(props["category"]["enum"], list(CATEGORIES))
        for field, good, bad in (("id", "F_1", "has space"), ("lines", "L1-L10", "1-10")):
            self.assertIsNotNone(re.fullmatch(props[field]["pattern"], good))
            self.assertIsNone(re.fullmatch(props[field]["pattern"], bad))
        self.assertEqual(props["file"]["maxLength"], 1024)
        for field in ("checked_items", "residual_risks"):
            self.assertEqual(REVIEW_VERDICT_SCHEMA["properties"][field]["items"],
                             {"type": "string", "minLength": 1, "maxLength": 8192})
        good = verdict("request_changes")
        good["findings"] = [finding()]
        validate_review_verdict(good)
        for field, invalid in (("id", "bad id"), ("severity", "critical"), ("category", "other"),
                               ("file", "../escape"), ("file", "a" * 1025), ("lines", "L0"),
                               ("title", "  "), ("evidence", "\0"), ("required_fix", 5)):
            with self.subTest(field=field, invalid=invalid):
                doc = deepcopy(good)
                doc["findings"][0][field] = invalid
                with self.assertRaises(TaskError) as caught:
                    validate_review_verdict(doc)
                self.assertEqual(caught.exception.code, "INVALID_EVIDENCE")
                self.assertIn(f"findings[0].{field}", str(caught.exception))
        for doc, detail in (({**good, "findings": [finding(), finding()]}, "duplicate id"),
                            ({**good, "verdict": "approve"}, "approve"),
                            (verdict("request_changes"), "requires at least one blocker"),
                            ({**good, "summary": " "}, "summary"),
                            ({**good, "findings": {}}, "findings"),
                            ({**good, "checked_items": [""]}, "checked_items[0]"),
                            ({**good, "residual_risks": [" "]}, "residual_risks[0]"),
                            ({**good, "extra": 1}, "exactly these fields")):
            with self.subTest(detail=detail), self.assertRaises(TaskError) as caught:
                validate_review_verdict(doc)
            self.assertIn(detail, str(caught.exception))
        # JSON Schema length counts characters, not UTF-8 bytes.
        doc = verdict()
        doc["summary"] = "中" * 8192
        validate_review_verdict(doc)


class PathGuardRegressionTests(unittest.TestCase):
    def test_symlink_dotdot_case_sensitive_names_and_internal_links(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required to verify the shipped Pi guard")
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "inside").mkdir()
            (root / "inside" / "ok").write_text("ok")
            (root / "internal").symlink_to("inside")
            (root / "absolute").symlink_to(root / "inside")
            (root / "y").symlink_to("/etc/pam.d")
            (root / "x").symlink_to("y/../passwd")
            (root / "sensitive").symlink_to(".GIT/../inside")
            (root / "loop").symlink_to("loop")
            script = f"""
import assert from 'node:assert/strict';
import {{allowed}} from {json.dumps(str(ROOT / 'pi-path-guard.mjs'))};
const root={json.dumps(str(root))};
for(const name of ['x','y/../passwd','.GIT/config','.ENV','.cursor/mcp.json',
                   '.CuRsOr/mcp.json','sensitive/ok','loop','../outside','inside/../../outside','bad\\0name'])
  assert.equal(allowed(root,name,'write',false),false,name);
for(const name of ['inside/ok','internal/ok','internal/../new','absolute/ok','new/path',root+'/internal/ok'])
  assert.equal(allowed(root,name,'write',false),true,name);
assert.equal(allowed(root,'internal/ok','write',true),false);
"""
            completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
