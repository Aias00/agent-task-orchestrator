"""Unified v1 task admission and ledger. Workflow remains the execution engine."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import wraps
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

import ato_config
import ato_core
import execution_state
import hub_contract
import runner
import workflow


_ERRORS = {
    2: {
        "USAGE": "Invalid command arguments.",
        "INVALID_ID": "Invalid identifier.",
        "INVALID_REPO": "The repository binding is invalid.",
        "INVALID_JSON": "Invalid JSON document.",
        "UNSUPPORTED_VERSION": "Unsupported schema version.",
        "INVALID_SPEC": "Invalid task specification.",
        "INVALID_EVIDENCE": "Invalid evidence.",
        "UNSUPPORTED_CAPABILITY": "The requested capability is unavailable.",
    },
    3: {
        "AUTH_REQUIRED": "A verified actor is required.",
        "NOT_FOUND": "The requested record is unavailable.",
        "FORBIDDEN": "The operation is outside the verified grant.",
    },
    4: {
        "BUSY": "The record is busy or its revision changed.",
        "NOT_RESUMABLE": "The task cannot resume.",
        "STOPPED": "The run was stopped.",
        "STALE_REQUEST": "The request is stale.",
        "BINDING_CHANGED": "The stored binding could not be verified.",
        "DECISION_CONFLICT": "A different decision is already recorded.",
        "PRODUCT_CHANGED": "The bound product changed.",
        "GUARDRAIL_VIOLATION": "The product violated a declared specification guardrail.",
        "CHECKS_REQUIRED": "Current independent checks are required.",
        "LAUNCH_FAILED": "The run failed to start.",
        "CLEANUP_BLOCKED": "Execution cleanup could not be verified.",
        "DELIVERY_UNKNOWN": "Delivery could not be verified.",
        "RUN_UNSETTLED": "The previous operation has an unresolved outcome.",
    },
    5: {
        "STATE_CORRUPT": "A trusted record could not be verified.",
        "SPEC_CHANGED": "The frozen specification no longer matches its digest.",
        "RUNTIME_MISMATCH": "The trusted runtime does not match the recorded seal.",
        "INTERNAL_ERROR": "The operation could not be completed.",
    },
}
_ERROR_DETAILS = {code: (status, message) for status, codes in _ERRORS.items()
                  for code, message in codes.items()}


class TaskError(ValueError):
    """A stable, redacted service failure with an optional authorized ID."""

    def __init__(self, code, task_id=None):
        self.code = code if code in _ERROR_DETAILS else "INTERNAL_ERROR"
        self.exit_code, message = _ERROR_DETAILS[self.code]
        self.task_id = task_id if self.exit_code != 3 and _valid_id(task_id, "task") else None
        super().__init__(message)


def result_envelope(*, data=None, error=None):
    if error is not None:
        if not isinstance(error, TaskError):
            error = TaskError("INTERNAL_ERROR")
        return dict(schema_version=1, ok=False, data=None,
                    error=dict(code=error.code, message=str(error), task_id=error.task_id))
    return dict(schema_version=1, ok=True, data=data, error=None)


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_SHA = re.compile(r"[0-9a-f]{64}")
_NATIVE_SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_WORKFLOW_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")
_GIT_BRANCH = "refs/heads/ato/"
_WORKFLOWS = Path(".codex/tasks/workflows")
_FILE_LIMIT = 1024 * 1024
_STATE_LIMIT = 16 * _FILE_LIMIT
_BLOB_LIMIT = 64 * _FILE_LIMIT
_FS_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _FS_FLAGS
_GIT_ENV = dict(PATH=os.defpath, LC_ALL="C", GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0",
                GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
                GIT_NO_REPLACE_OBJECTS="1", GIT_ALLOW_PROTOCOL="")


def _require(condition, code="STATE_CORRUPT"):
    if not condition:
        raise TaskError(code)


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _valid_id(value, kind):
    return isinstance(value, str) and re.fullmatch(kind + r"-[0-9a-f]{32}", value) is not None


def _identifier(value, kind):
    _require(_valid_id(value, kind), "INVALID_ID")
    return value


def new_id(kind):
    _require(kind in ("task", "run", "request"), "INVALID_ID")
    return f"{kind}-{uuid.uuid4().hex}"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _strict_json(data, code):
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=hub_contract._pairs)
        # Re-encoding rejects escaped surrogates and all nonfinite floats, including overflow.
        canonical_json(value)
        stack = [(value, 1)]
        while stack:
            item, depth = stack.pop()
            if isinstance(item, (dict, list)):
                _require(depth <= 32, code)
                children = item.values() if isinstance(item, dict) else item
                stack.extend((child, depth + 1) for child in children)
        _require(isinstance(value, dict), code)
        return value
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise TaskError(code) from None


def _fields(value, required, optional=(), *, code="STATE_CORRUPT"):
    _require(isinstance(value, dict) and set(required) <= value.keys()
             and value.keys() <= set(required) | set(optional), code)


def _text(value, maximum=8192, *, nonblank=True):
    _require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= maximum
             and "\x00" not in value and (not nonblank or bool(value.strip())), "INVALID_SPEC")
    return value


def _positive(value):
    return type(value) is int and value > 0


def _nonnegative(value):
    return type(value) is int and value >= 0


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _path(value):
    path = Path(value).absolute()
    if path.anchor != "/" or ".." in path.parts or any(ord(c) < 32 or ord(c) == 127 for c in str(path)):
        raise ValueError("unsafe path")
    str(path).encode("utf-8")
    return path


def _repo_path(value):
    raw = os.fspath(value)
    if not isinstance(raw, str) or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("unsafe repository path")
    return _path(Path(raw).resolve(strict=True))


def _absolute(value):
    return isinstance(value, str) and value.startswith("/") and str(_path(value)) == value


def _identity(info):
    return [info.st_dev, info.st_ino]


def _identity_valid(value):
    return isinstance(value, list) and len(value) == 2 and all(_nonnegative(x) for x in value)


def _overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


@contextmanager
def _directory(path, *, create=False, private_from=None, code="STATE_CORRUPT"):
    """Walk by directory descriptors without following any symlink."""
    fd = os.open("/", _DIR_FLAGS)
    try:
        depth = len(private_from.parts) if private_from is not None else math.inf
        _require(path == _path(path), code)
        for index, name in enumerate(path.parts[1:], 2):
            try:
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    else:
                        os.fsync(fd)
                next_fd = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise
            except OSError:
                raise TaskError(code) from None
            os.close(fd)
            fd = next_fd
            if index >= depth:
                info = os.fstat(fd)
                _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, code)
        yield fd
    finally:
        os.close(fd)


def _regular(info, private, code):
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, code)
    if private:
        _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, code)


def _file_stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _read_file(fd, name, *, limit, code="STATE_CORRUPT", private=True, stable_path=False):
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _FS_FLAGS, dir_fd=fd)
    except FileNotFoundError:
        raise
    except OSError:
        raise TaskError(code) from None
    with os.fdopen(handle, "rb") as stream:
        before = os.fstat(stream.fileno())
        if private and not before.st_nlink:
            raise TaskError("BUSY")
        _regular(before, private, code)
        _require(before.st_size <= limit, code)
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        _require(len(data) <= limit and before.st_size == after.st_size
                 and before.st_mtime_ns == after.st_mtime_ns and before.st_mode == after.st_mode, code)
        if stable_path:
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            _require(_file_stamp(before) == _file_stamp(after) == _file_stamp(current), code)
        else:
            _require(after.st_nlink in (0, 1), code)
        return data


def read_input_file(value, *, code="INVALID_SPEC"):
    """Read one bounded regular file, validating the actual bytes and file identity."""
    try:
        path = _path(value)
        with _directory(path.parent, code=code) as fd:
            data = _read_file(fd, path.name, limit=_FILE_LIMIT, code=code,
                              private=False, stable_path=True)
            with _directory(path.parent, code=code) as current:
                _require(_identity(os.fstat(fd)) == _identity(os.fstat(current)), code)
        return path, data
    except (OSError, TypeError, ValueError):
        raise TaskError(code) from None


def _read_json(fd, name, *, code="STATE_CORRUPT", canonical=True):
    data = _read_file(fd, name, limit=_STATE_LIMIT, code=code)
    value = _strict_json(data, code)
    _require(not canonical or data == canonical_json(value), code)
    return value


def _existing(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _publish(fd, name, data, *, exclusive=False):
    existing = _existing(fd, name)
    if existing is not None:
        _regular(existing, True, "STATE_CORRUPT")
        if exclusive:
            raise FileExistsError(name)
    temporary = f".write-{uuid.uuid4().hex}"
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FS_FLAGS,
                     0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Exclusive publication is serialized by the owning ledger lock.
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        if _existing(fd, temporary) is not None:
            os.unlink(temporary, dir_fd=fd)


def _save(fd, name, value, *, exclusive=False):
    data = canonical_json(value)
    _require(len(data) <= _STATE_LIMIT)
    _publish(fd, name, data, exclusive=exclusive)


@contextmanager
def _locked_file(fd, name, *, blocking=False):
    flags = os.O_RDWR | os.O_NONBLOCK | _FS_FLAGS
    with os.fdopen(os.open(name, flags, dir_fd=fd), "rb") as stream:
        _regular(os.fstat(stream.fileno()), True, "STATE_CORRUPT")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TaskError("BUSY") from None
        yield stream.fileno()


def _state_errors(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except TaskError:
            raise
        except (OSError, TypeError, ValueError, KeyError, OverflowError, RecursionError):
            raise TaskError("STATE_CORRUPT") from None
        except Exception:
            raise TaskError("INTERNAL_ERROR") from None
    return call


def _retry_busy(method):
    @wraps(method)
    def call(*args, **kwargs):
        for delay in (.005, .02, .05, .12, None):
            try:
                return method(*args, **kwargs)
            except TaskError as exc:
                if exc.code != "BUSY" or delay is None:
                    raise
                refresh = getattr(args[0], "_refresh_read_snapshot", None)
                if refresh is not None:
                    refresh()
                time.sleep(delay)
    return call


def _git(repo, *args, allowed=(0,), env=None, pass_fds=(), input_data=None):
    git = shutil.which("git", path=os.defpath)
    _require(git is not None, "UNSUPPORTED_CAPABILITY")
    argv = [git, "-C", str(repo), "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
            "-c", "protocol.allow=never", *args]
    result = subprocess.run(argv, env=_GIT_ENV if env is None else env, input=input_data,
                            capture_output=True, timeout=30, pass_fds=pass_fds)
    if result.returncode not in allowed:
        raise ValueError("Git operation failed")
    return result.stdout, result.returncode


def _git_text(repo, *args):
    return _git(repo, *args)[0].decode("utf-8").removesuffix("\n")


def _checkout_environment(repo):
    # Checkout/status can run local filters, even with global Git config disabled.
    # Keep their semantics local to this invocation and leave repository config alone.
    data = _git(repo, "config", "--includes", "--null", "--name-only",
                "--get-regexp", r"^filter\.", allowed=(0, 1))[0].decode("utf-8")
    filters = {key.rsplit(".", 1)[0] for key in data.split("\x00") if key}
    overrides = [(name + "." + field, value) for name in sorted(filters)
                 for field, value in (("process", ""), ("smudge", ""), ("clean", ""), ("required", "false"))]
    env = dict(_GIT_ENV, GIT_CONFIG_COUNT=str(len(overrides)))
    for index, (key, value) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _git_paths(repo):
    top, common, admin = _git_text(repo, "rev-parse", "--path-format=absolute",
                                  "--show-toplevel", "--git-common-dir", "--absolute-git-dir").split("\n")
    paths = tuple(_path(item).resolve(strict=True) for item in (top, common, admin))
    if paths[0] != repo:
        raise ValueError("worktree root required")
    return paths


def _git_worktrees(repo):
    data = _git(repo, "worktree", "list", "--porcelain", "-z")[0]
    entries = {}
    for entry in data.decode("utf-8").split("\x00\x00"):
        if not entry:
            continue
        fields = dict(item.partition(" ")[::2] for item in entry.split("\x00"))
        path = _path(fields.pop("worktree"))
        if path in entries:
            raise ValueError("duplicate worktree")
        entries[path] = fields
    return entries


@dataclass(frozen=True)
class Repository:
    root: str
    key: str
    root_identity: tuple[int, int]
    common_identity: tuple[int, int]


def _dir_identity(path):
    with _directory(path, code="BINDING_CHANGED") as fd:
        return _identity(os.fstat(fd))


def normalize_repository(value):
    """Normalize exact worktree roots to the primary worktree and Git common directory."""
    try:
        selected = _repo_path(value)
        _, common, admin = _git_paths(selected)
        entries = _git_worktrees(selected)
        primary = next(iter(entries))
        if (selected not in entries or "prunable" in entries[selected]
                or primary.resolve(strict=True) != primary or "bare" in entries[primary]):
            raise ValueError("invalid worktree registration")
        _, primary_common, primary_admin = _git_paths(primary)
        if primary_common != common or primary_admin != common:
            raise ValueError("inconsistent Git common directory")
        if selected != primary:
            _require(admin.parent == common / "worktrees", "INVALID_REPO")
            _git_pointers(selected, admin)
        return Repository(str(primary), str(common), tuple(_dir_identity(primary)),
                          tuple(_dir_identity(common)))
    except (TaskError, ValueError, OSError, TypeError, KeyError, StopIteration, RuntimeError,
            subprocess.SubprocessError):
        raise TaskError("INVALID_REPO") from None


def _checks(value):
    _require(isinstance(value, list) and bool(value), "INVALID_SPEC")
    for argv in value:
        _require(isinstance(argv, list) and bool(argv), "INVALID_SPEC")
        for argument in argv:
            _text(argument, nonblank=False)
    return value


def _worker(value):
    code = "INVALID_SPEC"
    _fields(value, ("objective", "scope", "authorization", "acceptance"),
            ("agent", "sandbox", "permissions", "execution_policy", "host_handoff",
             "budget", "stages", "limits", "memory"), code=code)
    for key in ("objective", "scope", "authorization"):
        _text(value[key])
    _require(value.setdefault("memory", False) is False, code)
    value.setdefault("agent", {"kind": "codex"})
    criteria = value["acceptance"]
    _require(isinstance(criteria, list) and bool(criteria), code)
    commands, manual = {}, set()
    for criterion in criteria:
        _fields(criterion, ("id", "description"), ("kind", "checks"), code=code)
        key = criterion["id"]
        _require(_matches(_ID, key) and key not in commands and key not in manual, code)
        _text(criterion["description"])
        kind = criterion.setdefault("kind", "command")
        if kind == "command":
            commands[key] = _checks(criterion.get("checks"))
        else:
            _require(kind == "manual" and "checks" not in criterion, code)
            manual.add(key)
    _require(bool(commands), code)

    # Let the existing validators check permission, adapter, budget and limit schemas.
    stages = value.pop("stages", [])
    workflow.validate(value)
    _require(isinstance(stages, list), code)
    if not stages:
        stages = [dict(id="hub-initial", covers=list(commands),
                       plan=dict(objective=value["objective"], tasks=[
                           dict(id="implement", prompt=value["objective"],
                                checks=[argv for rows in commands.values() for argv in rows])]))]
    seen = set()
    for stage in stages:
        _fields(stage, ("id", "covers", "plan"), code=code)
        key, covers, plan = (stage[name] for name in ("id", "covers", "plan"))
        _require(_matches(_ID, key) and key not in seen, code)
        seen.add(key)
        _require(isinstance(covers, list) and all(_matches(_ID, item) for item in covers)
                 and len(covers) == len(set(covers))
                 and set(covers) <= commands.keys() | manual and any(c in commands for c in covers), code)
        allowed = {tuple(argv) for c in covers for argv in commands.get(c, [])}
        _fields(plan, ("objective", "tasks"), code=code)
        _text(plan["objective"])
        _require(isinstance(plan["tasks"], list) and bool(plan["tasks"]), code)
        for step in plan["tasks"]:
            _fields(step, ("id", "prompt", "checks"), ("depends_on",), code=code)
            _require(_matches(_ID, step["id"]), code)
            _text(step["prompt"])
            depends = step.setdefault("depends_on", [])
            _require(isinstance(depends, list) and all(_matches(_ID, d) for d in depends)
                     and len(depends) == len(set(depends)), code)
            _require(all(tuple(argv) in allowed for argv in _checks(step["checks"])), code)
        effective = dict(plan, **{key: value[key] for key in (
            "agent", "sandbox", "memory", "permissions", "host_handoff", "execution_policy", "budget") if key in value})
        limits = value["limits"]
        effective["limits"] = {key: limits[key] for key in (
            "runtime_seconds", "attempt_seconds", "check_seconds", "max_attempts")}
        runner.validate(effective)
    value["stages"] = stages
    return workflow.validate(value)


def _design_doc(repo, base, name):
    hub_contract.relative(name)
    _require(not any(ord(c) < 32 or ord(c) == 127 for c in name), "INVALID_SPEC")
    rows = _git(repo, "ls-tree", "-z", base, "--", ":(literal)" + name)[0]
    entry, _, tail = rows.partition(b"\x00")
    header, _, path = entry.partition(b"\t")
    mode, kind, object_id = header.split()
    _require(path == name.encode() and not tail and kind == b"blob" and mode in (b"100644", b"100755"),
             "INVALID_SPEC")
    oid = object_id.decode("ascii")
    _require(_matches(_COMMIT, oid) and int(_git_text(repo, "cat-file", "-s", oid)) <= _FILE_LIMIT,
             "INVALID_SPEC")
    return dict(path=name, sha256=_sha(_git(repo, "cat-file", "blob", oid)[0]))


def _reviewer(value, repo, base):
    if value is None:
        return None
    _fields(value, ("agent",), ("allowed_kinds", "session_mode", "max_review_rounds",
                              "timeout_seconds", "checklist", "design_docs"), code="INVALID_SPEC")
    agent = value["agent"]
    _fields(agent, ("kind",), ("model", "effort"), code="INVALID_SPEC")
    _require(all(isinstance(v, str) for v in agent.values()), "INVALID_SPEC")
    hub_contract.HubAgentSpec(**agent)
    kinds = value.setdefault("allowed_kinds", [agent["kind"]])
    _require(isinstance(kinds, list) and all(k in hub_contract.KINDS for k in kinds)
             and len(kinds) == len(set(kinds)) and agent["kind"] in kinds, "INVALID_SPEC")
    _require(value.setdefault("session_mode", "fresh_only") in hub_contract.MODES, "INVALID_SPEC")
    hub_contract.integer(value.setdefault("max_review_rounds", 1), 1, 20)
    hub_contract.integer(value.setdefault("timeout_seconds", 300))
    for key in ("checklist", "design_docs"):
        items = value.setdefault(key, [])
        _require(isinstance(items, list) and len(items) <= 128, "INVALID_SPEC")
        for item in items:
            _text(item)
    value["design_docs"] = [_design_doc(repo, base, name) for name in value["design_docs"]]
    return value


@dataclass(frozen=True)
class NormalizedSpec:
    repository: Repository
    canonical: bytes
    input_sha256: str

    @property
    def spec_sha256(self):
        return _sha(self.canonical)

    @property
    def document(self):
        return _strict_json(self.canonical, "INVALID_SPEC")

    def preview(self):
        return dict(repo=self.repository.root, base_commit=self.document["base_ref"],
                    spec_sha256=self.spec_sha256)


def _guardrails(raw):
    fields = ("protected_paths", "forbid_delete_paths")
    _fields(raw, (), fields, code="INVALID_SPEC")
    result = {}
    for key in fields:
        paths = raw.get(key, [])
        _require(isinstance(paths, list) and len(paths) <= 128, "INVALID_SPEC")
        normalized = []
        for path in paths:
            _require(isinstance(path, str) and path.strip() and len(path.encode()) <= 1024
                     and all(ord(c) >= 32 and ord(c) != 127 for c in path), "INVALID_SPEC")
            path = path.removesuffix("/")
            hub_contract.relative(path, maximum=1024)
            _require(".git" not in path.split("/") and path != ".codex/tasks"
                     and not path.startswith(".codex/tasks/"), "INVALID_SPEC")
            normalized.append(path)
        _require(len(set(normalized)) == len(normalized), "INVALID_SPEC")
        result[key] = sorted(normalized)
    return result


def normalize_spec(repo, spec_file, *, home=None, native_homes=None):
    path, data = read_input_file(spec_file)
    value = _strict_json(data, "INVALID_JSON")
    _require("schema_version" in value and type(value["schema_version"]) is int, "INVALID_SPEC")
    _require(value["schema_version"] == 1, "UNSUPPORTED_VERSION")
    _fields(value, ("schema_version", "repo", "base_ref", "worker_contract"),
            ("reviewer", "worktree", "guardrails"), code="INVALID_SPEC")
    repository = normalize_repository(repo)
    try:
        spec_repo = path.parent / _text(value["repo"], 4096)
    except (ValueError, TypeError):
        raise TaskError("INVALID_SPEC") from None
    other = normalize_repository(spec_repo)
    _require(other == repository, "INVALID_REPO")
    _repo_boundaries(repository, home, native_homes, selected=repo)
    _repo_boundaries(repository, home, native_homes, selected=spec_repo)
    root = Path(repository.root)
    try:
        selector = _text(value["base_ref"], 255)
        _require(not selector.startswith("-") and not any(
            c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "$`\\;&|<>()" for c in selector),
            "INVALID_SPEC")
        base = _git_text(root, "rev-parse", "--verify", "--end-of-options", selector + "^{commit}")
        _require(_matches(_COMMIT, base), "INVALID_SPEC")
        value.update(repo=repository.root, repo_key=repository.key, base_ref=base)
        value["worker_contract"] = _worker(value["worker_contract"])
        value["reviewer"] = _reviewer(value.get("reviewer"), root, base)
        rules = value.pop("guardrails", None)
        if rules is not None:
            value["guardrails"] = _guardrails(rules)
        worktree = value.setdefault("worktree", {})
        _fields(worktree, (), ("shared_env_links",), code="INVALID_SPEC")
        names = worktree.setdefault("shared_env_links", [])
        _require(isinstance(names, list) and all(name in hub_contract.SHARED_ENV_LINKS for name in names)
                 and len(names) == len(set(names)), "INVALID_SPEC")
        canonical = canonical_json(value)
        _require(len(canonical) <= _STATE_LIMIT, "INVALID_SPEC")
        return NormalizedSpec(repository, canonical, _sha(data))
    except (ValueError, OSError, TypeError, KeyError, OverflowError, RecursionError,
            subprocess.SubprocessError):
        raise TaskError("INVALID_SPEC") from None


def _repo_boundaries(repository, home, native_homes, *, selected=None):
    try:
        store = _path(ato_config.unified_home() if home is None else home)
        native = (ato_config.codex_home(),) if native_homes is None else native_homes
        sources = [Path(repository.root), Path(repository.key)]
        if selected is not None:
            sources.append(_repo_path(selected))
        _require(not any(_overlaps(path, forbidden) for path in sources
                         for forbidden in (store, *map(_path, native))), "INVALID_REPO")
    except (ValueError, OSError, RuntimeError, TypeError):
        raise TaskError("INVALID_REPO") from None


@dataclass(frozen=True)
class InitialBinding:
    controller_ref: str
    interaction_ref: str
    revision: int = 1


@dataclass(frozen=True)
class SubmissionOrigin:
    """Private routing metadata. Null session/project mark an unverified locator."""

    native_home: str
    session_id: str | None
    project: str | None

    def valid(self):
        try:
            return _absolute(self.native_home) and (
                (self.session_id is None and self.project is None)
                or (_matches(_NATIVE_SESSION_ID, self.session_id) and _absolute(self.project))
            )
        except ValueError:
            return False


@dataclass(frozen=True)
class SubmissionGrant:
    """The host verifier returns this only after checking the exact submission."""

    reference: str
    repo_key: str
    spec_sha256: str
    binding: InitialBinding
    origin: SubmissionOrigin | None = None


@dataclass(frozen=True)
class TaskScope:
    task_id: str
    repo_key: str
    spec_sha256: str
    authorization_ref: str


@dataclass(frozen=True)
class RunIntent:
    run_id: str
    predecessor_run_id: str | None
    kind: str
    ordinal: int
    start_reason: str
    runtime_seal: str
    generation: tuple[float | None, int]


@dataclass(frozen=True)
class RunReceipt:
    """An assertion from the trusted execution adapter with retained proof."""

    task_id: str
    run_id: str
    spec_sha256: str
    runtime_seal: str
    generation: tuple[float | None, int]
    phase: str
    outcome: str | None
    evidence_sha256: str
    recorded_at: float


_PHASES = {
    "reserved": {"launching", "launch_failed"},
    "launching": {"active", "settled", "launch_failed", "unknown"},
    "active": {"settled", "unknown"},
    "unknown": {"active", "settled", "launch_failed"},
    "settled": set(),
    "launch_failed": set(),
}
_CREATION_PHASES = ("reserved", "allocating", "git_pending", "worktree_bound",
                    "workflow_allocating", "workflow_pending", "ready")
_TERMINAL_RUN = {"settled", "launch_failed"}
DELIVERY_OUTCOMES = frozenset({"completed", "needs_acceptance"})  # Includes older successful Run receipts.
_OUTCOMES = DELIVERY_OUTCOMES | {"stopped", "cleanup_blocked", "blocked",
             "waiting", "needs_input", "needs_host", "failed", "interrupted", "launch_failed"}
_KERNEL_STATUS = {"created", "running", "planning", "verifying", "waiting", "needs_input",
                  "needs_host", "completed", "blocked", "error", "cleanup_blocked", "stopped",
                  "interrupted", "limit", "stage_limit", "planning_limit", "contract_changed",
                  "failed", "budget_limit", "active_children", "runtime_mismatch", "permission_blocked"}
_CHECK_STATUS = {"pending", "passed", "failed", "needs_input"}
_KERNEL_CHILD_STATUS = _KERNEL_STATUS | {"plan_changed", "ready_for_checks"}


def remaining_declared_stages(contract, state):
    completed = {row["preset"] for row in state["stages"] if row["status"] == "completed"}
    return [stage for stage in contract["stages"] if stage["id"] not in completed]


def _binding_valid(value):
    _fields(value, ("controller_ref", "interaction_ref", "revision"))
    return (_matches(_REF, value["controller_ref"]) and _matches(_REF, value["interaction_ref"])
            and value["controller_ref"] != value["interaction_ref"] and _positive(value["revision"]))


def _generation_valid(value):
    return (isinstance(value, (tuple, list)) and len(value) == 2
            and (value[0] is None or _time(value[0])) and _nonnegative(value[1]))


def _git_slot_free(repo, target, branch):
    refs = _git_text(repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + branch)
    return not refs and target not in _git_worktrees(repo)


def _kernel_paths_safe(root):
    for relative in (Path(".codex"), Path(".codex/tasks")):
        try:
            with _directory(root / relative, code="BINDING_CHANGED"):
                pass
        except FileNotFoundError:
            continue
    _require(not _git(root, "ls-files", "-z", "--", ".codex/tasks")[0], "BINDING_CHANGED")


def _shared_source(repo, name):
    source = repo / name
    identity = _dir_identity(source)
    _require(not _git(repo, "ls-files", "-z", "--", name)[0] and
             _git(repo, "check-ignore", "--no-index", "-q", "--", name, allowed=(0, 1))[1] == 0,
             "BINDING_CHANGED")
    return identity


def _shared_links(repo, target, sources, *, create=False):
    links = {}
    for name, source_identity in sources.items():
        source = repo / name
        _require(_shared_source(repo, name) == source_identity, "BINDING_CHANGED")
        destination = target / name
        _require(not _git(target, "ls-files", "-z", "--", name)[0], "BINDING_CHANGED")
        expected = os.path.relpath(source, destination.parent)
        with _directory(destination.parent, create=create, code="BINDING_CHANGED") as fd:
            info = _existing(fd, destination.name)
            if info is None and create:
                os.symlink(expected, destination.name, dir_fd=fd)
                os.fsync(fd)
                info = _existing(fd, destination.name)
            _require(info is not None and stat.S_ISLNK(info.st_mode) and
                     os.readlink(destination.name, dir_fd=fd) == expected, "BINDING_CHANGED")
            links[name] = _identity(info)
    return links


def _git_pointers(target, admin):
    with _directory(target, code="BINDING_CHANGED") as fd:
        forward = _read_file(fd, ".git", limit=4096, private=False, code="BINDING_CHANGED").decode()
    with _directory(admin, code="BINDING_CHANGED") as fd:
        backward = _read_file(fd, "gitdir", limit=4096, private=False, code="BINDING_CHANGED").decode()
        admin_identity = _identity(os.fstat(fd))
    _require(forward.startswith("gitdir: "), "BINDING_CHANGED")
    registered_admin = _repo_path(target / forward.removeprefix("gitdir: ").removesuffix("\n"))
    registered_checkout = _repo_path(admin / backward.removesuffix("\n"))
    _require(registered_admin == admin and registered_checkout == target / ".git", "BINDING_CHANGED")
    return admin_identity


def _git_admin(repo, target, branch, base, expected_root, *, initial=False):
    _require(_dir_identity(target) == expected_root, "BINDING_CHANGED")
    _, common, admin = _git_paths(target)
    _require(common == Path(repo["key"]) and admin.parent == common / "worktrees",
             "BINDING_CHANGED")
    admin_identity = _git_pointers(target, admin)
    entry = _git_worktrees(Path(repo["root"])).get(target, {})
    ref = "refs/heads/" + branch
    head = _git_text(target, "rev-parse", "--verify", "HEAD^{commit}")
    _require(entry.get("branch") == ref and entry.get("HEAD") == head and "prunable" not in entry
             and _git_text(target, "symbolic-ref", "HEAD") == ref, "BINDING_CHANGED")
    if initial:
        _require(head == base, "BINDING_CHANGED")
    else:
        _require(_git(target, "merge-base", "--is-ancestor", base, head, allowed=(0, 1))[1] == 0,
                 "BINDING_CHANGED")
    return str(admin), admin_identity


def _fsync_tree(path):
    """Durably publish the new kernel files before referencing them from the ledger."""
    with _directory(path, private_from=path) as fd:
        for name in os.listdir(fd):
            info = _existing(fd, name)
            if stat.S_ISDIR(info.st_mode):
                _fsync_tree(path / name)
            else:
                handle = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _FS_FLAGS, dir_fd=fd)
                try:
                    _regular(os.fstat(handle), True, "STATE_CORRUPT")
                    os.fsync(handle)
                finally:
                    os.close(handle)
        os.fsync(fd)


# A synchronous initializer, isolated from the caller's Git, Python, actor and
# native environment. The inherited creation lock also covers parent death.
_WORKFLOW_INIT = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ato_config
import ato_task_store
import workflow
with ato_config.use_unified_kernel(sys.argv[2], sys.argv[3]):
    directory = workflow.create(sys.argv[4], sys.argv[5])
ato_task_store._record_kernel_creation(Path(sys.argv[5]).parent, directory)
"""


def _kernel_created_receipt(task, directory):
    creation = task["creation"]
    _require(directory.parent == Path(creation["target"]) / _WORKFLOWS
             and _matches(_WORKFLOW_ID, directory.name), "BINDING_CHANGED")
    return dict(schema_version=1, task_id=task["task_id"], spec_sha256=task["spec_sha256"],
                run_id=creation["initial_run_id"], workflow_dir=str(directory),
                workflow_identity=_dir_identity(directory), contract_hash=creation["contract_hash"])


def _record_kernel_creation(task_dir, directory):
    """Called by the isolated initializer while it still holds the creation lock."""
    with _directory(task_dir, private_from=task_dir.parents[2]) as fd:
        task = _read_json(fd, "task.json")
        _require(task["creation"]["phase"] == "workflow_pending", "BINDING_CHANGED")
        _save(fd, "kernel-created.json", _kernel_created_receipt(task, directory), exclusive=True)


class TaskStore:
    """Host entry points for admission and scoped observation.

    authenticate(proof) returns a verified principal, or None.
    can_observe(principal, scope) checks a current read grant on that TaskScope.
    authorize_submit(spec, proof, binding) returns a verified SubmissionGrant.
    These callbacks are supplied by trusted code. There is no ambient identity
    and the descriptive authorization in a specification never grants access.
    """

    def __init__(self, home=None, *, native_home=None, native_homes=(), worktree_parent=None,
                 runtime_seal=None, authenticate=None, can_observe=None, authorize_submit=None):
        try:
            self.home = _path(ato_config.unified_home() if home is None else home)
            self.native_home = _path(ato_config.codex_home() if native_home is None else native_home)
            self.native_homes = (self.native_home, *map(_path, native_homes))
            for path in self.native_homes:
                _require(path == path.resolve(strict=False) and not _overlaps(path, self.home),
                         "BINDING_CHANGED")
        except (OSError, TypeError, ValueError, RuntimeError):
            raise TaskError("BINDING_CHANGED") from None
        self.root = self.home / "unified-v1"
        self.kernel_root = self.root / "kernel"
        self.tasks = self.root / "tasks"
        self.worktree_parent = worktree_parent
        self.runtime_seal = runtime_seal
        self.authenticate = authenticate
        self.can_observe = can_observe
        self.authorize_submit = authorize_submit
        self.execution_projection = None

    def normalize_spec(self, repo, spec_file):
        return normalize_spec(repo, spec_file, home=self.home, native_homes=self.native_homes)

    def _private(self, path, *, create=False):
        return _directory(path, create=create, private_from=self.home)

    def _task_dir(self, task_id):
        return self.tasks / _identifier(task_id, "task")

    def _scope(self, fd, task_id):
        scope = _read_json(fd, "scope.json")
        _fields(scope, ("schema_version", "task_id", "repo_key", "spec_sha256", "authorization_ref"))
        _require(type(scope["schema_version"]) is int and scope["schema_version"] == 1
                 and scope["task_id"] == task_id and _absolute(scope["repo_key"])
                 and _matches(_SHA, scope["spec_sha256"]) and _matches(_REF, scope["authorization_ref"]))
        return TaskScope(task_id, scope["repo_key"], scope["spec_sha256"], scope["authorization_ref"])

    @_state_errors
    def submission_origin(self, task_id):
        with self._private(self._task_dir(task_id)) as fd:
            try:
                row = _read_json(fd, "origin-session.json")
            except FileNotFoundError:
                return None
        _fields(row, ("version", "task_id", "native_home", "session_id", "project"))
        origin = SubmissionOrigin(row["native_home"], row["session_id"], row["project"])
        _require(type(row["version"]) is int and row["version"] == 1 and row["task_id"] == task_id
                 and origin.valid())
        return origin

    def _spec(self, fd, task):
        try:
            data = _read_file(fd, "spec.json", limit=_STATE_LIMIT, code="SPEC_CHANGED")
            kernel_contract = _read_file(fd, "worker-contract.json", limit=_STATE_LIMIT, code="SPEC_CHANGED")
        except FileNotFoundError:
            raise TaskError("SPEC_CHANGED") from None
        _require(_sha(data) == task["spec_sha256"], "SPEC_CHANGED")
        spec = _strict_json(data, "SPEC_CHANGED")
        _require(canonical_json(spec) == data and spec.get("schema_version") == 1
                 and spec.get("repo") == task["repo"]["root"] and spec.get("repo_key") == task["repo"]["key"]
                 and spec.get("base_ref") == task["creation"]["base_commit"]
                 and canonical_json(spec.get("worker_contract")) == kernel_contract
                 and ato_core.digest(spec.get("worker_contract")) == task["creation"]["contract_hash"],
                 "SPEC_CHANGED")
        return spec

    def _task(self, fd, task_id):
        scope = self._scope(fd, task_id)
        task = _read_json(fd, "task.json")
        _fields(task, ("schema_version", "task_id", "repo", "spec_sha256", "input_sha256",
                       "authorization_ref", "binding", "revision", "created_at", "updated_at",
                       "creation", "worktree", "workflow", "run_id", "run_count", "run_hashes", "run_heads",
                       "stop_epoch", "worktree_removed"), ("stop_retired_epoch",))
        _require(type(task["schema_version"]) is int and task["schema_version"] == 1
                 and task["task_id"] == task_id and _matches(_SHA, task["input_sha256"])
                 and task["spec_sha256"] == scope.spec_sha256
                 and task["authorization_ref"] == scope.authorization_ref
                 and _positive(task["revision"]) and _time(task["created_at"]) and _time(task["updated_at"])
                 and _binding_valid(task["binding"]) and _nonnegative(task["stop_epoch"])
                 and _nonnegative(task.get("stop_retired_epoch", 0))
                 and task.get("stop_retired_epoch", 0) <= task["stop_epoch"]
                 and type(task["worktree_removed"]) is bool)
        repo = task["repo"]
        _fields(repo, ("root", "key", "root_identity", "common_identity"))
        _require(repo["key"] == scope.repo_key and _absolute(repo["root"])
                 and _identity_valid(repo["root_identity"]) and _identity_valid(repo["common_identity"]))
        creation = task["creation"]
        _fields(creation, ("phase", "problem", "target", "branch", "parent_identity", "root_identity",
                           "workflow_parent_identity", "initial_run_id", "base_commit", "runtime_seal",
                           "contract_hash", "shared_sources"))
        phase = creation["phase"]
        _require(phase in _CREATION_PHASES and creation["problem"] in (None, "BINDING_CHANGED")
                 and creation["branch"] == "ato/" + task_id
                 and _absolute(creation["target"]) and Path(creation["target"]).name == task_id
                 and _identity_valid(creation["parent_identity"])
                 and _valid_id(creation["initial_run_id"], "run") and _matches(_COMMIT, creation["base_commit"])
                 and _matches(_SHA, creation["runtime_seal"]) and _matches(_SHA, creation["contract_hash"])
                 and isinstance(creation["shared_sources"], dict)
                 and all(name in hub_contract.SHARED_ENV_LINKS and _identity_valid(value)
                         for name, value in creation["shared_sources"].items()))
        position = _CREATION_PHASES.index(phase)
        _require(_identity_valid(creation["root_identity"]) if position >= 2 else creation["root_identity"] is None)
        _require(_identity_valid(creation["workflow_parent_identity"])
                 if position >= 5 else creation["workflow_parent_identity"] is None)
        if position >= 3:
            self._validate_worktree_record(task)
        else:
            _require(task["worktree"] is None)
        _require(isinstance(task["run_hashes"], dict) and isinstance(task["run_heads"], dict))
        if phase == "ready":
            self._validate_workflow_record(task)
            _require(_valid_id(task["run_id"], "run") and _positive(task["run_count"])
                     and len(task["run_hashes"]) == task["run_count"] and task["run_id"] in task["run_hashes"]
                     and all(_valid_id(key, "run") and _matches(_SHA, value) for key, value in task["run_hashes"].items())
                     and task["run_heads"].keys() <= task["run_hashes"].keys()
                     and all(_valid_id(key, "run") and isinstance(values, list) and values
                             and all(_matches(_SHA, value) for value in values)
                             for key, values in task["run_heads"].items()))
        else:
            _require(task["workflow"] is None and task["run_id"] is None
                     and type(task["run_count"]) is int and task["run_count"] == 0
                     and not task["run_hashes"] and not task["run_heads"]
                     and not task["worktree_removed"])
        return task, self._spec(fd, task)

    @staticmethod
    def _validate_worktree_record(task):
        row, creation, repo = task["worktree"], task["creation"], task["repo"]
        _fields(row, ("repo", "common_dir", "path", "branch", "base_commit", "root_identity",
                      "git_admin", "git_admin_identity", "shared_env_links", "shared_link_identities"))
        _require(row["repo"] == repo["root"] and row["common_dir"] == repo["key"]
                 and row["path"] == creation["target"] and row["branch"] == creation["branch"]
                 and row["base_commit"] == creation["base_commit"] and row["root_identity"] == creation["root_identity"]
                 and _absolute(row["git_admin"]) and Path(row["git_admin"]).parent == Path(repo["key"]) / "worktrees"
                 and _identity_valid(row["git_admin_identity"])
                 and isinstance(row["shared_env_links"], list)
                 and row["shared_env_links"] == sorted(creation["shared_sources"])
                 and isinstance(row["shared_link_identities"], dict)
                 and set(row["shared_link_identities"]) == set(row["shared_env_links"])
                 and all(_identity_valid(value) for value in row["shared_link_identities"].values()))

    @staticmethod
    def _validate_workflow_record(task):
        row = task["workflow"]
        _fields(row, ("workflow_id", "directory", "directory_identity", "contract_hash"))
        _require(_matches(_WORKFLOW_ID, row["workflow_id"])
                 and row["directory"] == str(Path(task["worktree"]["path"]) / _WORKFLOWS / row["workflow_id"])
                 and _identity_valid(row["directory_identity"])
                 and row["contract_hash"] == task["creation"]["contract_hash"])

    @_state_errors
    def load_task(self, task_id):
        """Read and validate the private ledger and frozen spec. Trusted host API."""
        try:
            with self._private(self._task_dir(task_id)) as fd:
                return self._task(fd, task_id)[0]
        except FileNotFoundError:
            if self._has_task(task_id):
                raise TaskError("STATE_CORRUPT") from None
            raise TaskError("NOT_FOUND") from None

    def _has_task(self, task_id):
        try:
            with self._private(self.tasks) as parent:
                return _existing(parent, task_id) is not None
        except FileNotFoundError:
            return False

    @_state_errors
    def load_spec(self, task_id):
        with self._private(self._task_dir(task_id)) as fd:
            return self._task(fd, task_id)[1]

    def _worktree_target(self, spec, task_id):
        repo = Path(spec.repository.root)
        parent = _path(self.worktree_parent) if self.worktree_parent is not None else repo.parent
        target = parent / task_id
        _require(parent == parent.resolve(strict=True) and not parent.is_relative_to(repo)
                 and not any(_overlaps(target, other) or parent.is_relative_to(other)
                             for other in (Path(spec.repository.key), self.home, *self.native_homes)),
                 "INVALID_REPO")
        _require(not any(parent.is_relative_to(path) for path in _git_worktrees(repo)), "INVALID_REPO")
        enclosing, code = _git(parent, "rev-parse", "--show-toplevel", allowed=(0, 128))
        if code == 0:
            root = _path(enclosing.decode().removesuffix("\n"))
            ref = _git(root, "symbolic-ref", "-q", "HEAD", allowed=(0, 1))[0].decode().strip()
            _require(not ref.startswith(_GIT_BRANCH), "INVALID_REPO")
        return target, _dir_identity(parent)

    def _new_task(self, spec, grant, task_id):
        value = spec.document
        repo = Path(spec.repository.root)
        try:
            target, parent_identity = self._worktree_target(spec, task_id)
            base = value["base_ref"]
            entries = _git(repo, "ls-tree", "-z", base, "--", ".codex", ".codex/tasks")[0]
            for entry in entries.split(b"\x00"):
                if entry:
                    attrs, _, path = entry.partition(b"\t")
                    _require(path == b".codex" and attrs.startswith(b"040000 tree "), "INVALID_SPEC")
            sources = {name: _shared_source(repo, name) for name in value["worktree"]["shared_env_links"]}
            for name in sources:
                _require(not _git(repo, "ls-tree", "-z", base, "--", name)[0], "INVALID_SPEC")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if isinstance(exc, TaskError) and exc.code in ("INVALID_REPO", "INVALID_SPEC"):
                raise
            raise TaskError("INVALID_SPEC") from None
        now = time.time()
        creation = dict(phase="reserved", problem=None, target=str(target), branch="ato/" + task_id,
                        parent_identity=parent_identity, root_identity=None, workflow_parent_identity=None,
                        initial_run_id=new_id("run"), base_commit=base, runtime_seal=self.runtime_seal,
                        shared_sources=sources, contract_hash=ato_core.digest(value["worker_contract"]))
        repo_record = dict(root=spec.repository.root, key=spec.repository.key,
                           root_identity=list(spec.repository.root_identity),
                           common_identity=list(spec.repository.common_identity))
        return dict(schema_version=1, task_id=task_id, repo=repo_record, spec_sha256=spec.spec_sha256,
                    input_sha256=spec.input_sha256, authorization_ref=grant.reference,
                    binding=asdict(grant.binding), revision=1, created_at=now, updated_at=now,
                    creation=creation, worktree=None, workflow=None, run_id=None, run_count=0,
                    run_hashes={}, run_heads={}, stop_epoch=0, worktree_removed=False)

    def _reserve(self, spec, grant, task_id):
        task = self._new_task(spec, grant, task_id)
        scope = dict(schema_version=1, task_id=task_id, repo_key=task["repo"]["key"],
                     spec_sha256=spec.spec_sha256, authorization_ref=grant.reference)
        with self._private(self.tasks, create=True) as fd:
            staging = ".reservation-" + uuid.uuid4().hex
            os.mkdir(staging, mode=0o700, dir_fd=fd)
            try:
                with self._private(self.tasks / staging) as staged:
                    _publish(staged, "spec.json", spec.canonical, exclusive=True)
                    _save(staged, "worker-contract.json", spec.document["worker_contract"], exclusive=True)
                    _save(staged, "scope.json", scope, exclusive=True)
                    _save(staged, "task.json", task, exclusive=True)
                    if grant.origin is not None:
                        _save(staged, "origin-session.json", dict(
                            version=1, task_id=task_id, **asdict(grant.origin)), exclusive=True)
                    for name in ("runs", "evidence", "bindings", "interaction"):
                        os.mkdir(name, mode=0o700, dir_fd=staged)
                    # Stage real, private lock inodes before publishing the Task.
                    for name in ("creation.lock", "task.lock", "evidence.lock"):
                        _publish(staged, name, b"", exclusive=True)
                    os.fsync(staged)
                # Serialize the existence check and rename on the verified
                # directory inode. Closing fd releases the publication lock.
                fcntl.flock(fd, fcntl.LOCK_EX)
                if _existing(fd, task_id) is not None:
                    raise FileExistsError(task_id)
                os.rename(staging, task_id, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                if _existing(fd, staging) is not None:
                    shutil.rmtree(self.tasks / staging)

    @staticmethod
    def _advance(fd, task):
        task["revision"] += 1
        task["updated_at"] = time.time()
        _save(fd, "task.json", task)
        return task

    @contextmanager
    def _mutation(self, task_id, expected_revision):
        with self._private(self._task_dir(task_id)) as fd, _locked_file(fd, "task.lock"):
            task, spec = self._task(fd, task_id)
            _require(type(expected_revision) is int and task["revision"] == expected_revision, "BUSY")
            yield fd, task, spec

    def _creation_checkpoint(self, task, phase=None, problem=None, **updates):
        with self._mutation(task["task_id"], task["revision"]) as (fd, latest, _):
            if phase is not None:
                latest["creation"]["phase"] = phase
            latest["creation"].update(problem=problem, **updates)
            return self._advance(fd, latest)

    def _repo_matches(self, task):
        repo = task["repo"]
        try:
            current = normalize_repository(repo["root"])
            _require(current.key == repo["key"] and current.root == repo["root"]
                     and list(current.root_identity) == repo["root_identity"]
                     and list(current.common_identity) == repo["common_identity"], "BINDING_CHANGED")
            _repo_boundaries(current, self.home, self.native_homes)
        except TaskError:
            raise TaskError("BINDING_CHANGED") from None

    def _provision_worktree(self, task, creation_lock):
        creation = task["creation"]
        target, repo = Path(creation["target"]), Path(task["repo"]["root"])
        branch, base = creation["branch"], creation["base_commit"]
        _require(_dir_identity(target.parent) == creation["parent_identity"], "BINDING_CHANGED")
        if creation["phase"] in ("reserved", "allocating"):
            _require(_git_slot_free(repo, target, branch), "BINDING_CHANGED")
            if creation["phase"] == "reserved":
                task = self._creation_checkpoint(task, "allocating")
            with _directory(target.parent, code="BINDING_CHANGED") as parent:
                # An existing directory without our recorded inode is ambiguous.
                _require(_existing(parent, target.name) is None, "BINDING_CHANGED")
                os.mkdir(target.name, mode=0o700, dir_fd=parent)
                identity = _identity(_existing(parent, target.name))
                os.fsync(parent)
            task = self._creation_checkpoint(task, "git_pending", root_identity=identity)
            creation = task["creation"]
        _require(_dir_identity(target) == creation["root_identity"], "BINDING_CHANGED")
        if _git_slot_free(repo, target, branch):
            with _directory(target, code="BINDING_CHANGED") as fd:
                _require(not os.listdir(fd), "BINDING_CHANGED")
            _git(repo, "worktree", "add", "--no-guess-remote", "-b", branch, "--", str(target), base,
                 env=_checkout_environment(repo), pass_fds=(creation_lock,))
        admin, admin_identity = _git_admin(task["repo"], target, branch, base,
                                           creation["root_identity"], initial=True)
        _kernel_paths_safe(target)
        sources = creation["shared_sources"]
        link_identities = _shared_links(repo, target, sources, create=True)
        changes = _git(target, "status", "--porcelain=v1", "--untracked-files=all", "-z",
                       env=_checkout_environment(target))[0].split(b"\x00")
        _require(all(not entry or entry in {b"?? " + name.encode() for name in sources}
                     for entry in changes), "BINDING_CHANGED")
        binding = dict(repo=str(repo), common_dir=task["repo"]["key"], path=str(target),
                       branch=branch, base_commit=base, root_identity=creation["root_identity"],
                       git_admin=admin, git_admin_identity=admin_identity,
                       shared_env_links=sorted(sources), shared_link_identities=link_identities)
        return self.bind_worktree(task["task_id"], binding, task["revision"])

    @_state_errors
    def bind_worktree(self, task_id, binding, expected_revision):
        """Publish the verified worktree association using the Task revision fence."""
        task = self.load_task(task_id)
        _require(task["revision"] == expected_revision, "BUSY")
        _require(task["creation"]["phase"] == "git_pending", "BINDING_CHANGED")
        binding = _strict_json(canonical_json(binding), "STATE_CORRUPT")
        task["worktree"] = binding
        self._validate_worktree_record(task)
        self._verify_worktree(task, initial=True)
        with self._mutation(task_id, expected_revision) as (fd, latest, _):
            latest["worktree"] = binding
            latest["creation"].update(phase="worktree_bound", problem=None)
            return self._advance(fd, latest)

    def _verify_worktree(self, task, *, initial=False):
        self._repo_matches(task)
        link = task["worktree"]
        _require(link is not None and not task["worktree_removed"], "BINDING_CHANGED")
        target, repo = Path(link["path"]), Path(link["repo"])
        _require(not any(_overlaps(target, other) for other in (self.home, *self.native_homes))
                 and _dir_identity(target.parent) == task["creation"]["parent_identity"], "BINDING_CHANGED")
        admin, identity = _git_admin(task["repo"], target, link["branch"], link["base_commit"],
                                     link["root_identity"], initial=initial)
        _require(admin == link["git_admin"] and identity == link["git_admin_identity"], "BINDING_CHANGED")
        _kernel_paths_safe(target)
        _require(_shared_links(repo, target, task["creation"]["shared_sources"])
                 == link["shared_link_identities"], "BINDING_CHANGED")
        return target

    def verify_worktree(self, task_id):
        """Return the canonical worktree only after verifying its current Git binding."""
        task = self.load_task(task_id)
        try:
            return self._verify_worktree(task)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise TaskError("BINDING_CHANGED", task_id) from None

    def _workflow_parent(self, task, target):
        parent = target / _WORKFLOWS
        if task["creation"]["phase"] == "worktree_bound":
            task = self._creation_checkpoint(task, "workflow_allocating")
        if task["creation"]["phase"] == "workflow_allocating":
            with _directory(parent.parent, create=True, private_from=parent.parent,
                            code="BINDING_CHANGED") as fd:
                _require(_existing(fd, parent.name) is None, "BINDING_CHANGED")
                os.mkdir(parent.name, mode=0o700, dir_fd=fd)
                identity = _identity(_existing(fd, parent.name))
                os.fsync(fd)
            task = self._creation_checkpoint(task, "workflow_pending", workflow_parent_identity=identity)
        return task, parent

    def _kernel_create(self, task, target, creation_lock):
        worker_state = target / ".codex/tasks"
        with _directory(worker_state, private_from=worker_state, code="BINDING_CHANGED") as fd:
            _require(os.listdir(fd) == ["workflows"], "BINDING_CHANGED")
        control_dir = self.kernel_root / "ctask-interaction" / "control"
        with self._private(control_dir, create=True) as fd:
            if _existing(fd, "config.json") is not None:
                _read_json(fd, "config.json", canonical=False)
        source = self._task_dir(task["task_id"]) / "worker-contract.json"
        env = dict(_GIT_ENV, HOME=str(self.kernel_root))
        command = [sys.executable, "-I", "-B", "-c", _WORKFLOW_INIT,
                   str(Path(__file__).resolve().parent), str(self.kernel_root), str(self.native_home),
                   str(target), str(source)]
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=45, umask=0o077, pass_fds=(creation_lock,))
        _require(result.returncode == 0, "BINDING_CHANGED")

    def _workflow_backref(self, task, link):
        return dict(schema_version=1, task_id=task["task_id"], spec_sha256=task["spec_sha256"],
                    initial_run_id=task["creation"]["initial_run_id"], workflow_id=link["workflow_id"],
                    contract_hash=link["contract_hash"], directory=link["directory"],
                    directory_identity=link["directory_identity"], worktree=task["worktree"]["path"],
                    worktree_identity=task["worktree"]["root_identity"], namespace=str(self.root))

    @staticmethod
    def _workflow_files(fd, task, link):
        _require(_identity(os.fstat(fd)) == link["directory_identity"], "BINDING_CHANGED")
        try:
            contract = _read_json(fd, "contract.json", code="SPEC_CHANGED", canonical=False)
        except FileNotFoundError:
            raise TaskError("SPEC_CHANGED") from None
        _require(ato_core.digest(contract) == link["contract_hash"], "SPEC_CHANGED")
        state = _read_json(fd, "state.json", canonical=False)
        origin = _read_json(fd, "origin.json", canonical=False)
        _require(state.get("kind") == "workflow" and state.get("id") == link["workflow_id"]
                 and origin.get("run_id") == link["workflow_id"]
                 and state.get("repo") == origin.get("repo") == task["worktree"]["path"]
                 and state.get("contract_hash") == link["contract_hash"], "BINDING_CHANGED")
        return state, contract, origin

    def _provision_workflow(self, task, target, creation_lock):
        task, parent = self._workflow_parent(task, target)
        with _directory(parent, private_from=parent.parent, code="BINDING_CHANGED") as fd, \
                self._private(self._task_dir(task["task_id"])) as task_fd:
            _require(_identity(os.fstat(fd)) == task["creation"]["workflow_parent_identity"],
                     "BINDING_CHANGED")
            entries = os.listdir(fd)
            if not entries:
                _require(_existing(task_fd, "kernel-created.json") is None, "BINDING_CHANGED")
                self._kernel_create(task, target, creation_lock)
                entries = os.listdir(fd)
            _require(len(entries) == 1 and _matches(_WORKFLOW_ID, entries[0]), "BINDING_CHANGED")
            workflow_id = entries[0]
            _require(_existing(task_fd, "kernel-created.json") is not None, "BINDING_CHANGED")
            proof = _read_json(task_fd, "kernel-created.json")
        directory = parent / workflow_id
        _require(_kernel_created_receipt(task, directory) == proof, "BINDING_CHANGED")
        link = dict(workflow_id=workflow_id, directory=str(directory),
                    directory_identity=proof["workflow_identity"],
                    contract_hash=task["creation"]["contract_hash"])
        with _directory(directory, private_from=parent.parent, code="BINDING_CHANGED") as fd:
            state, contract, origin = self._workflow_files(fd, task, link)
            expected_criteria = {row["id"]: {"status": "pending"} for row in contract["acceptance"]}
            _require(state.get("status") == "created" and state.get("criteria") == expected_criteria
                     and state.get("stages") == [] and state.get("planning_attempts") == 0
                     and state.get("elapsed_seconds") == 0 and state.get("execution_revision", 0) == 0
                     and not any(state.get(name) for name in ("owner_pid", "active_process", "active_run"))
                     and _time(state.get("created_at")) and _time(origin.get("created_at"))
                     and origin.get("origin_thread_id") is None and "origin_session_ref" not in origin
                     and _existing(fd, "STOP") is None and _existing(fd, "events.jsonl") is None,
                     "BINDING_CHANGED")
            _require(_read_json(fd, "planner-schema.json", canonical=False) == workflow.PLANNER_SCHEMA,
                     "BINDING_CHANGED")
            for name in ("evidence", "checkpoints", "acknowledgements"):
                with _directory(directory / name, private_from=directory, code="BINDING_CHANGED") as child:
                    _require(not os.listdir(child), "BINDING_CHANGED")
            if contract.get("budget"):
                ledger = _read_json(fd, "budget.json", canonical=False)
                _require(ledger.get("contract_hash") == link["contract_hash"], "BINDING_CHANGED")
            backref = self._workflow_backref(task, link)
            if _existing(fd, "ato-binding.json") is not None:
                _require(_read_json(fd, "ato-binding.json") == backref, "BINDING_CHANGED")
            else:
                _save(fd, "ato-binding.json", backref, exclusive=True)
        _fsync_tree(directory)
        with _directory(parent, private_from=parent.parent) as fd:
            os.fsync(fd)
        run = RunIntent(task["creation"]["initial_run_id"], None, "initial", 1, "TASK_CREATED",
                        task["creation"]["runtime_seal"], (None, 0))
        with self._mutation(task["task_id"], task["revision"]) as (fd, latest, _):
            latest["workflow"] = link
            latest["creation"].update(phase="ready", problem=None)
            self._publish_run(latest, run, created_at=task["created_at"])
            return self._advance(fd, latest)

    @_state_errors
    def create_task(self, spec, authority, binding, *, intent_id=None):
        """Reserve and provision a Task and first Run without starting execution.

        An optional Task-shaped intent ID belongs to the trusted caller. Retry
        with that same ID and verified inputs to reconcile an interrupted create.
        Independent calls that omit it always allocate independent Tasks.
        """
        _require(type(spec) is NormalizedSpec, "INVALID_SPEC")
        _require(type(binding) is InitialBinding and _binding_valid(asdict(binding)), "FORBIDDEN")
        _require(self.authorize_submit is not None, "AUTH_REQUIRED")
        grant = self.authorize_submit(spec, authority, binding)
        _require(grant is not None, "AUTH_REQUIRED")
        _require(type(grant) is SubmissionGrant and grant.binding == binding
                 and grant.spec_sha256 == spec.spec_sha256 and grant.repo_key == spec.repository.key
                 and _matches(_REF, grant.reference), "FORBIDDEN")
        _require(grant.origin is None or (type(grant.origin) is SubmissionOrigin and grant.origin.valid()),
                 "FORBIDDEN")
        _require(_matches(_SHA, self.runtime_seal), "RUNTIME_MISMATCH")
        _require(_matches(_SHA, spec.input_sha256) and canonical_json(spec.document) == spec.canonical,
                 "INVALID_SPEC")
        try:
            current = normalize_repository(spec.repository.root)
            _require(current == spec.repository, "BINDING_CHANGED")
        except TaskError:
            raise TaskError("BINDING_CHANGED") from None
        _repo_boundaries(current, self.home, self.native_homes)

        if intent_id is not None:
            task_id = _identifier(intent_id, "task")
            if not self._has_task(task_id):
                try:
                    self._reserve(spec, grant, task_id)
                except FileExistsError:
                    pass
        else:
            for _ in range(8):
                task_id = new_id("task")
                try:
                    self._reserve(spec, grant, task_id)
                except FileExistsError:
                    continue
                break
            else:
                raise TaskError("BUSY")

        with self._private(self._task_dir(task_id)) as fd:
            # Check the independent visibility anchor before disclosing any
            # details about an occupied ID, even when its task record is damaged.
            try:
                scope = self._scope(fd, task_id)
            except (OSError, TaskError):
                raise TaskError("NOT_FOUND") from None
            _require(scope == TaskScope(task_id, grant.repo_key, grant.spec_sha256, grant.reference),
                     "NOT_FOUND")
            with _locked_file(fd, "creation.lock") as creation_lock:
                task, _ = self._task(fd, task_id)
                _require(task["input_sha256"] == spec.input_sha256 and task["binding"] == asdict(binding),
                         "DECISION_CONFLICT")
                _require(task["creation"]["runtime_seal"] == self.runtime_seal, "RUNTIME_MISMATCH")
                if task["creation"]["phase"] != "ready":
                    try:
                        if task["worktree"] is None:
                            task = self._provision_worktree(task, creation_lock)
                        target = self._verify_worktree(task, initial=True)
                        task = self._provision_workflow(task, target, creation_lock)
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        if isinstance(exc, TaskError) and exc.code not in ("BINDING_CHANGED", "SPEC_CHANGED"):
                            raise TaskError(exc.code, task_id) from None
                        latest = self.load_task(task_id)
                        if latest["creation"]["problem"] is None:
                            self._creation_checkpoint(latest, problem="BINDING_CHANGED")
                        code = exc.code if isinstance(exc, TaskError) else "BINDING_CHANGED"
                        raise TaskError(code, task_id) from None
                self._history(task)
                return dict(task_id=task_id, spec_sha256=spec.spec_sha256)

    def _run_body(self, task, run, created_at):
        workflow_link = task["workflow"]
        return dict(schema_version=1, task_id=task["task_id"], spec_sha256=task["spec_sha256"],
                    worktree=task["worktree"]["path"], workflow_id=workflow_link["workflow_id"],
                    workflow_dir=workflow_link["directory"], contract_hash=workflow_link["contract_hash"],
                    created_at=created_at, **asdict(run))

    @staticmethod
    def _validate_run_intent(row):
        return (_valid_id(row["run_id"], "run") and _positive(row["ordinal"])
                and _matches(_SHA, row["runtime_seal"]) and _matches(_ID, row["start_reason"])
                and _generation_valid(row["generation"])
                and (row["kind"] == "initial" and row["ordinal"] == 1 and row["predecessor_run_id"] is None
                     or row["kind"] in ("resume", "recheck") and row["ordinal"] > 1
                     and _valid_id(row["predecessor_run_id"], "run")))

    def _publish_run(self, task, run, *, created_at=None):
        with self._private(self._task_dir(task["task_id"]) / "runs") as fd:
            name = run.run_id + ".json"
            if _existing(fd, name) is None:
                body = self._run_body(task, run, time.time() if created_at is None else created_at)
                _save(fd, name, body, exclusive=True)
            else:
                body = _read_json(fd, name)
                _require(_time(body.get("created_at"))
                         and (created_at is None or body["created_at"] == created_at)
                         and canonical_json(body) == canonical_json(self._run_body(task, run, body["created_at"])),
                         "DECISION_CONFLICT")
                os.fsync(fd)
        task["run_hashes"][run.run_id] = _sha(canonical_json(body))
        task["run_id"], task["run_count"] = run.run_id, run.ordinal

    def _run(self, task, run_id):
        expected = task["run_hashes"].get(run_id)
        _require(expected is not None, "NOT_FOUND")
        with self._private(self._task_dir(task["task_id"]) / "runs") as fd:
            run = _read_json(fd, run_id + ".json")
        _require(_sha(canonical_json(run)) == expected)
        _fields(run, ("schema_version", "task_id", "spec_sha256", "worktree", "workflow_id", "workflow_dir",
                      "contract_hash", "created_at", "run_id", "predecessor_run_id", "kind", "ordinal",
                      "start_reason", "runtime_seal", "generation"))
        link = task["workflow"]
        _require(type(run["schema_version"]) is int and run["schema_version"] == 1
                 and run["run_id"] == run_id and run["task_id"] == task["task_id"]
                 and run["spec_sha256"] == task["spec_sha256"] and _time(run["created_at"])
                 and run["worktree"] == task["worktree"]["path"] and run["workflow_id"] == link["workflow_id"]
                 and run["workflow_dir"] == link["directory"] and run["contract_hash"] == link["contract_hash"]
                 and self._validate_run_intent(run))
        if run["kind"] == "initial":
            _require(run_id == task["creation"]["initial_run_id"] and run["generation"] == [None, 0]
                     and run["runtime_seal"] == task["creation"]["runtime_seal"])
        receipts = []
        for digest in task["run_heads"].get(run_id, []):
            body = _strict_json(self._read_blob(task["task_id"], digest), "STATE_CORRUPT")
            self._validate_receipt(run, body, receipts[-1][1] if receipts else None)
            self._read_blob(task["task_id"], body["evidence_sha256"])
            receipts.append((digest, body))
        return run, receipts

    def _history(self, task):
        history = []
        run_id = task["run_id"]
        for ordinal in range(task["run_count"], 0, -1):
            run, receipts = self._run(task, run_id)
            _require(run["ordinal"] == ordinal)
            history.append((run, receipts))
            run_id = run["predecessor_run_id"]
        _require(run_id is None)
        for _, receipts in history[1:]:
            _require(bool(receipts) and receipts[-1][1]["phase"] in _TERMINAL_RUN)
        return history[::-1]

    @_state_errors
    def load_run(self, task_id, run_id):
        """Return a committed Run and its receipt pointers, never an orphaned intent."""
        _identifier(run_id, "run")
        task = self.load_task(task_id)
        for run, receipts in self._history(task):
            if run["run_id"] == run_id:
                if not task["worktree_removed"]:
                    self._kernel_facts(task)
                return dict(run, phase=receipts[-1][1]["phase"] if receipts else "reserved",
                            receipt_refs=[digest for digest, _ in receipts])
        raise TaskError("NOT_FOUND")

    @_state_errors
    def append_run(self, task_id, run, expected_revision):
        """Link one same-worktree Run after the trusted driver admits it.

        The caller owns repo admission and kernel execution authorization.
        An unsettled previous Run always fences this structural transaction.
        """
        _require(type(run) is RunIntent and self._validate_run_intent(asdict(run)) and run.kind != "initial",
                 "INVALID_ID")
        task = self.load_task(task_id)
        _require(task["revision"] == expected_revision, "BUSY")
        _require(task["creation"]["phase"] == "ready" and not task["worktree_removed"], "NOT_RESUMABLE")
        _require(run.runtime_seal == self.runtime_seal, "RUNTIME_MISMATCH")
        _require(run.run_id not in task["run_hashes"] and run.predecessor_run_id == task["run_id"]
                 and run.ordinal == task["run_count"] + 1, "DECISION_CONFLICT")
        previous = self._history(task)[-1][1]
        _require(bool(previous) and previous[-1][1]["phase"] in _TERMINAL_RUN, "RUN_UNSETTLED")
        _require(previous[-1][1]["outcome"] != "cleanup_blocked", "CLEANUP_BLOCKED")
        _, _, generation, _ = self._kernel_facts(task)
        _require(list(run.generation) == generation, "BINDING_CHANGED")
        with self._mutation(task_id, expected_revision) as (fd, latest, _):
            self._publish_run(latest, run)
            return self._advance(fd, latest)

    def _read_blob(self, task_id, digest):
        _require(_matches(_SHA, digest))
        with self._private(self._task_dir(task_id) / "evidence") as fd:
            data = _read_file(fd, digest, limit=_BLOB_LIMIT)
        _require(_sha(data) == digest)
        return data

    def _put_blob(self, task_id, data):
        digest = _sha(data)
        with self._private(self._task_dir(task_id)) as task_fd:
            with _locked_file(task_fd, "evidence.lock", blocking=True):
                with self._private(self._task_dir(task_id) / "evidence") as fd:
                    if _existing(fd, digest) is None:
                        _publish(fd, digest, data, exclusive=True)
                    else:
                        _require(_read_file(fd, digest, limit=_BLOB_LIMIT) == data)
                        os.fsync(fd)
        return digest

    @_state_errors
    def put_evidence(self, task_id, data):
        _require(type(data) is bytes and len(data) <= _BLOB_LIMIT, "INVALID_EVIDENCE")
        self.load_task(task_id)
        return self._put_blob(task_id, data)

    @_state_errors
    def read_evidence(self, task_id, digest):
        self.load_task(task_id)
        return self._read_blob(task_id, digest)

    @staticmethod
    def _validate_receipt_body(run, body, code):
        _fields(body, ("task_id", "run_id", "spec_sha256", "runtime_seal", "generation", "phase",
                       "outcome", "evidence_sha256", "recorded_at"), code=code)
        _require(all(body[k] == run[k] for k in ("task_id", "run_id", "spec_sha256", "runtime_seal"))
                 and body["phase"] in _PHASES.keys() - {"reserved"}
                 and _generation_valid(body["generation"])
                 and _matches(_SHA, body["evidence_sha256"]) and _time(body["recorded_at"])
                 and body["recorded_at"] >= run["created_at"], code)
        if body["phase"] == "settled":
            _require(body["outcome"] in _OUTCOMES - {"launch_failed"}, code)
        elif body["phase"] == "launch_failed":
            _require(body["outcome"] == "launch_failed", code)
        else:
            _require(body["outcome"] is None, code)

    @classmethod
    def _validate_receipt(cls, run, body, previous, *, code="STATE_CORRUPT"):
        cls._validate_receipt_body(run, body, code)
        old_phase = previous["phase"] if previous else "reserved"
        generation = body["generation"]
        prior = previous["generation"] if previous else run["generation"]
        _require(body["phase"] in _PHASES[old_phase] and generation[1] >= prior[1]
                 and (prior[0] is None or (generation[0] is not None and generation[0] >= prior[0]))
                 and (previous is None or body["recorded_at"] >= previous["recorded_at"]), code)
        if body["phase"] == "launch_failed":
            _require(list(generation) == run["generation"], code)

    @_state_errors
    def record_run_receipt(self, task_id, run_id, receipt, expected_revision):
        _identifier(run_id, "run")
        _require(type(receipt) is RunReceipt, "INVALID_EVIDENCE")
        task = self.load_task(task_id)
        _require(task["revision"] == expected_revision, "BUSY")
        run, receipts = self._run(task, run_id)
        body = asdict(receipt)
        encoded = canonical_json(body)
        digest = _sha(encoded)
        if any(digest == old for old, _ in receipts):
            return task
        self._validate_receipt_body(run, body, "RUN_UNSETTLED")
        self._read_blob(task_id, receipt.evidence_sha256)
        # Retain valid late proofs even if their Run can no longer advance.
        self._put_blob(task_id, encoded)
        _require(task["run_id"] == run_id, "RUN_UNSETTLED")
        self._validate_receipt(run, body, receipts[-1][1] if receipts else None, code="RUN_UNSETTLED")
        if receipt.phase in ("active", "settled"):
            _, _, generation, _ = self._kernel_facts(task)
            _require(list(receipt.generation) == generation, "RUN_UNSETTLED")
        with self._mutation(task_id, expected_revision) as (fd, latest, _):
            latest["run_heads"].setdefault(run_id, []).append(digest)
            return self._advance(fd, latest)

    def _stage_facts(self, task, contract, state):
        """Project child attempts through the kernel's native parent links."""
        root = Path(task["worktree"]["path"])
        workflow_link = task["workflow"]
        records = state.get("stages")
        _require(isinstance(records, list) and _nonnegative(state.get("planning_attempts"))
                 and _time(state.get("elapsed_seconds")))
        criteria = {criterion["id"] for criterion in contract["acceptance"]}
        stage_id, steps, directories = None, [], set()
        active_child = False
        latest = state.get("updated_at", state.get("created_at"))
        _require(_time(latest))
        for stage in records:
            covers, preset, status = stage["covers"], stage["preset"], stage["status"]
            location = stage["run_dir"]
            _require(_absolute(location) and location not in directories
                     and isinstance(covers, list) and set(covers) <= criteria
                     and (preset is None or _matches(_ID, preset))
                     and status in _KERNEL_CHILD_STATUS)
            directories.add(location)
            directory = Path(location)
            _require(directory.parent == root / ".codex/tasks" and _matches(_WORKFLOW_ID, directory.name))
            with _directory(directory, private_from=directory.parent) as fd:
                parent = _read_json(fd, "parent.json", canonical=False)
                child = _read_json(fd, "state.json", canonical=False)
                plan = _read_json(fd, "plan.json", canonical=False)
            _require(parent.get("workflow_id") == workflow_link["workflow_id"]
                     and parent.get("workflow_dir") == workflow_link["directory"]
                     and parent.get("covers") == covers and child.get("repo") == str(root)
                     and child.get("id") == directory.name and child.get("plan_hash") == ato_core.digest(plan)
                     and child.get("status") in _KERNEL_CHILD_STATUS and isinstance(child.get("tasks"), dict))
            observed = child.get("updated_at", child.get("created_at"))
            _require(_time(observed))
            latest = max(latest, observed)
            active_child |= bool(child.get("owner_pid") or child.get("active_process"))
            stage_id = preset
            for step in plan["tasks"]:
                key = step["id"]
                row = child["tasks"][key]
                count = row.get("attempt_sequence", row.get("attempts"))
                _require(_matches(_ID, key) and _nonnegative(count)
                         and row["status"] in _KERNEL_CHILD_STATUS | {"pending", "dependency_blocked"})
                steps.append(dict(stage_id=preset, step_id=key, state=row["status"], attempts=count))
        _require(state.get("active_run") is None or state["active_run"] in directories)
        return dict(state=state["status"], stage_id=stage_id, criterion_ids=sorted(criteria),
                    remaining_stage_ids=[row["id"] for row in remaining_declared_stages(contract, state)],
                    steps=steps, attempts=sum(step["attempts"] for step in steps),
                    elapsed_seconds=state["elapsed_seconds"], planning_attempts=state["planning_attempts"]), active_child, latest

    def _kernel_facts(self, task):
        try:
            target = self._verify_worktree(task)
        except (ValueError, OSError, subprocess.SubprocessError):
            raise TaskError("BINDING_CHANGED") from None
        link = task["workflow"]
        directory = Path(link["directory"])
        with _directory(directory, private_from=target / ".codex/tasks") as fd:
            _require(_dir_identity(directory.parent) == task["creation"]["workflow_parent_identity"],
                     "BINDING_CHANGED")
            state, contract, _ = self._workflow_files(fd, task, link)
            _require(_read_json(fd, "ato-binding.json") == self._workflow_backref(task, link),
                     "BINDING_CHANGED")
            for name in ("events.jsonl", "STOP"):
                info = _existing(fd, name)
                if info is not None:
                    _regular(info, True, "STATE_CORRUPT")
            stopped = _existing(fd, "STOP") is not None
            generation = execution_state.generation(directory, state)
            _require(_generation_valid(generation) and state.get("status") in _KERNEL_STATUS)
            if _read_json(fd, "state.json", canonical=False) != state:
                raise TaskError("BUSY")
        return state, contract, generation, stopped

    def _principal(self, proof):
        _require(self.authenticate is not None and self.can_observe is not None, "AUTH_REQUIRED")
        try:
            principal = self.authenticate(proof)
        except (ValueError, PermissionError):
            raise TaskError("AUTH_REQUIRED") from None
        _require(principal is not None, "AUTH_REQUIRED")
        return principal

    def _visible_scope(self, task_id, principal, *, observe=None):
        # Missing, damaged and out-of-scope anchor records all look unavailable
        # until the host has established a matching observation grant.
        try:
            with self._private(self._task_dir(task_id)) as fd:
                scope = self._scope(fd, task_id)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return None
        return scope if (observe or self.can_observe)(principal, scope) is True else None

    def _project(self, task, spec):
        creation, worktree = task["creation"], task["worktree"]
        history = self._history(task)
        current = history[-1][1] if history else []
        last = current[-1][1] if current else None
        phase = last["phase"] if last else ("reserved" if history else None)
        status, reason, next_step = "preparing", None, "PROVISION_TASK"
        execution = dict(state="idle", run_id=task["run_id"], pid=None, owner_role=None,
                         verified=False, stop_requested=task["stop_epoch"] > task.get("stop_retired_epoch", 0), lease=None)
        stage, generation, checks, updated = None, None, [], task["updated_at"]
        worktree_state = None if worktree is None else "present"
        status_data = None
        if task["worktree_removed"]:
            worktree_state = "removed"
        elif creation["phase"] == "ready":
            state, contract, generation, kernel_stop = self._kernel_facts(task)
            stage, child_active, kernel_updated = self._stage_facts(task, contract, state)
            updated = max(updated, kernel_updated)
            criteria = state.get("criteria")
            _require(isinstance(criteria, dict) and criteria.keys() == {c["id"] for c in contract["acceptance"]})
            for criterion in contract["acceptance"]:
                key = criterion["id"]
                check_status = criteria[key]["status"]
                _require(check_status in _CHECK_STATUS)
                if criterion["kind"] == "command":
                    checks.append(dict(criterion_id=key, state=check_status, receipt_id=None))
            activity = bool(state.get("owner_pid") or state.get("active_process") or child_active)
            execution["stop_requested"] |= kernel_stop
            status_data = state["status"], activity

        if creation["phase"] != "ready":
            if creation["problem"]:
                reason, next_step = creation["problem"], "RECONCILE_CREATION"
        elif (status_data and status_data[0] == "cleanup_blocked") or (last and last["outcome"] == "cleanup_blocked"):
            status, reason, next_step = "cleanup_blocked", "CLEANUP_BLOCKED", "RECONCILE_CLEANUP"
            execution["state"] = "unknown"
        elif phase == "unknown":
            status, reason, next_step = "delivery_unknown", "RUN_UNSETTLED", "RECONCILE_RUN"
            execution["state"] = "unknown"
        elif execution["stop_requested"]:
            if phase == "settled" and last["outcome"] == "stopped" and (status_data is None or not status_data[1]):
                status, reason, next_step = "stopped", "STOPPED", "REVIEW_STOPPED_TASK"
                execution["verified"] = True
            else:
                status, reason, next_step = "stopping", "STOPPED", "VERIFY_CLEANUP"
                execution["state"] = "stopping"
        elif phase == "launching":
            status, next_step = "launching", "VERIFY_STARTUP"
            execution["state"] = "launching"
        elif phase == "active" or (status_data and
                (status_data[1] or phase == "reserved" and status_data[0] != "created")):
            status, reason, next_step = "delivery_unknown", "RUN_UNSETTLED", "VERIFY_EXECUTION_OWNER"
            execution["state"] = "unknown"
        elif phase == "launch_failed":
            status, reason, next_step = "blocked", "LAUNCH_FAILED", "REVIEW_LAUNCH_FAILURE"
            execution["verified"] = True
        elif phase == "settled":
            outcome = last["outcome"]
            execution["verified"] = True
            status, reason, next_step = "blocked", "CHECKS_REQUIRED", "VERIFY_PRODUCT_AND_CHECKS"
            if outcome in {"waiting", "needs_input", "needs_host"}:
                status, reason, next_step = outcome, None, "REVIEW_PENDING_ACTION"
            elif outcome in {"blocked", "failed", "interrupted"}:
                reason, next_step = "WORKFLOW_BLOCKED", "REVIEW_WORKFLOW"
        elif phase == "reserved":
            next_step = "ADMIT_INITIAL_RUN"
            execution["verified"] = bool(status_data and status_data[0] == "created")
        manual = [row["id"] for row in spec["worker_contract"]["acceptance"] if row["kind"] == "manual"]
        projection = dict(task_id=task["task_id"], repo=task["repo"]["root"], status=status, reason=reason,
                    updated_at=updated, spec_sha256=task["spec_sha256"], execution=execution,
                    run_history=[self._run_summary(run, receipts) for run, receipts in history],
                    stage=stage, checks=checks, review=None, product=None,
                    acceptance=dict(disposition="not_recorded", criteria=manual, receipt_id=None),
                    pending=[], delivery=[], worktree_state=worktree_state, next_step=next_step)
        if self.execution_projection is not None:
            self.execution_projection(task, spec, history, generation, projection)
        return projection, generation

    @staticmethod
    def _run_summary(run, receipts):
        last = receipts[-1][1] if receipts else None
        terminal = last and last["phase"] in _TERMINAL_RUN
        return dict(run_id=run["run_id"], kind=run["kind"], phase=last["phase"] if last else "reserved",
                    outcome=last["outcome"] if terminal else None,
                    created_at=run["created_at"], settled_at=last["recorded_at"] if terminal else None)

    def _read_projection(self, task_id, scope=None):
        for _ in range(3):
            try:
                with self._private(self._task_dir(task_id)) as fd:
                    task, spec = self._task(fd, task_id)
                    if scope is not None:
                        _require(self._scope(fd, task_id) == scope, "NOT_FOUND")
                    status, generation = self._project(task, spec)
                    latest = _read_json(fd, "task.json")
                    if latest == task:
                        return task, status, generation
                    _require(latest.get("revision") != task["revision"])
            except TaskError as exc:
                if exc.code != "BUSY":
                    raise
        raise TaskError("BUSY")

    @_state_errors
    def task_snapshot(self, task_id):
        """Shared trusted snapshot for execution, interaction and binding adapters."""
        task, status, generation = self._read_projection(task_id)
        link = task["workflow"] or {}
        return dict(task_id=task_id, repo_key=task["repo"]["key"], spec_sha256=task["spec_sha256"],
                    revision=task["revision"], stop_epoch=task["stop_epoch"], run_id=task["run_id"],
                    workflow_id=link.get("workflow_id"), contract_hash=link.get("contract_hash"),
                    generation=generation, execution=status["execution"], product=status["product"])

    @_state_errors
    def task_status(self, actor, task_id):
        _identifier(task_id, "task")
        principal = self._principal(actor)
        scope = self._visible_scope(task_id, principal)
        _require(scope is not None, "NOT_FOUND")
        try:
            result = self._read_projection(task_id, scope)[1]
            _require(self.can_observe(principal, scope) is True, "NOT_FOUND")
            return result
        except TaskError as exc:
            raise TaskError(exc.code, task_id) from None
        except (OSError, TypeError, ValueError, KeyError, OverflowError):
            raise TaskError("STATE_CORRUPT", task_id) from None

    @_state_errors
    def list_tasks(self, actor, repo=None):
        principal = self._principal(actor)
        key = None
        if repo is not None:
            repository = normalize_repository(repo)
            _repo_boundaries(repository, self.home, self.native_homes, selected=repo)
            key = repository.key
        try:
            with self._private(self.tasks) as fd:
                identifiers = sorted(name for name in os.listdir(fd) if _valid_id(name, "task"))
        except FileNotFoundError:
            return dict(tasks=[], warnings=[])
        tasks, warnings = [], set()
        for task_id in identifiers:
            scope = self._visible_scope(task_id, principal)
            if scope is None or (key is not None and key != scope.repo_key):
                continue
            try:
                status = self._read_projection(task_id, scope)[1]
                if self.can_observe(principal, scope) is True:
                    tasks.append({name: status[name] for name in ("task_id", "repo", "status", "reason", "updated_at")})
            except TaskError as exc:
                if exc.code != "NOT_FOUND" and self.can_observe(principal, scope) is True:
                    warnings.add(exc.code)
            except (OSError, TypeError, ValueError, KeyError, OverflowError):
                if self.can_observe(principal, scope) is True:
                    warnings.add("STATE_CORRUPT")
        return dict(tasks=tasks, warnings=sorted(warnings))
