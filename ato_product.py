"""Content-addressed product and independent check evidence for unified Tasks."""
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from ato_task_store import (TaskError, _directory, _file_stamp, _git, _git_text, _read_file,
                            _require, _sha, _strict_json, canonical_json)


_MAX_FILES = 20000
_MAX_FILE = 64 * 1024 * 1024
_MAX_TOTAL = 256 * 1024 * 1024


def _names(data):
    for raw in data.split(b"\0"):
        if raw:
            yield raw.decode("utf-8")


def _ignored_runtime(name, task):
    return (name == ".codex/tasks" or name.startswith(".codex/tasks/")
            or name in task["worktree"]["shared_link_identities"])


def _entry(root, name, remaining):
    relative = PurePosixPath(name)
    _require(not relative.is_absolute() and relative.as_posix() == name
             and not set(relative.parts) & {"..", ".", ".git"}, "PRODUCT_CHANGED")
    parent = root
    for part in relative.parts[:-1]:
        parent = parent / part
        try:
            if not stat.S_ISDIR(parent.lstat().st_mode):
                return {"kind": "missing"}, 0
        except FileNotFoundError:
            return {"kind": "missing"}, 0
    path = root / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}, 0
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        target.encode("utf-8")
        _require(_file_stamp(info) == _file_stamp(path.lstat()), "PRODUCT_CHANGED")
        return {"kind": "link", "mode": mode, "target": target}, 0
    if stat.S_ISDIR(info.st_mode):
        # A tracked leaf replaced by a directory now has a deletion
        # and independent untracked paths beneath it.
        return {"kind": "missing"}, 0
    _require(stat.S_ISREG(info.st_mode) and info.st_size <= min(remaining, _MAX_FILE),
             "PRODUCT_CHANGED")
    with _directory(path.parent) as fd:
        data = _read_file(fd, path.name, limit=min(remaining, _MAX_FILE), private=False,
                          stable_path=True, code="PRODUCT_CHANGED")
        _require(_file_stamp(info) == _file_stamp(os.stat(path.name, dir_fd=fd, follow_symlinks=False)),
                 "PRODUCT_CHANGED")
    return {"kind": "file", "mode": mode, "sha256": _sha(data)}, len(data)


def measure(task):
    """Measure the verified worktree using the Git index and current file bytes."""
    worktree = task["worktree"]
    root, admin = Path(worktree["path"]), Path(worktree["git_admin"])
    try:
        with _directory(admin) as fd:
            index = _read_file(fd, "index", limit=_MAX_FILE, private=False, stable_path=True,
                               code="PRODUCT_CHANGED")
        head = _git_text(root, "rev-parse", "--verify", "HEAD")
        tree = _git_text(root, "rev-parse", "--verify", head + "^{tree}")
        tracked = set()
        for row in _names(_git(root, "ls-files", "--stage", "-z")[0]):
            header, name = row.split("\t", 1)
            mode, _, stage = header.split()
            _require(stage == "0" and mode in {"100644", "100755", "120000"}, "PRODUCT_CHANGED")
            tracked.add(name)
        untracked = set(_names(_git(root, "ls-files", "--others", "--exclude-standard", "-z")[0]))
        names = sorted(name for name in tracked | untracked if not _ignored_runtime(name, task))
        _require(len(names) <= _MAX_FILES, "PRODUCT_CHANGED")
        entries = []
        remaining = _MAX_TOTAL
        for name in names:
            entry, size = _entry(root, name, remaining)
            entries.append(dict(path=name, **entry))
            remaining -= size
        with _directory(admin) as fd:
            reread_index = _read_file(fd, "index", limit=_MAX_FILE, private=False, stable_path=True,
                                      code="PRODUCT_CHANGED")
        _require(reread_index == index and _git_text(root, "rev-parse", "--verify", "HEAD") == head,
                 "PRODUCT_CHANGED")
        return dict(schema_version=1, task_id=task["task_id"], spec_sha256=task["spec_sha256"],
                    base=worktree["base_commit"], head=head, tree=tree, index_sha256=_sha(index),
                    entries=entries)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, TaskError):
            raise
        raise TaskError("PRODUCT_CHANGED") from None


def retain_checks(store, task, run, generation, contract, criteria, manifest):
    """Capture the exact host check invocation of every command criterion."""
    task_id = task["task_id"]
    manifest_ref = store.put_evidence(task_id, canonical_json(manifest))
    product_id = "product-" + manifest_ref
    evidence_dir = Path(run["workflow_dir"]) / "evidence"
    checks = []
    with _directory(evidence_dir, private_from=evidence_dir) as fd:
        for criterion in contract["acceptance"]:
            if criterion["kind"] != "command":
                continue
            key = criterion["id"]
            row = criteria[key]
            invocations = []
            paths = row["evidence"]
            _require(len(paths) <= len(criterion["checks"])
                     and (row["status"] != "passed" or len(paths) == len(criterion["checks"])))
            for number, path in enumerate(paths):
                log = Path(path)
                _require(log.parent == evidence_dir and log.name.startswith(key + "-overall-"))
                invocations.append(dict(argv_sha256=_sha(canonical_json(criterion["checks"][number])),
                    log_sha256=store.put_evidence(task_id, _read_file(fd, log.name, limit=_MAX_FILE)),
                    state="passed" if number < len(paths) - 1 else row["status"]))
            receipt = dict(schema_version=1, task_id=task_id, run_id=run["run_id"],
                           spec_sha256=task["spec_sha256"], generation=generation,
                           workflow_id=run["workflow_id"], contract_hash=run["contract_hash"],
                           criterion_id=key, argv_sha256=_sha(canonical_json(criterion["checks"])),
                           product_id=product_id, state=row["status"], invocations=invocations)
            checks.append(dict(criterion_id=key, state=row["status"],
                               receipt_id=store.put_evidence(task_id, canonical_json(receipt))))
    checks_sha256 = store.put_evidence(task_id, canonical_json([row["receipt_id"] for row in checks]))
    return dict(product_id=product_id, base=manifest["base"], branch=task["worktree"]["branch"],
                worktree=task["worktree"]["path"], checks_sha256=checks_sha256), checks


def read_delivery(store, task, run, generation, spec, evidence, review_revision):
    """Verify content and correlation of the receipts referenced by settlement."""
    task_id = task["task_id"]
    product, checks, review = (evidence[k] for k in ("product", "checks", "review"))
    ref = product["product_id"].removeprefix("product-")
    manifest = _strict_json(store.read_evidence(task_id, ref), "STATE_CORRUPT")
    _require(product["product_id"] == "product-" + ref
             and manifest["task_id"] == task_id and manifest["spec_sha256"] == task["spec_sha256"]
             and manifest["base"] == task["worktree"]["base_commit"] == product["base"]
             and task["worktree"]["path"] == product["worktree"]
             and task["worktree"]["branch"] == product["branch"])
    if not task["worktree_removed"]:
        _require(manifest == measure(task), "PRODUCT_CHANGED")
    expected = [row for row in spec["worker_contract"]["acceptance"] if row["kind"] == "command"]
    _require(len(checks) == len(expected))
    _require(store.read_evidence(task_id, product["checks_sha256"])
             == canonical_json([row["receipt_id"] for row in checks]))
    for criterion, summary in zip(expected, checks):
        receipt = _strict_json(store.read_evidence(task_id, summary["receipt_id"]), "STATE_CORRUPT")
        _require(receipt["task_id"] == task_id and receipt["run_id"] == run["run_id"]
                 and receipt["spec_sha256"] == task["spec_sha256"]
                 and receipt["workflow_id"] == run["workflow_id"]
                 and receipt["contract_hash"] == run["contract_hash"]
                 and receipt["generation"] == generation and receipt["product_id"] == product["product_id"]
                 and receipt["criterion_id"] == summary["criterion_id"] == criterion["id"]
                 and receipt["argv_sha256"] == _sha(canonical_json(criterion["checks"]))
                 and receipt["state"] == summary["state"] == "passed"
                 and len(receipt["invocations"]) == len(criterion["checks"]), "CHECKS_REQUIRED")
        for argv, invocation in zip(criterion["checks"], receipt["invocations"]):
            _require(invocation["argv_sha256"] == _sha(canonical_json(argv))
                     and invocation["state"] == "passed", "CHECKS_REQUIRED")
            store.read_evidence(task_id, invocation["log_sha256"])
    if spec["reviewer"] is not None:
        _require(review is not None and review["verdict"] == "approve", "CHECKS_REQUIRED")
        verdict = _strict_json(store.read_evidence(task_id, review["receipt_id"]), "STATE_CORRUPT")
        _require(verdict["task_id"] == task_id and verdict["run_id"] == run["run_id"]
                 and verdict["spec_sha256"] == task["spec_sha256"]
                 and verdict["generation"] == generation and verdict["product_id"] == product["product_id"]
                 and verdict["checks_sha256"] == product["checks_sha256"]
                 and verdict["review_revision"] == review_revision
                 and verdict["verdict"]["verdict"] == "approve", "CHECKS_REQUIRED")
    return product, checks, review
