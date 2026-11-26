"""Content-addressed product and independent check evidence for unified Tasks."""
import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
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


_MAX_DIFF_LIST = 200
_MAX_PATCH_BYTES = 256 * 1024
_DIFF_FLAGS = ("--no-renames", "--no-ext-diff", "--no-textconv", "--no-color",
               "--ignore-submodules=dirty")
_CHANGE = {"A": "added", "D": "deleted", "M": "modified"}


def _is_test_path(name):
    path = PurePosixPath(name)
    return (bool(set(path.parts[:-1]) & {"test", "tests", "__tests__", "spec", "specs"})
            or path.name.startswith("test_")
            or bool(re.search(r"(?:_test|\.test|\.spec|Tests?)\.[^.]+$", path.name)))


def _base_entries(root, base, env):
    entries = {}
    for record in _names(_git(root, "ls-tree", "-rz", "--full-tree", base, env=env)[0]):
        meta, name = record.split("\t", 1)
        mode, _, oid = meta.split()
        entries[name] = (mode, oid)
    return entries


def _diff_row(name, status, added, deleted):
    return dict(path=name, status=status, lines_added=added, lines_deleted=deleted,
                binary=added is None, is_test=_is_test_path(name))


def _untracked_bytes(root, name, base_entry):
    path = root / name
    limit = 1024 * 1024
    try:
        with _directory(path.parent, code="PRODUCT_CHANGED") as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                return "120000", os.fsencode(os.readlink(path.name, dir_fd=parent)), False
            if not stat.S_ISREG(info.st_mode):
                return None, b"", False
            fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(fd)
                _require(stat.S_ISREG(opened.st_mode) and _file_stamp(opened) == _file_stamp(info), "PRODUCT_CHANGED")
                data = stream.read(limit + 1)
                # Recreated index deletions can be large. Compare the object ID
                # incrementally without retaining either entire version in memory.
                if base_entry and len(data) > limit:
                    mode, oid = base_entry
                    disk_mode = "100755" if opened.st_mode & stat.S_IXUSR else "100644"
                    if mode == disk_mode:
                        algorithm = "sha1" if len(oid) == 40 else "sha256"
                        digest = hashlib.new(algorithm, f"blob {opened.st_size}\0".encode(), usedforsecurity=False)
                        digest.update(data)
                        for chunk in iter(lambda: stream.read(limit), b""):
                            digest.update(chunk)
                        if digest.hexdigest() == oid:
                            return None, b"", False
    except FileNotFoundError:
        return None, b"", False
    mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
    return mode, data[:limit], len(data) > limit


def _git_argv(root, *args):
    git = shutil.which("git", path=os.defpath)
    _require(git is not None, "UNSUPPORTED_CAPABILITY")
    return [git, "-C", str(root), "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
            "-c", "protocol.allow=never", *args]


def _git_patch(root, env, args):
    argv = _git_argv(root, "diff", *_DIFF_FLAGS, *args)
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env) as process:
        data = process.stdout.read(_MAX_PATCH_BYTES + 1)
        if len(data) > _MAX_PATCH_BYTES:
            process.kill()
        else:
            try:
                _require(process.wait(timeout=30) == 0, "BINDING_CHANGED")
            except subprocess.TimeoutExpired:
                process.kill()
                raise
    return bytearray(data)


def _add_untracked(root, env, tree, name, rows, patch, truncated):
    mode, data, partial = _untracked_bytes(root, name, tree.get(name))
    if mode is None:
        return truncated
    old = b""
    before = "/dev/null"
    if name in tree:
        oldmode, oid = tree[name]
        if oldmode == "160000":
            old = f"Subproject commit {oid}\n".encode("ascii")
        else:
            size = int(_git(root, "cat-file", "-s", oid, env=env)[0])
            if size > 1024 * 1024:
                # No complete hunk can be generated without reading the base.
                rows[name] = _diff_row(name, "M", None, None)
                rows[name]["binary"] = b"\0" in data or mode == "120000"
                truncated |= patch is not None
                return truncated
            old = _git(root, "cat-file", "-p", oid, env=env)[0]
        if not partial and mode == oldmode and data == old:
            return truncated
        before = "a/" + name
    binary = b"\0" in old or b"\0" in data or mode == "120000"
    status = "M" if name in tree else "A"
    added = deleted = None
    hunks = ()
    if not binary:
        left, right = old.splitlines(True), data.splitlines(True)
        if not left:
            added, deleted = len(right), 0
        else:
            matches = difflib.SequenceMatcher(None, left, right).get_matching_blocks()
            equal = sum(match.size for match in matches)
            added, deleted = len(right) - equal, len(left) - equal
        if patch is not None and not truncated:
            hunks = difflib.diff_bytes(difflib.unified_diff, left, right, before.encode(), ("b/" + name).encode())
    rows[name] = _diff_row(name, status, None if partial else added, None if partial else deleted)
    rows[name]["binary"] = binary
    if patch is not None and not truncated:
        if mode == "120000":
            target = os.fsdecode(data)
            resolved = os.path.normpath(os.path.join(os.path.dirname(name), target))
            if os.path.isabs(resolved) or resolved == ".." or resolved.startswith("../"):
                data = b"[external symlink]"
            hunks = [f"--- {before}\n+++ b/{name}\n@@ -0,0 +1 @@\n".encode(), b"+" + data + b"\n"]
        elif binary:
            hunks = [f"Binary files {before} and b/{name} differ\n".encode()]
        elif not old and not data:
            header = f"new file mode {mode}" if name not in tree else f"old mode {oldmode}\nnew mode {mode}"
            hunks = [f"diff --git a/{name} b/{name}\n{header}\n".encode()]
        for line in hunks:
            if not line.endswith(b"\n"):
                line += b"\n\\ No newline at end of file\n"
            patch.extend(line[:_MAX_PATCH_BYTES + 1 - len(patch)])
            if len(patch) > _MAX_PATCH_BYTES:
                truncated = True
                break
        truncated |= partial and not binary
    return truncated


def _summary(base, files):
    summary = dict(base=base, truncated=len(files) > _MAX_DIFF_LIST)
    for code, change in _CHANGE.items():
        names = [row["path"] for row in files if row["status"] == code]
        summary[change + "_count"] = len(names)
        summary["files_" + change] = names[:_MAX_DIFF_LIST]
        if code != "A":
            summary["tests_" + change] = [name for name in names if _is_test_path(name)][:_MAX_DIFF_LIST]
    for field in ("lines_added", "lines_deleted"):
        summary[field] = sum(row[field] or 0 for row in files)
    return summary


def collect_diff(task, *, manifest=None, include_patch=False, frozen_changes=None):
    worktree = task["worktree"]
    base = manifest["base"] if manifest else worktree["base_commit"]
    removed = task["worktree_removed"]
    root = Path(task["repo"]["root"] if removed else worktree["path"])
    env = _checkout_environment(root)
    revisions = [base]
    if removed:
        ref = "refs/heads/" + worktree["branch"]
        target, code = _git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}", allowed=(0, 1), env=env)
        _require(code == 0, "BINDING_CHANGED")
        revisions.append(target.decode().strip())
    exclusions = [".codex/tasks", *worktree["shared_link_identities"]]
    if removed:
        untracked = []
    elif manifest is not None:
        indexed = set(_names(_git(root, "ls-files", "--cached", "-z", env=env)[0]))
        untracked = sorted(row["path"] for row in retained_entries(manifest) if row["path"] not in indexed)
    else:
        untracked = sorted(name for name in _names(
            _git(root, "ls-files", "--others", "--exclude-standard", "-z", env=env)[0])
            if not _ignored_runtime(name, task))
    tree = _base_entries(root, base, env) if manifest is not None or untracked else {}
    frozen = frozen_changes
    if manifest is not None and frozen is None:
        frozen = _manifest_changes(task, manifest, root, env, tree)
    # A removed index entry may still exist on disk. Compare those bytes once,
    # outside the index-based diff, including in the patch.
    exclusions.extend(name for name in untracked if name in tree)
    args = [*revisions, "--", ".", *[":(exclude,literal)" + name for name in exclusions]]
    raw = _git(root, "diff", *_DIFF_FLAGS, "--raw", "-z", *args, env=env)[0].split(b"\0")
    states = {name.decode(): header.split()[-1].decode() for header, name in zip(raw[::2], raw[1::2])}
    rows = {}
    for record in _names(_git(root, "diff", *_DIFF_FLAGS, "--numstat", "-z", *args, env=env)[0]):
        added, deleted, name = record.split("\t", 2)
        state = states.get(name, "M")
        rows[name] = _diff_row(name, state if state in _CHANGE else "M",
                               None if added == "-" else int(added), None if deleted == "-" else int(deleted))
    patch = _git_patch(root, env, args) if include_patch else None
    truncated = patch is not None and len(patch) > _MAX_PATCH_BYTES
    for name in untracked:
        truncated = _add_untracked(root, env, tree, name, rows, patch, truncated)
    if frozen is not None:
        # The manifest is authoritative even for assume-unchanged entries or
        # repository attributes that normalize bytes for the Git diff driver.
        rows = {name: dict(rows[name], status=state) if name in rows else _diff_row(name, state, None, None)
                for name, state in frozen.items()}
    files = [rows[name] for name in sorted(rows)]
    # A replacement character can occupy more UTF-8 bytes than invalid input.
    rendered = patch[:_MAX_PATCH_BYTES].decode("utf-8", "replace").encode() if include_patch else b""
    return dict(summary=_summary(base, files), files=files[:_MAX_DIFF_LIST],
                patch=rendered[:_MAX_PATCH_BYTES].decode("utf-8", "ignore") if include_patch else None,
                patch_truncated=truncated or len(rendered) > _MAX_PATCH_BYTES)


def summarize_diff(task, manifest, *, changes=None):
    return collect_diff(task, manifest=manifest, frozen_changes=changes)["summary"]


def _matching_paths(path, rules):
    return [rule for rule, paths in rules.items()
            if any(path == prefix or path.startswith(prefix + "/") for prefix in paths)]


def _base_blob_digest(reader, oid):
    reader.stdin.write(oid.encode("ascii") + b"\n")
    reader.stdin.flush()
    header = reader.stdout.readline().split()
    _require(len(header) == 3 and header[:2] == [oid.encode("ascii"), b"blob"], "PRODUCT_CHANGED")
    remaining = int(header[2])
    digest = hashlib.sha256()
    while remaining:
        chunk = reader.stdout.read(min(remaining, 1024 * 1024))
        _require(chunk, "PRODUCT_CHANGED")
        digest.update(chunk)
        remaining -= len(chunk)
    _require(reader.stdout.read(1) == b"\n", "PRODUCT_CHANGED")
    return digest.hexdigest()


def _manifest_changes(task, manifest, root, env, tree, rules=None):
    """Compare frozen modes/bytes with base objects, independently of Git index flags."""
    target = {row["path"]: row for row in retained_entries(manifest)}
    changes = {}
    argv = _git_argv(root, "cat-file", "--batch")
    with subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as reader:
        try:
            for path in sorted(tree.keys() | target.keys()):
                if _ignored_runtime(path, task):
                    continue
                if rules is not None:
                    matching = _matching_paths(path, rules)
                    if not matching or (path in target and "protected_paths" not in matching):
                        continue
                if path not in target:
                    changes[path] = "D"
                    continue
                if path not in tree:
                    changes[path] = "A"
                    continue
                mode, oid = tree[path]
                row = target[path]
                if row["kind"] == "gitlink":
                    same = mode == "160000" and row["oid"] == oid
                elif row["kind"] == "link":
                    same = mode == "120000" and _sha(row["target"].encode()) == _base_blob_digest(reader, oid)
                else:
                    wanted = "100755" if row["mode"] & stat.S_IXUSR else "100644"
                    same = mode == wanted and row["sha256"] == _base_blob_digest(reader, oid)
                if not same:
                    changes[path] = "M"
        finally:
            # An interrupted batch response may still be writing to the pipe.
            reader.kill()
    return changes


def frozen_changes(task, manifest, *, rules=None):
    root = Path(task["repo"]["root"])
    env = _checkout_environment(root)
    return _manifest_changes(task, manifest, root, env, _base_entries(root, manifest["base"], env), rules)


def check_guardrails(task, spec, manifest, *, diff=None):
    rules = spec.get("guardrails")
    if not rules or not any(rules.values()):
        return []
    if diff is None:
        diff = frozen_changes(task, manifest, rules=rules)
    violations = []
    for path, change in sorted(diff.items()):
        for rule in sorted(_matching_paths(path, rules)):
            if rule == "protected_paths" or change == "D":
                violations.append(dict(rule=rule, path=path, change=_CHANGE[change]))
                if len(violations) == _MAX_DIFF_LIST:
                    return violations
    return violations


def retain_checks(store, task, run, generation, contract, criteria, manifest, *, changes=None):
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
    product = dict(product_id=product_id, base=manifest["base"], branch=task["worktree"]["branch"],
                worktree=task["worktree"]["path"], checks_sha256=checks_sha256)
    try:
        product["diff_summary"] = summarize_diff(task, manifest, changes=changes)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # Optional visibility must never prevent settlement.
    return product, checks


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
    if "diff_summary" in product:
        _require(isinstance(product["diff_summary"], dict) and product["diff_summary"].get("base") == product["base"])
    _require(not check_guardrails(task, spec, manifest), "GUARDRAIL_VIOLATION")
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
