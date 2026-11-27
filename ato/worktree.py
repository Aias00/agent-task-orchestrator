"""Isolated Git worktree lifecycle, branch retention, and mutation tripwire."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory

from ato import config
from ato.spec import SHARED_ENV_LINKS, TaskError, canonical_json, require, sha256_bytes


GIT_ENV = {
    "PATH": os.defpath,
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_ALLOW_PROTOCOL": "",
}
RETENTION_IDENTITY = {
    "GIT_AUTHOR_NAME": "ATO Host",
    "GIT_AUTHOR_EMAIL": "ato-host@local",
    "GIT_COMMITTER_NAME": "ATO Host",
    "GIT_COMMITTER_EMAIL": "ato-host@local",
}


def git_argv(repo: str | Path, *args: str) -> list[str]:
    git = shutil.which("git", path=os.defpath)
    require(git is not None, "UNSUPPORTED_CAPABILITY")
    return [
        git, "-C", str(repo),
        "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false",
        "-c", "submodule.recurse=false",
        "-c", "protocol.allow=never",
        *args,
    ]


def git_run(repo: str | Path, *args: str, allowed: tuple[int, ...] = (0,),
            env: dict[str, str] | None = None, input_data: bytes | None = None,
            timeout: int = 30) -> tuple[bytes, int]:
    result = subprocess.run(
        git_argv(repo, *args),
        env=GIT_ENV if env is None else env,
        input=input_data,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode not in allowed:
        raise ValueError(f"git {' '.join(args[:3])} failed ({result.returncode})")
    return result.stdout, result.returncode


def git_text(repo: str | Path, *args: str, env: dict[str, str] | None = None) -> str:
    return git_run(repo, *args, env=env)[0].decode("utf-8").removesuffix("\n")


def checkout_env(repo: str | Path) -> dict[str, str]:
    """Disable repo-configured clean/smudge filters for trusted local Git reads/writes."""
    out = git_run(repo, "config", "--includes", "--null", "--name-only",
                  "--get-regexp", r"^filter\.", allowed=(0, 1))[0].decode("utf-8")
    filters = {key.rsplit(".", 1)[0] for key in out.split("\x00") if key}
    overrides = [
        (f"{name}.{field}", val)
        for name in sorted(filters)
        for field, val in (("process", ""), ("smudge", ""), ("clean", ""), ("required", "false"))
    ]
    env = dict(GIT_ENV, GIT_CONFIG_COUNT=str(len(overrides)))
    for idx, (k, v) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{idx}"] = k
        env[f"GIT_CONFIG_VALUE_{idx}"] = v
    return env


def create_worktree(repo_root: str | Path, task_id: str, base_commit: str,
                    shared_env_links: list[str] | None = None) -> tuple[str, str]:
    """Create an isolated worktree + branch ato/<task_id> at base_commit."""
    repo = Path(repo_root).resolve()
    branch = f"ato/{task_id}"
    root_dir = config.ensure_private_dir(config.worktrees_dir())
    wt_path = root_dir / task_id
    require(not wt_path.exists(), "BUSY")
    env = checkout_env(repo)
    try:
        git_run(repo, "worktree", "add", "-b", branch, str(wt_path), base_commit, env=env)
        for rel in shared_env_links or []:
            if rel not in SHARED_ENV_LINKS:
                continue
            src = repo / rel
            dst = wt_path / rel
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(src.resolve(), dst)
    except Exception:
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
        git_run(repo, "worktree", "prune", allowed=(0, 1))
        git_run(repo, "branch", "-D", branch, allowed=(0, 1))
        raise TaskError("LAUNCH_FAILED") from None
    return str(wt_path.resolve()), branch


def verify_worktree(task: dict) -> Path:
    require(task["worktree_state"] == "present" and task["worktree_path"], "BINDING_CHANGED")
    wt = Path(task["worktree_path"])
    require(wt.is_dir(), "BINDING_CHANGED")
    try:
        top = Path(git_text(wt, "rev-parse", "--path-format=absolute", "--show-toplevel")).resolve(strict=True)
        common = Path(git_text(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
        require(top == wt.resolve(strict=True) and str(common) == task["repo_key"], "BINDING_CHANGED")
    except TaskError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TaskError("BINDING_CHANGED") from None
    return wt


def shared_links_for(task: dict) -> set[str]:
    return set((task.get("spec") or {}).get("worktree", {}).get("shared_env_links") or [])


def tree_fingerprint(worktree: str | Path, ignored: set[str] | None = None) -> str:
    """Content-sensitive fingerprint of HEAD + index + working tree + untracked files (for review tripwire)."""
    wt = Path(worktree).resolve()
    env = checkout_env(wt)
    ignore_set = ignored or set()
    head = git_text(wt, "rev-parse", "--verify", "HEAD", env=env)
    tracked = [p for p in git_run(wt, "ls-files", "-z", env=env)[0].decode("utf-8").split("\x00") if p]
    untracked = [
        p for p in git_run(wt, "ls-files", "--others", "--exclude-standard", "-z", env=env)[0].decode("utf-8").split("\x00")
        if p
    ]
    rows = []
    for name in sorted(set(tracked) | set(untracked)):
        if name == ".codex/tasks" or name.startswith(".codex/tasks/") or name in ignore_set:
            continue
        p = wt / name
        if not os.path.lexists(p):
            rows.append([name, "missing"])
            continue
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode):
            rows.append([name, "link", os.readlink(p)])
        elif stat.S_ISREG(st.st_mode):
            rows.append([name, "file", stat.S_IMODE(st.st_mode), sha256_bytes(p.read_bytes())])
        else:
            rows.append([name, "other", stat.S_IFMT(st.st_mode)])
    return sha256_bytes(canonical_json({"head": head, "entries": rows}))


def is_clean(worktree: str | Path, ignored: set[str] | None = None) -> bool:
    wt = Path(worktree).resolve()
    env = checkout_env(wt)
    out = git_run(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", env=env)[0].decode("utf-8")
    ignore_set = ignored or set()
    for token in out.split("\x00"):
        if not token:
            continue
        rel = token[3:]
        if rel == ".codex/tasks" or rel.startswith(".codex/tasks/") or rel in ignore_set:
            continue
        return False
    return True


def commit_worktree_product(task: dict) -> str:
    """Commit all non-ignored worktree changes onto the task branch cleanly before removal."""
    wt = verify_worktree(task)
    env = dict(checkout_env(wt), **RETENTION_IDENTITY)
    head = git_text(wt, "rev-parse", "--verify", "HEAD", env=env)
    branch_ref = f"refs/heads/{task['branch']}"
    branch_oid = git_text(wt, "rev-parse", "--verify", branch_ref, env=env)
    ignored = shared_links_for(task)
    entries = {}
    for row in git_run(wt, "ls-files", "--stage", "-z", env=env)[0].split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, oid, stage = meta.decode("ascii").split()
        require(stage == "0", "CLEANUP_BLOCKED", detail="Resolve index conflicts before retaining product.")
        entries[os.fsdecode(name)] = (mode, oid)
    tracked = list(entries)
    untracked = [
        p for p in git_run(wt, "ls-files", "--others", "--exclude-standard", "-z", env=env)[0].decode("utf-8").split("\x00")
        if p
    ]
    names = sorted(
        n for n in set(tracked) | set(untracked)
        if n != ".codex/tasks" and not n.startswith(".codex/tasks/") and n not in ignored
    )
    with TemporaryDirectory(prefix="ato-retain-") as tmp:
        index = Path(tmp) / "index"
        env["GIT_INDEX_FILE"] = str(index)
        git_run(wt, "read-tree", "--empty", env=env)
        lines = bytearray()
        for name in names:
            p = wt / name
            if entries.get(name, (None,))[0] == "160000":
                mode, oid = entries[name]
                lines.extend(f"{mode} {oid}\t".encode("ascii") + os.fsencode(name) + b"\0")
                continue
            if not os.path.lexists(p):
                continue
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                mode = "120000"
                content = os.fsencode(os.readlink(p))
            elif stat.S_ISREG(st.st_mode):
                mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
                content = p.read_bytes()
            else:
                continue
            oid = git_run(wt, "hash-object", "-w", "--stdin", env=env, input_data=content)[0].decode("ascii").strip()
            lines.extend(f"{mode} {oid}\t{name}\0".encode("utf-8"))
        if lines:
            git_run(wt, "update-index", "-z", "--index-info", env=env, input_data=bytes(lines))
        tree = git_run(wt, "write-tree", env=env)[0].decode("ascii").strip()
        head_tree = git_text(wt, "rev-parse", "--verify", head + "^{tree}", env=env)
        if tree == head_tree:
            if head != branch_oid:
                git_run(wt, "update-ref", branch_ref, head, branch_oid, env=env)
            return head
        commit = git_run(
            wt, "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", head,
            "-m", f"Retain ATO product ({task['id']})", env=env,
        )[0].decode("ascii").strip()
        git_run(wt, "update-ref", branch_ref, commit, branch_oid, env=env)
        return commit


def remove_worktree(task: dict) -> None:
    repo = Path(task["repo_root"]).resolve()
    wt = Path(task["worktree_path"]).resolve()
    # Unlink shared env symlinks first so git worktree remove doesn't complain.
    for rel in shared_links_for(task):
        link = wt / rel
        if link.is_symlink():
            link.unlink()
    env = checkout_env(repo)
    _, code = git_run(repo, "worktree", "remove", "--force", str(wt), allowed=(0, 1, 128), env=env)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    git_run(repo, "worktree", "prune", allowed=(0, 1), env=env)
    require(not wt.exists(), "CLEANUP_BLOCKED")
