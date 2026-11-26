"""Product visibility and explicit path rules on real local Git trees."""
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ato_task import ato_product as product
from test_ato_unified_task_store import git


class ProductDiffTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(TemporaryDirectory())).resolve()
        git(self.repo, "init", "-q")
        for name in ("app/old", "app/change", "tests/old", "tests/change", "apple.py"):
            self.write(name, "before\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.task = dict(task_id="fixture", spec_sha256="a"*64, repo=dict(root=str(self.repo)),
                         worktree_removed=False, worktree=dict(path=str(self.repo), git_admin=str(self.repo/".git"),
                         base_commit=git(self.repo, "rev-parse", "HEAD"), shared_link_identities={"venv": {}}))

    def write(self, name, text):
        path = self.repo/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_summary_and_manifest_rules_classify_changes_against_base(self):
        for name in ("app/old", "tests/old"):
            (self.repo/name).unlink()
        for name in ("app/change", "tests/change", "apple.py", "app/new", "staged"):
            self.write(name, "after\n")
        git(self.repo, "add", "staged")
        self.write(".codex/tasks/runtime", "private")
        (self.repo/"venv").symlink_to(self.repo/".git")
        git(self.repo, "commit", "-qm", "new tracked file")
        manifest = product.measure(self.task)
        summary = product.summarize_diff(self.task, manifest)
        self.assertEqual(summary["files_added"], ["app/new", "staged"])
        self.assertEqual(summary["files_modified"], ["app/change", "apple.py", "tests/change"])
        self.assertEqual(summary["files_deleted"], ["app/old", "tests/old"])
        self.assertEqual(summary["tests_modified"], ["tests/change"])
        self.assertEqual(summary["tests_deleted"], ["tests/old"])
        self.assertEqual((summary["added_count"], summary["modified_count"], summary["deleted_count"],
                          summary["lines_added"], summary["lines_deleted"], summary["truncated"]), (2,3,2,5,5,False))
        rules = dict(protected_paths=["app"], forbid_delete_paths=["tests"])
        violations = product.check_guardrails(self.task, dict(guardrails=rules), manifest)
        self.assertEqual([(v["path"], v["change"]) for v in violations],
                         [("app/change","modified"), ("app/new","added"), ("app/old","deleted"), ("tests/old","deleted")])
        self.assertEqual(product.check_guardrails(self.task, {}, manifest), [])

    def test_recreated_index_deletion_binary_link_fifo_and_patch(self):
        git(self.repo, "rm", "--cached", "app/change")
        self.assertEqual(product.collect_diff(self.task)["files"], [])
        self.write("app/change", "changed\n")
        (self.repo/"binfile").write_bytes(b"binary\0")
        with TemporaryDirectory() as outside:
            secret = Path(outside)/"secret"
            secret.write_text("external-content-sentinel")
            (self.repo/"link").symlink_to(secret)
            os.mkfifo(self.repo/"fifo")
            result = product.collect_diff(self.task, include_patch=True)
        self.assertEqual(result["summary"]["files_modified"], ["app/change"])
        row = result["files"][0]
        self.assertEqual((row["lines_added"], row["lines_deleted"]), (1, 1))
        self.assertNotIn("external-content-sentinel", result["patch"])
        self.assertNotIn(str(secret), result["patch"])
        self.assertIn("+changed", result["patch"])
        self.assertIn("Binary files", result["patch"])
        self.assertTrue(result["files"][1]["binary"])
        self.assertFalse(result["patch_truncated"])

    def test_diff_disables_external_drivers_and_preserves_git_index(self):
        # The marker would appear if any configured program ran.
        marker = self.repo/"marker"
        script = self.write("driver.sh", "#!/bin/sh\ntouch marker\ncat\n")
        script.chmod(0o755)
        self.write(".gitattributes", "apple.py diff=fixture filter=fixture\n")
        for key in ("diff.fixture.command", "diff.fixture.textconv", "filter.fixture.clean",
                    "filter.fixture.smudge", "core.fsmonitor"):
            git(self.repo, "config", key, str(script))
        self.write("apple.py", "updated\n")
        index = self.repo/".git/index"
        before = index.stat(), index.read_bytes()
        result = product.collect_diff(self.task, include_patch=True)
        self.assertIn("apple.py", result["summary"]["files_modified"])
        self.assertIn("+updated", result["patch"])
        self.assertFalse(marker.exists())
        self.assertEqual((index.stat().st_mtime_ns, index.read_bytes()), (before[0].st_mtime_ns, before[1]))

    def test_replaced_gitlink_needs_no_submodule_object(self):
        oid = "a" * 40
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},sub")
        git(self.repo, "commit", "-qm", "gitlink fixture")
        self.task["worktree"]["base_commit"] = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "rm", "--cached", "sub")
        self.write("sub", "replacement\n")
        result = product.collect_diff(self.task, include_patch=True)
        self.assertEqual(result["summary"]["files_modified"], ["sub"])
        self.assertIn("+replacement", result["patch"])

    def test_large_recreated_text_has_unknown_counts_and_text_type(self):
        self.write("large", "x" * (1024 * 1024 + 1))
        git(self.repo, "add", "large")
        git(self.repo, "commit", "-qm", "large base")
        self.task["worktree"]["base_commit"] = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "rm", "--cached", "large")
        self.write("large", "changed\n")
        row, = product.collect_diff(self.task)["files"]
        self.assertEqual((row["status"], row["binary"], row["lines_added"]), ("M", False, None))

    def test_untracked_races_preserve_product_error_boundary(self):
        self.assertEqual(product._untracked_bytes(self.repo, "missing/leaf", None), (None, b"", False))
        (self.repo / "alias").symlink_to(self.repo / "app")
        with self.assertRaises(product.TaskError) as caught:
            product._untracked_bytes(self.repo, "alias/change", None)
        self.assertEqual(caught.exception.code, "PRODUCT_CHANGED")

    def test_patch_wait_timeout_kills_child_and_missing_git_is_typed(self):
        with patch.object(product.shutil, "which", return_value=None), self.assertRaises(product.TaskError) as caught:
            product._git_argv(self.repo, "diff")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CAPABILITY")
        with patch.object(product.subprocess, "Popen") as spawn:
            child = spawn.return_value.__enter__.return_value
            child.stdout.read.return_value = b""
            child.wait.side_effect = subprocess.TimeoutExpired("git", 30)
            with self.assertRaises(subprocess.TimeoutExpired):
                product._git_patch(self.repo, {}, [])
            child.wait.assert_called_once_with(timeout=30)
            child.kill.assert_called_once_with()

    def test_file_list_and_patch_have_independent_bounds(self):
        for i in range(202):
            self.write(f"new-{i:03}", "added\n")
        self.write("large", "λ" * 180000)
        result = product.collect_diff(self.task, include_patch=True)
        self.assertEqual(result["summary"]["added_count"], 203)
        self.assertEqual(len(result["files"]), 200)
        self.assertEqual(len(result["summary"]["files_added"]), 200)
        self.assertTrue(result["summary"]["truncated"])
        self.assertTrue(result["patch_truncated"])
        self.assertLessEqual(len(result["patch"].encode()), 256*1024)
        self.write("app/change", "x" * 300000)
        result = product.collect_diff(self.task, include_patch=True)
        self.assertTrue(result["patch_truncated"])
        self.assertTrue(result["patch"].startswith("diff --git"))
