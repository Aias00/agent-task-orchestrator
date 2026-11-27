"""Tests for isolated Git worktrees, structured diff, guardrails, and product retention."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ato import product, worktree
from ato.spec import normalize_spec
from ato.store import Store
from test_core_and_store import init_repo, make_spec


class WorktreeDiffGuardrailsTests(unittest.TestCase):
    def test_worktree_diff_guardrails_and_branch_retention(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            env = {"ATO_HOME": str(base / "ato")}
            with patch.dict(os.environ, env):
                repo = init_repo(base / "repo")
                sp = make_spec(
                    repo, base / "spec.json",
                    guardrails={"protected_paths": ["README.md"], "forbid_delete_paths": ["tests"]},
                )
                norm = normalize_spec(repo, sp)
                store = Store()
                tid = "task-" + "1" * 32
                wt_path, branch = worktree.create_worktree(repo, tid, norm.document["base_ref"])
                task = store.create_task(
                    task_id=tid, repo_root=str(repo), repo_key=norm.repository.key,
                    base_commit=norm.document["base_ref"], branch=branch, worktree_path=wt_path,
                    spec_doc=norm.document, spec_sha256=norm.spec_sha256,
                    origin_kind="cli", origin_session_id=None,
                )
                wt = Path(wt_path)
                before_fp = worktree.tree_fingerprint(wt)

                # Modify app.py, add untracked test_extra.py, touch protected README.md
                (wt / "app.py").write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
                (wt / "test_extra.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
                (wt / "README.md").write_text("# Tampered\n", encoding="utf-8")
                self.assertNotEqual(before_fp, worktree.tree_fingerprint(wt))

                diff = product.collect_diff(task, include_patch=True)
                self.assertEqual(diff["summary"]["added_count"], 1)
                self.assertEqual(diff["summary"]["modified_count"], 2)
                self.assertIn("test_extra.py", diff["summary"]["files_added"])
                self.assertIn("def add(a, b):", diff["patch"])

                violations = product.check_guardrails(task, norm.document, diff=diff)
                self.assertEqual(len(violations), 1)
                self.assertEqual(violations[0]["rule"], "protected_paths")
                self.assertEqual(violations[0]["path"], "README.md")

                # Revert README.md -> guardrail passes, commit product to branch & remove worktree
                (wt / "README.md").write_text("# Demo\n", encoding="utf-8")
                self.assertEqual(product.check_guardrails(task, norm.document), [])

                commit = worktree.commit_worktree_product(task)
                self.assertEqual(len(commit), 40)
                worktree.remove_worktree(task)
                task = store.update_task(tid, worktree_state="removed", status="completed")
                self.assertFalse(wt.exists())

                # Post-cleanup diff reads cleanly from retained task branch
                retained_diff = product.collect_diff(task, include_patch=False)
                self.assertIn("test_extra.py", retained_diff["summary"]["files_added"])
                self.assertIn("app.py", retained_diff["summary"]["files_modified"])


if __name__ == "__main__":
    unittest.main()
