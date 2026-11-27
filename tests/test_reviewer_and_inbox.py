"""Tests for multi-agent read-only reviewer loop, mutation tripwire, and human Q&A inbox."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ato.adapters.base import ReviewResult, WorkerResult
from ato.store import Store
from test_core_and_store import init_repo, make_spec
from test_execution_and_cli import run_cli


def _verdict(verdict: str, findings: list[dict] | None = None) -> dict:
    return {
        "verdict": verdict,
        "summary": f"verdict {verdict}",
        "findings": findings or [],
        "checked_items": ["checked"],
        "residual_risks": [],
    }


class ReviewerAndInboxTests(unittest.TestCase):
    def test_reviewer_request_changes_repair_and_approve(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato")}):
                repo = init_repo(base / "repo")
                sp = make_spec(
                    repo, base / "spec.json",
                    worker_contract={
                        "objective": "Update app.py",
                        "scope": "app.py",
                        "authorization": "local",
                        "max_attempts": 3,
                        "acceptance": [{"id": "c1", "kind": "command", "description": "ok", "checks": [["true"]]}],
                    },
                    reviewer={"agent": {"kind": "claude"}, "max_review_rounds": 2},
                )
                store = Store()
                rounds = {"r": 0}

                def worker(req):
                    (req.worktree / "app.py").write_text(f"# attempt {rounds['r']}\n", encoding="utf-8")
                    return WorkerResult(status="completed", summary="done")

                def rev(req):
                    rounds["r"] += 1
                    if rounds["r"] == 1:
                        return ReviewResult(
                            ok=True, code="OK", session_id="rev-1",
                            verdict=_verdict("request_changes", [{
                                "id": "F1", "severity": "blocker", "file": "app.py", "lines": "L1",
                                "category": "correctness", "title": "Fix bug", "evidence": "bad", "required_fix": "fix",
                            }]),
                        )
                    return ReviewResult(ok=True, code="OK", session_id="rev-2", verdict=_verdict("approve"))

                code, res = run_cli(["run", "--repo", str(repo), "--spec", str(sp), "--foreground"],
                                    store=store, worker_runner=worker, reviewer_runner=rev)
                self.assertEqual(code, 0)
                self.assertEqual(rounds["r"], 2)
                self.assertEqual(res["data"]["status"], "completed")
                self.assertEqual(res["data"]["review"]["verdict"], "approve")
                self.assertEqual(res["data"]["review"]["reviewer_kind"], "claude")

    def test_reviewer_mutation_tripwire_blocks_dirty_reviewer(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato")}):
                repo = init_repo(base / "repo")
                sp = make_spec(repo, base / "spec.json", reviewer={"agent": {"kind": "agy"}})
                store = Store()

                def rogue_reviewer(req):
                    (req.worktree / "rogue.txt").write_text("mutated!\n", encoding="utf-8")
                    return ReviewResult(ok=True, code="OK", verdict=_verdict("approve"))

                code, res = run_cli(
                    ["run", "--repo", str(repo), "--spec", str(sp), "--foreground"],
                    store=store,
                    worker_runner=lambda req: WorkerResult(status="completed", summary="ok"),
                    reviewer_runner=rogue_reviewer,
                )
                self.assertEqual(code, 0)
                self.assertEqual(res["data"]["status"], "blocked")
                self.assertEqual(res["data"]["reason"], "MUTATION_VIOLATION")

    def test_worker_needs_input_inbox_answer_and_resume(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            with patch.dict(os.environ, {"ATO_HOME": str(base / "ato")}):
                repo = init_repo(base / "repo")
                sp = make_spec(repo, base / "spec.json")
                store = Store()
                seen_prompts = []

                def asking_worker(req):
                    seen_prompts.append(req.prompt)
                    if len(seen_prompts) == 1:
                        return WorkerResult(status="needs_input", summary="Use tabs or spaces?", blocker="style choice")
                    return WorkerResult(status="completed", summary="done with answer")

                code, res = run_cli(["run", "--repo", str(repo), "--spec", str(sp), "--foreground"],
                                    store=store, worker_runner=asking_worker)
                self.assertEqual(code, 0)
                tid = res["data"]["task_id"]
                self.assertEqual(res["data"]["status"], "waiting_input")

                code, inbox_res = run_cli(["inbox"], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(inbox_res["data"]["count"], 1)
                req_id = inbox_res["data"]["requests"][0]["request_id"]

                # Cannot resume before answering
                code, err = run_cli(["resume", tid, "--foreground"], store=store, worker_runner=asking_worker)
                self.assertEqual(code, 4)
                self.assertEqual(err["error"]["code"], "NOT_RESUMABLE")

                # Answer & conflict check
                code, ans = run_cli(["answer", req_id, "--answer", "Use 4 spaces"], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(ans["data"]["state"], "answered")

                code, conflict = run_cli(["answer", req_id, "--answer", "Use tabs"], store=store)
                self.assertEqual(code, 4)
                self.assertEqual(conflict["error"]["code"], "DECISION_CONFLICT")

                # Resume succeeds and receives answer in prompt
                code, done = run_cli(["resume", tid, "--foreground"], store=store, worker_runner=asking_worker)
                self.assertEqual(code, 0)
                self.assertEqual(done["data"]["status"], "completed")
                self.assertIn("Use 4 spaces", seen_prompts[-1])

                # Also verify `agent reviewer` and `agent binding`
                code, rev_upd = run_cli(["agent", "reviewer", tid, "--kind", "codex", "--effort", "high"], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(rev_upd["data"]["reviewer"]["agent"]["kind"], "codex")
                code, binding = run_cli(["agent", "binding", tid], store=store)
                self.assertEqual(code, 0)
                self.assertEqual(binding["data"]["reviewer"]["agent"]["effort"], "high")


if __name__ == "__main__":
    unittest.main()
