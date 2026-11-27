"""Diff inspection, opt-in guardrails, product fingerprinting, and independent host checks."""
from __future__ import annotations

import difflib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

from ato.adapters.base import run_bounded_subprocess
from ato.spec import TaskError, canonical_json, require, sha256_bytes
from ato.worktree import checkout_env, git_argv, git_run, shared_links_for, tree_fingerprint

MAX_DIFF_LIST = 200
MAX_PATCH_BYTES = 256 * 1024
MAX_UNTRACKED_BYTES = 1024 * 1024
MAX_CHECK_LOG = 8192
_DIFF_FLAGS = ("--no-renames", "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=dirty")
_CHANGE = {"A": "added", "D": "deleted", "M": "modified"}
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}
_TEST_FILE = re.compile(r"(?:_test|\.test|\.spec|Tests?)\.[^.]+$")


def is_test_path(name: str) -> bool:
    p = PurePosixPath(name)
    return bool(set(p.parts[:-1]) & _TEST_DIRS) or p.name.startswith("test_") or bool(_TEST_FILE.search(p.name))


def _ignored(name: str, task: dict) -> bool:
    return name == ".codex/tasks" or name.startswith(".codex/tasks/") or name in shared_links_for(task)


def _names(data: bytes) -> list[str]:
    return [raw.decode("utf-8") for raw in data.split(b"\0") if raw]


def _base_entries(root: Path, base: str, env: dict[str, str]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for row in _names(git_run(root, "ls-tree", "-rz", "--full-tree", base, env=env)[0]):
        meta, name = row.split("\t", 1)
        mode, _, oid = meta.split()
        out[name] = (mode, oid)
    return out


def _diff_row(name: str, status: str, added: int | None, deleted: int | None, *, binary: bool | None = None) -> dict:
    return {
        "path": name,
        "status": status,
        "lines_added": added,
        "lines_deleted": deleted,
        "binary": (added is None) if binary is None else binary,
        "is_test": is_test_path(name),
    }


def _git_patch(root: Path, env: dict[str, str], args: list[str]) -> bytearray:
    argv = git_argv(root, "diff", *_DIFF_FLAGS, *args)
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env) as proc:
        assert proc.stdout is not None
        data = proc.stdout.read(MAX_PATCH_BYTES + 1)
        if len(data) > MAX_PATCH_BYTES:
            proc.kill()
            proc.wait()
        else:
            try:
                require(proc.wait(timeout=30) == 0, "BINDING_CHANGED")
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
    return bytearray(data)


def _add_untracked(root: Path, env: dict[str, str], tree: dict[str, tuple[str, str]],
                   name: str, rows: dict[str, dict], patch: bytearray | None, truncated: bool) -> bool:
    p = root / name
    try:
        st = p.lstat()
    except FileNotFoundError:
        return truncated
    if stat.S_ISLNK(st.st_mode):
        mode = "120000"
        data = os.fsencode(os.readlink(p))
        partial = False
    elif stat.S_ISREG(st.st_mode):
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        with p.open("rb") as fh:
            raw = fh.read(MAX_UNTRACKED_BYTES + 1)
        partial = len(raw) > MAX_UNTRACKED_BYTES
        data = raw[:MAX_UNTRACKED_BYTES]
    else:
        return truncated

    old = b""
    before = "/dev/null"
    if name in tree:
        oldmode, oid = tree[name]
        if oldmode == "160000":
            old = f"Subproject commit {oid}\n".encode("ascii")
        else:
            size = int(git_run(root, "cat-file", "-s", oid, env=env)[0])
            if size > MAX_UNTRACKED_BYTES:
                binary = b"\0" in data or mode == "120000"
                rows[name] = _diff_row(name, "M", None, None, binary=binary)
                return truncated or (patch is not None)
            old = git_run(root, "cat-file", "-p", oid, env=env)[0]
        if not partial and mode == oldmode and data == old:
            return truncated
        before = "a/" + name

    binary = b"\0" in old or b"\0" in data or mode == "120000"
    status = "M" if name in tree else "A"
    added = deleted = None
    hunks: list[bytes] = []
    if not binary:
        left, right = old.splitlines(True), data.splitlines(True)
        if not left:
            added, deleted = len(right), 0
        else:
            matches = difflib.SequenceMatcher(None, left, right).get_matching_blocks()
            equal = sum(m.size for m in matches)
            added, deleted = len(right) - equal, len(left) - equal
        if patch is not None and not truncated:
            hunks = list(difflib.diff_bytes(difflib.unified_diff, left, right, before.encode(), ("b/" + name).encode()))
    rows[name] = _diff_row(name, status, None if partial else added, None if partial else deleted, binary=binary)

    if patch is not None and not truncated:
        if mode == "120000":
            target = os.fsdecode(data)
            resolved = os.path.normpath(os.path.join(os.path.dirname(name), target))
            shown = b"[external symlink]" if (os.path.isabs(resolved) or resolved == ".." or resolved.startswith("../")) else data
            hunks = [f"--- {before}\n+++ b/{name}\n@@ -0,0 +1 @@\n".encode(), b"+" + shown + b"\n"]
        elif binary:
            hunks = [f"Binary files {before} and b/{name} differ\n".encode()]
        elif not old and not data:
            hunks = [f"diff --git a/{name} b/{name}\nnew file mode {mode}\n".encode()]
        for line in hunks:
            if not line.endswith(b"\n"):
                line += b"\n\\ No newline at end of file\n"
            patch.extend(line[:MAX_PATCH_BYTES + 1 - len(patch)])
            if len(patch) > MAX_PATCH_BYTES:
                truncated = True
                break
        truncated = truncated or (partial and not binary)
    return truncated


def _summary(base: str, files: list[dict]) -> dict:
    out: dict = {"base": base, "truncated": len(files) > MAX_DIFF_LIST}
    for code, label in _CHANGE.items():
        names = [row["path"] for row in files if row["status"] == code]
        out[label + "_count"] = len(names)
        out["files_" + label] = names[:MAX_DIFF_LIST]
        if code != "A":
            out["tests_" + label] = [n for n in names if is_test_path(n)][:MAX_DIFF_LIST]
    out["lines_added"] = sum(row["lines_added"] or 0 for row in files)
    out["lines_deleted"] = sum(row["lines_deleted"] or 0 for row in files)
    return out


def _collect_diff_data(task: dict, *, include_patch: bool = False) -> dict:
    base = task["base_commit"]
    removed = task["worktree_state"] == "removed"
    root = Path(task["repo_root"] if removed else task["worktree_path"]).resolve()
    env = checkout_env(root)
    revisions = [base]
    if removed:
        ref = "refs/heads/" + task["branch"]
        out, code = git_run(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}", allowed=(0, 1), env=env)
        require(code == 0, "BINDING_CHANGED")
        revisions.append(out.decode("ascii").strip())
    exclusions = [".codex/tasks", *sorted(shared_links_for(task))]
    untracked = (
        [] if removed else sorted(
            name for name in _names(git_run(root, "ls-files", "--others", "--exclude-standard", "-z", env=env)[0])
            if not _ignored(name, task)
        )
    )
    tree = _base_entries(root, base, env) if untracked else {}
    exclusions.extend(name for name in untracked if name in tree)
    args = [*revisions, "--", ".", *[":(exclude,literal)" + name for name in exclusions]]
    raw = git_run(root, "diff", *_DIFF_FLAGS, "--raw", "-z", *args, env=env)[0].split(b"\0")
    states = {name.decode("utf-8"): header.split()[-1].decode("utf-8") for header, name in zip(raw[::2], raw[1::2]) if name}
    rows: dict[str, dict] = {}
    for record in _names(git_run(root, "diff", *_DIFF_FLAGS, "--numstat", "-z", *args, env=env)[0]):
        added, deleted, name = record.split("\t", 2)
        state = states.get(name, "M")
        rows[name] = _diff_row(
            name, state if state in _CHANGE else "M",
            None if added == "-" else int(added), None if deleted == "-" else int(deleted),
        )
    patch = _git_patch(root, env, args) if include_patch else None
    truncated = patch is not None and len(patch) > MAX_PATCH_BYTES
    for name in untracked:
        truncated = _add_untracked(root, env, tree, name, rows, patch, truncated)
    files = [rows[name] for name in sorted(rows)]
    rendered = patch[:MAX_PATCH_BYTES].decode("utf-8", "replace").encode("utf-8") if include_patch and patch is not None else b""
    return {
        "summary": _summary(base, files),
        "files": files,
        "patch": rendered[:MAX_PATCH_BYTES].decode("utf-8", "ignore") if include_patch else None,
        "patch_truncated": truncated or len(rendered) > MAX_PATCH_BYTES,
    }


def _collect_diff_rows(task: dict, *, include_patch: bool = False) -> list[dict]:
    """Full changed-file set, independent of public display limits."""
    return _collect_diff_data(task, include_patch=include_patch)["files"]


def collect_diff(task: dict, *, include_patch: bool = False) -> dict:
    data = _collect_diff_data(task, include_patch=include_patch)
    data["files"] = data["files"][:MAX_DIFF_LIST]
    return data


def _matching_rules(path: str, rules: dict[str, list[str]]) -> list[str]:
    return [rule for rule, prefixes in rules.items()
            if any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)]


def check_guardrails(task: dict, spec: dict, *, diff: dict | None = None) -> list[dict]:
    rules = spec.get("guardrails")
    if not rules or not any(rules.values()):
        return []
    rows = (_collect_diff_rows(task) if diff is None or diff.get("summary", {}).get("truncated")
            else diff["files"])
    violations: list[dict] = []
    for row in rows:
        path, change = row["path"], row["status"]
        for rule in sorted(_matching_rules(path, rules)):
            if rule == "protected_paths" or change == "D":
                violations.append({"rule": rule, "path": path, "change": _CHANGE.get(change, change)})
                if len(violations) >= MAX_DIFF_LIST:
                    return violations
    return violations


def build_product(task: dict, checks: list[dict], *, diff_data: dict | None = None) -> dict:
    fp = tree_fingerprint(task["worktree_path"], ignored=shared_links_for(task))
    checks_sha = sha256_bytes(canonical_json([c["receipt_id"] for c in checks]))
    product: dict = {
        "product_id": "product-" + fp,
        "base": task["base_commit"],
        "branch": task["branch"],
        "worktree": task["worktree_path"],
        "checks_sha256": checks_sha,
    }
    try:
        d = collect_diff(task) if diff_data is None else diff_data
        product["diff_summary"] = d["summary"]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return product


def run_checks(task: dict, spec: dict, *, timeout_per_cmd: int = 300,
               stop_check=None, on_child: object = None) -> tuple[list[dict], bool]:
    """Run all frozen host command acceptance checks independently of the worker."""
    wt = Path(task["worktree_path"]).resolve()
    env = {k: v for k, v in os.environ.items()
           if k not in ("CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE")}
    results: list[dict] = []
    all_passed = True
    for criterion in spec["worker_contract"]["acceptance"]:
        if criterion["kind"] != "command":
            continue
        cid = criterion["id"]
        passed = True
        excerpts: list[str] = []
        invocations: list[dict] = []
        diagnostic = None
        for argv in criterion["checks"]:
            if stop_check is not None and stop_check():
                raise TaskError("STOPPED", task["id"])
            try:
                rc, stdout, stderr, code = run_bounded_subprocess(
                    argv, cwd=wt, env=env, timeout=timeout_per_cmd,
                    stop_check=stop_check, on_child=on_child,
                )
            except OSError:
                rc, stdout, stderr, code = -1, b"[check invocation failed]", b"", "PROCESS_ERROR"
            if code == "STOPPED":
                raise TaskError("STOPPED", task["id"])
            text = (stdout + stderr).decode("utf-8", "replace")[-MAX_CHECK_LOG:]
            if code == "TIMEOUT":
                text += "\n[check timed out]"
            cmd_ok = rc == 0 and code == "OK"
            invocations.append({"argv": argv, "returncode": rc, "passed": cmd_ok})
            excerpts.append(text)
            if not cmd_ok:
                passed = False
                diagnostic = {"command_index": len(invocations), "code": code, "returncode": rc}
                break
        state = "passed" if passed else "failed"
        if not passed:
            all_passed = False
        receipt_id = sha256_bytes(canonical_json({
            "task_id": task["id"], "criterion_id": cid, "state": state, "invocations": invocations,
        }))
        results.append({
            "criterion_id": cid,
            "state": state,
            "receipt_id": receipt_id,
            "diagnostic": diagnostic,
            "output_excerpt": "\n".join(excerpts)[-MAX_CHECK_LOG:],
        })
    return results, all_passed
