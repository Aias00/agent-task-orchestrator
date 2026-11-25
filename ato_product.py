"""Content-addressed product and independent check evidence for unified Tasks."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from tempfile import TemporaryDirectory

from ato_task_store import (TaskError, _checkout_environment, _directory, _file_stamp, _git, _git_text, _read_file,
                            _require, _sha, _strict_json, canonical_json)


_MAX_FILES = 20000
_MAX_FILE = 64 * 1024 * 1024
_MAX_TOTAL = 256 * 1024 * 1024
RETENTION_COMMITTER = dict(GIT_COMMITTER_NAME="ATO Host", GIT_COMMITTER_EMAIL="ato-host@local")


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
        tracked, gitlinks = set(), {}
        for row in _names(_git(root, "ls-files", "--stage", "-z")[0]):
            header, name = row.split("\t", 1)
            mode, oid, stage = header.split()
            _require(stage == "0" and mode in {"100644", "100755", "120000", "160000"}, "PRODUCT_CHANGED")
            if mode == "160000":
                gitlinks[name] = oid
            tracked.add(name)
        untracked = set(_names(_git(root, "ls-files", "--others", "--exclude-standard", "-z")[0]))
        names = sorted(name for name in tracked | untracked if not _ignored_runtime(name, task))
        _require(len(names) <= _MAX_FILES, "PRODUCT_CHANGED")
        entries = []
        remaining = _MAX_TOTAL
        for name in names:
            if name in gitlinks:
                entries.append(dict(path=name, kind="gitlink", oid=gitlinks[name]))
                continue
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


def retained_entries(manifest):
    return [entry for entry in manifest["entries"] if entry["kind"] != "missing"]


def index_bytes(task):
    admin = Path(task["worktree"]["git_admin"])
    with _directory(admin) as fd:
        return _read_file(fd, "index", limit=_MAX_FILE, private=False, stable_path=True,
                          code="PRODUCT_CHANGED")


def install_index(task, data):
    admin = Path(task["worktree"]["git_admin"])
    with _directory(admin) as fd:
        handle = os.open("index.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        installed = False
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace("index.lock", "index", src_dir_fd=fd, dst_dir_fd=fd)
            installed = True
            os.fsync(fd)
        finally:
            if not installed:
                os.unlink("index.lock", dir_fd=fd)


def build_retention(task, manifest, scratch, *, existing=None):
    """Build the Git tree from accepted file bytes using an isolated index."""
    root = Path(task["worktree"]["path"])
    env = dict(_checkout_environment(root), GIT_AUTHOR_NAME="ATO Host", GIT_AUTHOR_EMAIL="ato-host@local",
               **RETENTION_COMMITTER)
    head = manifest["head"]
    object_hash = "sha1" if len(head) == 40 else "sha256"
    head_blobs = {}
    for entry in _names(_git(root, "ls-tree", "-rz", "--full-tree", head, env=env)[0]):
        metadata, name = entry.split("\t", 1)
        _, kind, oid = metadata.split()
        if kind == "blob":
            head_blobs[name] = oid
    with TemporaryDirectory(prefix="cleanup-index-", dir=scratch) as folder:
        index = Path(folder)/"index"
        env["GIT_INDEX_FILE"] = str(index)
        _git(root, "read-tree", "--empty", env=env)
        entries = bytearray()
        for row in retained_entries(manifest):
            path = root/row["path"]
            if row["kind"] == "gitlink":
                mode, oid = "160000", row["oid"]
            else:
                if row["kind"] == "link":
                    target = os.readlink(path)
                    _require(target == row["target"], "PRODUCT_CHANGED")
                    mode, content = "120000", target.encode()
                else:
                    mode = "100755" if row["mode"] & stat.S_IXUSR else "100644"
                    with _directory(path.parent) as fd:
                        content = _read_file(fd, path.name, limit=_MAX_FILE, private=False,
                                             stable_path=True, code="PRODUCT_CHANGED")
                    _require(_sha(content) == row["sha256"], "PRODUCT_CHANGED")
                oid = head_blobs.get(row["path"])
                if oid:
                    blob = hashlib.new(object_hash, f"blob {len(content)}\0".encode(), usedforsecurity=False)
                    blob.update(content)
                    if blob.hexdigest() != oid:
                        oid = None
                if oid is None:
                    oid = _git(root, "hash-object", "-w", "--stdin", env=env, input_data=content)[0].decode().strip()
            entries.extend(f"{mode} {oid}\t{row['path']}\0".encode())
        _git(root, "update-index", "-z", "--index-info", env=env, input_data=entries)
        tree = _git(root, "write-tree", env=env)[0].decode().strip()
        if existing is not None:
            commit = existing
            _require(_git_text(root, "rev-parse", "--verify", commit+"^{tree}") == tree,
                     "CLEANUP_BLOCKED")
            if commit != head:
                _require(_git_text(root, "rev-parse", "--verify", commit+"^") == head, "CLEANUP_BLOCKED")
        elif tree == manifest["tree"]:
            commit = head
        else:
            commit = _git(root, "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", head,
                          "-m", "Retain accepted ATO product", env=env)[0].decode().strip()
            _require(_git_text(root, "rev-parse", "--verify", commit+"^{tree}") == tree, "CLEANUP_BLOCKED")
        return commit, tree, index.read_bytes()


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
    advisory = evidence.get("advisory_checks") is True
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
                 and receipt["state"] == summary["state"]
                 and (receipt["state"] == "passed" or advisory and receipt["state"] == "failed")
                 and 1 <= len(receipt["invocations"]) <= len(criterion["checks"])
                 and (receipt["state"] != "passed" or len(receipt["invocations"]) == len(criterion["checks"])),
                 "CHECKS_REQUIRED")
        for index, (argv, invocation) in enumerate(zip(criterion["checks"], receipt["invocations"])):
            state = receipt["state"] if index + 1 == len(receipt["invocations"]) else "passed"
            _require(invocation["argv_sha256"] == _sha(canonical_json(argv))
                     and invocation["state"] == state, "CHECKS_REQUIRED")
            store.read_evidence(task_id, invocation["log_sha256"])
    if spec["reviewer"] is not None and (not advisory or review is not None):
        _require(review is not None and review["verdict"] == "approve", "CHECKS_REQUIRED")
        verdict = _strict_json(store.read_evidence(task_id, review["receipt_id"]), "STATE_CORRUPT")
        _require(verdict["task_id"] == task_id and verdict["run_id"] == run["run_id"]
                 and verdict["spec_sha256"] == task["spec_sha256"]
                 and verdict["generation"] == generation and verdict["product_id"] == product["product_id"]
                 and verdict["checks_sha256"] == product["checks_sha256"]
                 and verdict["review_revision"] == review_revision
                 and verdict["verdict"]["verdict"] == "approve", "CHECKS_REQUIRED")
    return product, checks, review
