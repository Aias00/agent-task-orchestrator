"""Unit tests for config, spec normalization, SQLite WAL store, and safe process helpers."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from ato import config, process
from ato.spec import TaskError, normalize_spec, valid_id
from ato.store import Store, _DDL


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    (path / ".gitignore").write_text(".codex/tasks/\n", encoding="utf-8")
    (path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (path / "README.md").write_text("# Demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path.resolve()


def make_spec(repo: Path, spec_path: Path, **overrides) -> Path:
    doc = {
        "schema_version": 1,
        "repo": str(repo),
        "base_ref": "HEAD",
        "worker_contract": {
            "objective": "Implement feature",
            "scope": "Only app.py",
            "authorization": "Local test only",
            "acceptance": [
                {"id": "check-1", "kind": "command", "description": "Passes", "checks": [["true"]]}
            ],
        },
    }
    doc.update(overrides)
    spec_path.write_text(json.dumps(doc), encoding="utf-8")
    return spec_path


class CoreAndStoreTests(unittest.TestCase):
    def test_timeout_preview_preserves_canonical_spec_and_hash(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato"), "CODEX_HOME": str(base / "codex")}):
                repo = init_repo(base / "repo")
                sp = make_spec(repo, base / "spec.json")
                raw = json.loads(sp.read_text())
                # Explicit pre-change canonical shape: metadata must never enter this document.
                expected = {
                    "schema_version": 1, "repo": str(repo), "repo_key": str(repo / ".git"),
                    "base_ref": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
                    "worker_contract": {**raw["worker_contract"], "agent": {"kind": "codex"},
                                        "sandbox": "workspace-write", "max_attempts": 3, "timeout_seconds": 600},
                    "reviewer": None, "worktree": {"shared_env_links": []},
                }
                for explicit in (False, True):
                    if explicit:
                        raw["worker_contract"]["timeout_seconds"] = 600
                    sp.write_text(json.dumps(raw))
                    norm = normalize_spec(repo, sp)
                    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                    self.assertEqual(norm.canonical, canonical)
                    self.assertEqual(norm.spec_sha256, hashlib.sha256(canonical).hexdigest())
                    self.assertEqual(norm.preview()["timeout_seconds"], 600)
                    self.assertEqual(norm.timeout_source, "spec" if explicit else "default")
                    self.assertEqual(norm.preview()["timeout_source"], norm.timeout_source)
                    self.assertIsNone(norm.reviewer_timeout_source)
                    self.assertNotIn("reviewer_timeout_seconds", norm.preview())
                    self.assertNotIn("reviewer_timeout_source", norm.preview())
                for explicit in (False, True):
                    raw["reviewer"] = {"agent": {"kind": "codex"}}
                    if explicit:
                        raw["reviewer"]["timeout_seconds"] = 300
                    sp.write_text(json.dumps(raw))
                    norm = normalize_spec(repo, sp)
                    self.assertEqual(norm.preview()["reviewer_timeout_seconds"], 300)
                    self.assertEqual(norm.reviewer_timeout_source, "spec" if explicit else "default")
                    self.assertEqual(norm.preview()["reviewer_timeout_source"], norm.reviewer_timeout_source)
                    if explicit:
                        self.assertEqual(norm.spec_sha256, default_reviewer_hash)
                    else:
                        default_reviewer_hash = norm.spec_sha256

    def test_existing_v2_schema_migration_is_idempotent_and_lossless(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp).resolve() / "ato.db"
            old_ddl = _DDL.replace("  spec_timeout_source TEXT,\n", "").replace(
                "  timeout_seconds INTEGER,\n", "").replace("  timeout_source TEXT,\n", "")
            tid, rid = "task-" + "a" * 32, "run-" + "b" * 32
            with sqlite3.connect(db) as con:
                con.executescript(old_ddl)
                con.execute("PRAGMA user_version = 2")
                con.execute("""INSERT INTO tasks (id, repo_root, repo_key, base_commit, branch, worktree_path,
                            spec_json, spec_sha256, status, active_run_id, acceptance_json, created_at, updated_at)
                            VALUES (?, '/repo', '/repo/.git', ?, 'branch', '/wt', ?, ?, 'stopped', ?, ?, 1, 2)""",
                            (tid, "c" * 40, json.dumps({"worker_contract": {"timeout_seconds": 42}}),
                             "d" * 64, rid, '{"disposition":"accepted","criteria":[]}'))
                con.execute("INSERT INTO runs(id, task_id, kind, phase, created_at) VALUES (?, ?, 'initial', 'settled', 1)",
                            (rid, tid))
                con.execute("INSERT INTO events(task_id, run_id, kind, detail_json, created_at) VALUES (?, ?, 'task_submitted', '{}', 1)",
                            (tid, rid))
                tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                columns = {table: [r[1] for r in con.execute(f"PRAGMA table_info({table})")] for table in tables}
                before = {table: con.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
            for _ in range(3):
                store = Store(db)
                with sqlite3.connect(db) as con:
                    self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 2)
                    for table in tables:
                        self.assertEqual(con.execute(f"SELECT {', '.join(columns[table])} FROM {table}").fetchall(), before[table])
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertIsNone(store.get_task(tid)["spec_timeout_source"])
                self.assertIsNone(store.get_run(rid)["timeout_seconds"])
                self.assertIsNone(store.get_run(rid)["timeout_source"])
            new_run = store.create_run(tid, "resume", timeout_seconds=3600, timeout_source="resume_override")
            self.assertEqual((new_run["timeout_seconds"], new_run["timeout_source"]), (3600, "resume_override"))

    def test_submission_origin_agy_takes_precedence_over_ambient_codex_thread(self):
        tid = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(
            config.detect_submission_origin({"ATO_SUBMISSION_KIND": "agy", "CODEX_THREAD_ID": tid}),
            ("agy", None),
        )
        self.assertEqual(
            config.detect_submission_origin({"ANTIGRAVITY_CONVERSATION_ID": "conv-1", "CODEX_THREAD_ID": tid}),
            ("agy", None),
        )
        self.assertEqual(config.detect_submission_origin({"CODEX_THREAD_ID": tid}), ("codex", tid))
        self.assertEqual(config.detect_submission_origin({}), ("cli", None))

    def test_spec_normalization_guardrails_reviewer_and_errors(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = init_repo(base / "repo")
            sp = make_spec(
                repo, base / "spec.json",
                guardrails={"protected_paths": ["README.md"], "forbid_delete_paths": ["app.py"]},
                reviewer={"agent": {"kind": "codex"}, "design_docs": ["README.md"], "checklist": ["Check tests"]},
            )
            norm = normalize_spec(repo, sp)
            self.assertEqual(norm.repository.root, str(repo))
            self.assertEqual(norm.document["guardrails"]["protected_paths"], ["README.md"])
            self.assertEqual(norm.document["reviewer"]["design_docs"][0]["path"], "README.md")
            self.assertEqual(len(norm.spec_sha256), 64)

            # Unsupported schema version -> UNSUPPORTED_VERSION (exit 2)
            bad = make_spec(repo, base / "bad.json", schema_version=99)
            with self.assertRaises(TaskError) as ctx:
                normalize_spec(repo, bad)
            self.assertEqual(ctx.exception.code, "UNSUPPORTED_VERSION")
            self.assertEqual(ctx.exception.exit_code, 2)

            # Invalid guardrail path -> INVALID_SPEC
            bad_g = make_spec(repo, base / "bad_g.json", guardrails={"protected_paths": ["../escape"]})
            with self.assertRaises(TaskError) as ctx:
                normalize_spec(repo, bad_g)
            self.assertEqual(ctx.exception.code, "INVALID_SPEC")

    def test_sqlite_store_crud_and_notification_dedupe(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            store = Store(base / "ato.db")
            task = store.create_task(
                task_id="task-" + "a" * 32,
                repo_root=str(base / "repo"),
                repo_key=str(base / "repo/.git"),
                base_commit="1" * 40,
                branch="ato/task-" + "a" * 32,
                worktree_path=str(base / "wt"),
                spec_doc={"schema_version": 1, "worker_contract": {"acceptance": []}},
                spec_sha256="b" * 64,
                origin_kind="codex",
                origin_session_id="11111111-2222-3333-4444-555555555555",
            )
            self.assertEqual(task["status"], "queued")
            self.assertTrue(valid_id(task["id"], "task"))

            # Idempotent deduplicated notification enqueue
            n1 = store.enqueue_notification(
                dedupe_key="completion:task1:run1", task_id=task["id"], kind="completion",
                target_session=task["origin_session_id"], body="done",
            )
            n2 = store.enqueue_notification(
                dedupe_key="completion:task1:run1", task_id=task["id"], kind="completion",
                target_session=task["origin_session_id"], body="done again",
            )
            self.assertEqual(n1["id"], n2["id"])
            self.assertEqual(len(store.pending_notifications()), 1)

    def test_process_identity_probe_exited_and_stop_group(self):
        self.assertIsNotNone(process.identity(os.getpid()))
        self.assertEqual(process.disposition(os.getpid(), process.identity(os.getpid())), "live")
        self.assertEqual(process.disposition(os.getpid(), "0" * 64), "reused")

        # Child that exits immediately: probe_exited detects zombie WITHOUT reaping.
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        try:
            deadline = time.monotonic() + 5.0
            while not process.probe_exited(proc) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(process.probe_exited(proc))
            self.assertIsNone(proc.returncode)  # Still unreaped!
        finally:
            proc.wait()

        # Sleeping child stopped cleanly via stop_pid_group
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            birth = process.identity(sleeper.pid)
            self.assertIsNotNone(birth)
            self.assertTrue(process.stop_pid_group(sleeper.pid, birth, grace_seconds=1.0))
        finally:
            sleeper.wait()


if __name__ == "__main__":
    unittest.main()
