"""Unit tests for config, spec normalization, SQLite WAL store, and safe process helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from ato import config, process
from ato.spec import TaskError, normalize_spec, valid_id
from ato.store import Store


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
