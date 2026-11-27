"""Tests for Codex worker/reviewer argv (P0-3), session lookup/queue delivery, and adapter profiles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
import unittest

from ato import codex_notify
from ato.adapters import base, codex
from ato.adapters.base import WorkerRequest


class CodexAndAdaptersTests(unittest.TestCase):
    def test_p0_3_codex_worker_preserves_workspace_write_and_approval_defaults(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp).resolve()
            # AGY / plain CLI submission (empty controller_args): keeps workspace-write & approval_policy=never.
            req = WorkerRequest(
                task_id="task-" + "1" * 32, kind="codex", worktree=p, runtime_dir=p, prompt="hi",
                sandbox="workspace-write", model="gpt-5", permissions={"network_access": False},
                controller_args=(),
            )
            argv = codex.build_worker_argv("/usr/bin/codex", req, p / "schema.json", p / "out.json")
            self.assertIn("--sandbox", argv)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
            self.assertIn('approval_policy="never"', argv)
            self.assertIn("--model", argv)
            self.assertIn('sandbox_workspace_write.network_access=false', argv)

            # Explicit controller overrides win without duplicating flags.
            req_override = WorkerRequest(
                task_id="task-" + "1" * 32, kind="codex", worktree=p, runtime_dir=p, prompt="hi",
                sandbox="workspace-write", model="gpt-5",
                controller_args=("--sandbox", "danger-full-access", "-c", 'approval_policy="on-request"', "-c", 'model="o3"'),
            )
            argv2 = codex.build_worker_argv("/usr/bin/codex", req_override, p / "schema.json", p / "out.json")
            self.assertEqual(argv2.count("--sandbox"), 1)
            self.assertEqual(argv2[argv2.index("--sandbox") + 1], "danger-full-access")
            self.assertNotIn("--model", argv2)
            self.assertNotIn('approval_policy="never"', argv2)

    def test_codex_session_lookup_config_extraction_and_queue_receipt(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            tid = "11111111-2222-3333-4444-555555555555"
            rollout = home / "rollout.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": tid}}) + "\n"
                + json.dumps({
                    "type": "turn_context",
                    "payload": {
                        "model": "o4-mini",
                        "effort": "high",
                        "approval_policy": "never",
                        "sandbox_policy": {"type": "workspace-write", "network_access": False},
                    },
                }) + "\n",
                encoding="utf-8",
            )
            db = home / "state_1.sqlite"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)")
                con.execute("INSERT INTO threads VALUES (?, ?)", (tid, str(rollout)))

            self.assertEqual(codex_notify.session_path(home, tid), str(rollout.resolve()))
            args = codex_notify.worker_config_args(home, tid)
            self.assertIn("--sandbox", args)
            self.assertIn('model="o4-mini"', args)
            self.assertIn('model_reasoning_effort="high"', args)

            # Synthetic `codex queue` binary returning exact receipt
            fake_bin = home / "fake-codex"
            receipt = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            fake_bin.write_text(f"#!/bin/sh\necho 'Queued message {receipt} for thread {tid}.'\n", encoding="utf-8")
            os.chmod(fake_bin, stat.S_IRWXU)
            ok, got_receipt, err = codex_notify.send_queue_notice(home, tid, "hello", queue_bin=str(fake_bin))
            self.assertTrue(ok)
            self.assertEqual(got_receipt, receipt)
            self.assertEqual(err, "")

    def test_sandbox_profiles_secrets_and_doctor(self):
        with TemporaryDirectory() as tmp:
            base_dir = Path(tmp).resolve()
            wt = base_dir / "wt"
            rt = base_dir / "rt"
            wt.mkdir()
            rt.mkdir()
            prof = base.worker_sandbox_profile(wt, rt, "workspace-write")
            self.assertIn("(deny file-write*)", prof)
            self.assertIn(".git", prof)
            ro = base.readonly_sandbox_profile(rt, wt)
            self.assertIn(f'(deny file-write* (subpath "{wt}"))', ro)

            secrets = base.secret_values({"MY_API_KEY": "super-secret-value-99", "NORMAL": "hello"})
            self.assertTrue(base.has_secret({"text": "leaked super-secret-value-99"}, secrets))
            self.assertFalse(base.has_secret({"text": "clean"}, secrets))

            doc = base.doctor_all(probe_versions=False)
            self.assertEqual(set(doc["reviewer_kinds"]), {"codex", "claude", "agy"})
            self.assertIn("acp_pi", doc["worker"])

    def test_run_bounded_subprocess_handles_large_stdout_stderr_without_pipe_deadlock(self):
        import sys
        with TemporaryDirectory() as tmp:
            rc, out, err, code = base.run_bounded_subprocess(
                [sys.executable, "-c",
                 "import sys; sys.stdout.write('A' * 262144); sys.stderr.write('B' * 262144)"],
                cwd=Path(tmp), env=dict(os.environ), timeout=10,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(code, "OK")
            self.assertEqual(len(out), 262144)
            self.assertEqual(len(err), 262144)


if __name__ == "__main__":
    unittest.main()
