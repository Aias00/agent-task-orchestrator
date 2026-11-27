"""Task specification validation, error taxonomy, and review verdict contracts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import uuid

from ato import config


ERRORS: dict[int, dict[str, str]] = {
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
        "NOT_FOUND": "The requested record is unavailable.",
        "FORBIDDEN": "The operation is not permitted.",
    },
    4: {
        "BUSY": "The record is busy or its state changed concurrently.",
        "NOT_RESUMABLE": "The task cannot resume.",
        "STOPPED": "The run was stopped.",
        "STALE_REQUEST": "The request is stale.",
        "BINDING_CHANGED": "The repository or worktree binding changed.",
        "DECISION_CONFLICT": "A different decision is already recorded.",
        "PRODUCT_CHANGED": "The bound product changed.",
        "GUARDRAIL_VIOLATION": "The product violated a declared specification guardrail.",
        "CHECKS_REQUIRED": "Current independent checks are required.",
        "LAUNCH_FAILED": "The run failed to start.",
        "CLEANUP_BLOCKED": "Execution cleanup could not be verified.",
    },
    5: {
        "STATE_CORRUPT": "A trusted record could not be verified.",
        "INTERNAL_ERROR": "The operation could not be completed.",
    },
}
_ERROR_DETAILS = {code: (status, msg) for status, codes in ERRORS.items() for code, msg in codes.items()}

ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
MODEL_RE = re.compile(
    r"(?=.{1,256}\Z)[A-Za-z0-9][A-Za-z0-9_.:/-]*"
    r"(?:\[[A-Za-z][A-Za-z0-9_-]*=[A-Za-z0-9_.:/-]+"
    r"(?:,[A-Za-z][A-Za-z0-9_-]*=[A-Za-z0-9_.:/-]+)*\])?"
)
WORKER_KINDS = ("codex", "claude", "cursor", "pi")
REVIEWER_KINDS = ("codex", "claude", "agy")
SESSION_MODES = ("fresh_only", "resume_if_supported", "require_resume")
EFFORTS = (None, "minimal", "low", "medium", "high", "xhigh", "max")
SHARED_ENV_LINKS = frozenset((".venv", "venv", "node_modules"))
SEVERITIES = ("blocker", "major", "minor", "nit")
CATEGORIES = ("security", "correctness", "architecture", "tests", "scope", "docs")
MAX_SPEC_BYTES = 1024 * 1024
DEFAULT_WORKER_TIMEOUT_SECONDS = 600
MIN_WORKER_TIMEOUT_SECONDS = 5
MAX_WORKER_TIMEOUT_SECONDS = 7200
DEFAULT_REVIEWER_TIMEOUT_SECONDS = 300

WORKER_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "blocker", "next_step"],
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked", "needs_input"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 8192},
        "blocker": {"type": "string", "maxLength": 4096},
        "next_step": {"type": "string", "maxLength": 4096},
    },
}

FINDING_FIELDS = ("id", "severity", "file", "lines", "category", "title", "evidence", "required_fix")
REVIEW_VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings", "checked_items", "residual_risks"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes", "blocked"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 8192},
        "findings": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(FINDING_FIELDS),
                "properties": {
                    **{name: {"type": "string", "minLength": 1, "maxLength": 8192} for name in FINDING_FIELDS},
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "lines": {"type": "string", "maxLength": 8192, "pattern": "^L[1-9][0-9]*(-L[1-9][0-9]*)?$"},
                    "file": {"type": "string", "minLength": 1, "maxLength": 1024},
                },
            },
        },
        "checked_items": {"type": "array", "maxItems": 128, "items": {"type": "string", "minLength": 1, "maxLength": 8192}},
        "residual_risks": {"type": "array", "maxItems": 128, "items": {"type": "string", "minLength": 1, "maxLength": 8192}},
    },
}


class TaskError(ValueError):
    """Stable service error mapped to a deterministic CLI exit code."""

    def __init__(self, code: str, task_id: str | None = None, detail: str | None = None):
        self.code = code if code in _ERROR_DETAILS else "INTERNAL_ERROR"
        self.exit_code, default_msg = _ERROR_DETAILS[self.code]
        self.task_id = task_id if self.exit_code != 3 and valid_id(task_id, "task") else None
        super().__init__(detail or default_msg)


def require(condition: bool, code: str = "STATE_CORRUPT", task_id: str | None = None, detail: str | None = None) -> None:
    if not condition:
        raise TaskError(code, task_id=task_id, detail=detail)


def result_envelope(*, data=None, error: Exception | None = None) -> dict:
    if error is not None:
        if not isinstance(error, TaskError):
            error = TaskError("INTERNAL_ERROR")
        return {
            "schema_version": 1,
            "ok": False,
            "data": None,
            "error": {"code": error.code, "message": str(error), "task_id": error.task_id},
        }
    return {"schema_version": 1, "ok": True, "data": data, "error": None}


def valid_id(value: object, kind: str) -> bool:
    return isinstance(value, str) and re.fullmatch(kind + r"-[0-9a-f]{32}", value) is not None


def require_id(value: object, kind: str) -> str:
    require(valid_id(value, kind), "INVALID_ID")
    return str(value)


def new_id(kind: str) -> str:
    require(kind in ("task", "run", "request", "notice", "alert"), "INVALID_ID")
    return f"{kind}-{uuid.uuid4().hex}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate JSON key")
        out[k] = v
    return out


def strict_json(data: bytes | str, code: str = "INVALID_JSON") -> dict:
    try:
        raw = data.decode("utf-8") if isinstance(data, bytes) else data
        require(isinstance(raw, str) and len(raw.encode("utf-8")) <= 16 * 1024 * 1024, code)
        def _finite(val: str) -> float:
            num = float(val)
            if not math.isfinite(num):
                raise ValueError("nonfinite")
            return num
        parsed = json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_float=_finite,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
        require(isinstance(parsed, dict), code)
        canonical_json(parsed)
        return parsed
    except TaskError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise TaskError(code) from None


def _text(value: object, maximum: int = 8192, *, nonblank: bool = True, code: str = "INVALID_SPEC") -> str:
    require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= maximum
            and "\x00" not in value and (not nonblank or bool(value.strip())), code)
    return str(value)


def _relative_path(value: object, *, maximum: int = 1024, code: str = "INVALID_SPEC") -> str:
    text = _text(value, maximum, code=code)
    require(not text.startswith(("/", "\\", "-")) and "\\" not in text
            and all(part not in ("", ".", "..") for part in text.split("/")), code)
    return text


def _fields(obj: object, required: tuple[str, ...], optional: tuple[str, ...] = (), *, code: str = "INVALID_SPEC") -> None:
    require(isinstance(obj, dict) and set(required) <= obj.keys() <= (set(required) | set(optional)), code)


def validate_review_verdict(value: object) -> dict:
    def check(condition: bool, field: str, rule: str) -> None:
        require(condition, "INVALID_EVIDENCE", detail=f"{field}: {rule}")

    def fields(obj, expected, field):
        check(isinstance(obj, dict), field, "must be a JSON object")
        missing = [name for name in expected if name not in obj]
        extra = [str(name) for name in obj if name not in expected]
        check(not missing and not extra, field,
              "must contain exactly these fields: " + ", ".join(expected)
              + "; missing: " + (", ".join(missing) or "none")
              + "; unexpected: " + (", ".join(extra) or "none"))

    def text(item, field, maximum=8192):
        check(isinstance(item, str), field, "must be a string")
        check(1 <= len(item) <= maximum, field, f"length must be 1..{maximum} characters")
        check(bool(item.strip()), field, "must not be blank")
        check("\x00" not in item, field, "must not contain NUL bytes")

    def items(item, field):
        check(isinstance(item, list), field, "must be an array")
        check(len(item) <= 128, field, "must contain at most 128 items")

    fields(value, REVIEW_VERDICT_SCHEMA["required"], "receipt")
    check(value["verdict"] in ("approve", "request_changes", "blocked"), "verdict",
          "must be approve, request_changes, or blocked")
    text(value["summary"], "summary")
    items(value["findings"], "findings")
    seen: set[str] = set()
    for idx, finding in enumerate(value["findings"]):
        prefix = f"findings[{idx}]"
        fields(finding, FINDING_FIELDS, prefix)
        for field in FINDING_FIELDS:
            text(finding[field], f"{prefix}.{field}", 1024 if field == "file" else 8192)
        check(ID_TOKEN.fullmatch(finding["id"]) is not None, prefix + ".id",
              "must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
        check(finding["id"] not in seen, prefix + ".id", "duplicate id; each finding id must be unique")
        seen.add(finding["id"])
        check(finding["severity"] in SEVERITIES, prefix + ".severity", "must be one of: " + ", ".join(SEVERITIES))
        check(finding["category"] in CATEGORIES, prefix + ".category", "must be one of: " + ", ".join(CATEGORIES))
        file = finding["file"]
        check(not file.startswith(("/", "\\", "-")) and "\\" not in file
              and all(part not in ("", ".", "..") for part in file.split("/")), prefix + ".file",
              "must be a relative repository path; no leading slash, backslash, dash, empty, . or .. segments")
        check(re.fullmatch(r"L[1-9][0-9]*(?:-L[1-9][0-9]*)?", finding["lines"]) is not None,
              prefix + ".lines", "must use L1 or L1-L2 format with positive line numbers")
    for key in ("checked_items", "residual_risks"):
        items(value[key], key)
        for idx, item in enumerate(value[key]):
            text(item, f"{key}[{idx}]")
    has_blocker = any(f["severity"] == "blocker" for f in value["findings"])
    check(not (value["verdict"] == "approve" and has_blocker), "verdict", "approve must not contain blocker findings")
    check(not (value["verdict"] == "request_changes" and not has_blocker), "verdict",
          "request_changes requires at least one blocker finding")
    return value


def blocker_fingerprint(tree_sha: str, verdict: dict) -> str:
    validate_review_verdict(verdict)
    items = sorted(
        (f["file"], f["category"], " ".join(f["title"].casefold().split()))
        for f in verdict["findings"] if f["severity"] == "blocker"
    )
    return sha256_bytes(canonical_json([tree_sha, items]))


@dataclass(frozen=True)
class Repository:
    root: str
    key: str


@dataclass(frozen=True)
class NormalizedSpec:
    repository: Repository
    document: dict
    canonical: bytes
    spec_sha256: str
    timeout_source: str = "default"
    reviewer_timeout_source: str | None = None

    def preview(self) -> dict:
        preview = {
            "repo": self.repository.root,
            "base_commit": self.document["base_ref"],
            "spec_sha256": self.spec_sha256,
            "timeout_seconds": self.document["worker_contract"]["timeout_seconds"],
            "timeout_source": self.timeout_source,
        }
        if self.document.get("reviewer"):
            preview.update(
                reviewer_timeout_seconds=self.document["reviewer"]["timeout_seconds"],
                reviewer_timeout_source=self.reviewer_timeout_source,
            )
        return preview


def _git_quick(repo: Path, *args: str) -> str:
    from ato.worktree import git_text
    return git_text(repo, *args)


def normalize_repository(value: str | Path) -> Repository:
    try:
        selected = Path(value).expanduser().resolve(strict=True)
        require(selected.is_dir(), "INVALID_REPO")
        lines = _git_quick(selected, "rev-parse", "--path-format=absolute",
                           "--show-toplevel", "--git-common-dir").splitlines()
        require(len(lines) == 2, "INVALID_REPO")
        top, common = Path(lines[0]).resolve(strict=True), Path(lines[1]).resolve(strict=True)
        require(top == selected, "INVALID_REPO")
        # Resolve primary worktree root so linked worktrees map to the same project.
        wt_out = _git_quick(selected, "worktree", "list", "--porcelain")
        primary = top
        for line in wt_out.splitlines():
            if line.startswith("worktree "):
                primary = Path(line.split(" ", 1)[1]).resolve(strict=True)
                break
        home = config.ato_home()
        require(not (primary == home or primary.is_relative_to(home) or home.is_relative_to(primary)), "INVALID_REPO")
        return Repository(root=str(primary), key=str(common))
    except TaskError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TaskError("INVALID_REPO") from None


def _normalize_worker_contract(raw: object) -> dict:
    _fields(
        raw,
        ("objective", "scope", "authorization", "acceptance"),
        ("agent", "model", "sandbox", "permissions", "max_attempts", "timeout_seconds", "memory"),
        code="INVALID_SPEC",
    )
    assert isinstance(raw, dict)
    contract = {
        "objective": _text(raw["objective"], 32768),
        "scope": _text(raw["scope"], 16384),
        "authorization": _text(raw["authorization"], 16384),
    }
    agent = raw.get("agent")
    if agent is not None:
        _fields(agent, ("kind",), ("model",), code="INVALID_SPEC")
        require(agent["kind"] in WORKER_KINDS, "INVALID_SPEC")
        norm_agent = {"kind": agent["kind"]}
        if "model" in agent and agent["model"] is not None:
            require(isinstance(agent["model"], str) and MODEL_RE.fullmatch(agent["model"]) is not None, "INVALID_SPEC")
            norm_agent["model"] = agent["model"]
        contract["agent"] = norm_agent
    else:
        contract["agent"] = {"kind": "codex"}
    if "model" in raw and raw["model"] is not None:
        require(isinstance(raw["model"], str) and MODEL_RE.fullmatch(raw["model"]) is not None, "INVALID_SPEC")
        contract["model"] = raw["model"]
    sandbox = raw.get("sandbox", "workspace-write")
    require(sandbox in ("workspace-write", "read-only"), "INVALID_SPEC")
    contract["sandbox"] = sandbox
    if "permissions" in raw and raw["permissions"] is not None:
        perms = raw["permissions"]
        _fields(perms, (), ("network_access", "writable_roots"), code="INVALID_SPEC")
        norm_perms = {}
        if "network_access" in perms:
            require(type(perms["network_access"]) is bool, "INVALID_SPEC")
            norm_perms["network_access"] = perms["network_access"]
        if "writable_roots" in perms:
            roots = perms["writable_roots"]
            require(isinstance(roots, list) and all(isinstance(r, str) and os.path.isabs(r) for r in roots),
                    "INVALID_SPEC")
            norm_perms["writable_roots"] = roots
        contract["permissions"] = norm_perms
    max_attempts = raw.get("max_attempts", 3)
    require(type(max_attempts) is int and 1 <= max_attempts <= 10, "INVALID_SPEC")
    contract["max_attempts"] = max_attempts
    timeout = raw.get("timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)
    require(type(timeout) is int and MIN_WORKER_TIMEOUT_SECONDS <= timeout <= MAX_WORKER_TIMEOUT_SECONDS, "INVALID_SPEC")
    contract["timeout_seconds"] = timeout

    acc = raw["acceptance"]
    require(isinstance(acc, list) and 1 <= len(acc) <= 64, "INVALID_SPEC")
    seen: set[str] = set()
    criteria = []
    for item in acc:
        _fields(item, ("id", "kind", "description"), ("checks",), code="INVALID_SPEC")
        cid = item["id"]
        require(isinstance(cid, str) and ID_TOKEN.fullmatch(cid) is not None and cid not in seen, "INVALID_SPEC")
        seen.add(cid)
        kind = item["kind"]
        require(kind in ("command", "manual"), "INVALID_SPEC")
        entry: dict = {"id": cid, "kind": kind, "description": _text(item["description"], 4096)}
        if kind == "command":
            checks = item.get("checks")
            require(isinstance(checks, list) and 1 <= len(checks) <= 32, "INVALID_SPEC")
            norm_checks = []
            for argv in checks:
                require(isinstance(argv, list) and 1 <= len(argv) <= 128
                        and all(isinstance(tok, str) and tok != "" and "\x00" not in tok for tok in argv),
                        "INVALID_SPEC")
                norm_checks.append(list(argv))
            entry["checks"] = norm_checks
        else:
            require("checks" not in item or not item["checks"], "INVALID_SPEC")
        criteria.append(entry)
    contract["acceptance"] = criteria
    return contract


def _normalize_reviewer(raw: object, repo: Path, base: str) -> dict | None:
    if raw is None:
        return None
    _fields(raw, ("agent",), ("allowed_kinds", "session_mode", "max_review_rounds",
                              "timeout_seconds", "checklist", "design_docs"), code="INVALID_SPEC")
    assert isinstance(raw, dict)
    agent = raw["agent"]
    _fields(agent, ("kind",), ("model", "effort"), code="INVALID_SPEC")
    require(agent["kind"] in REVIEWER_KINDS, "INVALID_SPEC")
    norm_agent: dict = {"kind": agent["kind"]}
    if "model" in agent and agent["model"] is not None:
        require(isinstance(agent["model"], str) and MODEL_RE.fullmatch(agent["model"]) is not None, "INVALID_SPEC")
        norm_agent["model"] = agent["model"]
    if "effort" in agent and agent["effort"] is not None:
        require(agent["effort"] in EFFORTS[1:], "INVALID_SPEC")
        norm_agent["effort"] = agent["effort"]
    allowed = raw.get("allowed_kinds", [agent["kind"]])
    require(isinstance(allowed, list) and all(k in REVIEWER_KINDS for k in allowed)
            and len(allowed) == len(set(allowed)) and agent["kind"] in allowed, "INVALID_SPEC")
    session_mode = raw.get("session_mode", "fresh_only")
    require(session_mode in SESSION_MODES, "INVALID_SPEC")
    rounds = raw.get("max_review_rounds", 1)
    require(type(rounds) is int and 1 <= rounds <= 20, "INVALID_SPEC")
    timeout = raw.get("timeout_seconds", DEFAULT_REVIEWER_TIMEOUT_SECONDS)
    require(type(timeout) is int and 5 <= timeout <= 3600, "INVALID_SPEC")
    checklist = raw.get("checklist", [])
    require(isinstance(checklist, list) and len(checklist) <= 128, "INVALID_SPEC")
    checklist = [_text(c, 4096) for c in checklist]
    design_docs = raw.get("design_docs", [])
    require(isinstance(design_docs, list) and len(design_docs) <= 128, "INVALID_SPEC")
    resolved_docs = []
    from ato.worktree import git_run
    for doc_name in design_docs:
        rel = _relative_path(doc_name)
        out, code = git_run(repo, "rev-parse", "--verify", "--quiet", f"{base}:{rel}", allowed=(0, 1))
        require(code == 0, "INVALID_SPEC")
        blob = git_run(repo, "cat-file", "blob", out.decode("ascii").strip())[0]
        require(len(blob) <= MAX_SPEC_BYTES, "INVALID_SPEC")
        resolved_docs.append({"path": rel, "sha256": sha256_bytes(blob)})
    return {
        "agent": norm_agent,
        "allowed_kinds": list(allowed),
        "session_mode": session_mode,
        "max_review_rounds": rounds,
        "timeout_seconds": timeout,
        "checklist": checklist,
        "design_docs": resolved_docs,
    }


def _normalize_guardrails(raw: object) -> dict:
    _fields(raw, (), ("protected_paths", "forbid_delete_paths"), code="INVALID_SPEC")
    assert isinstance(raw, dict)
    out: dict[str, list[str]] = {}
    for key in ("protected_paths", "forbid_delete_paths"):
        paths = raw.get(key, [])
        require(isinstance(paths, list) and len(paths) <= 128, "INVALID_SPEC")
        cleaned = []
        for item in paths:
            require(isinstance(item, str) and item.strip() and all(ord(c) >= 32 and ord(c) != 127 for c in item),
                    "INVALID_SPEC")
            rel = _relative_path(item.removesuffix("/"))
            require(".git" not in rel.split("/") and rel != ".codex/tasks"
                    and not rel.startswith(".codex/tasks/"), "INVALID_SPEC")
            cleaned.append(rel)
        require(len(set(cleaned)) == len(cleaned), "INVALID_SPEC")
        out[key] = sorted(cleaned)
    return out


def read_regular_file(path_str: str | Path, *, code: str = "INVALID_SPEC", limit: int = MAX_SPEC_BYTES) -> tuple[Path, bytes]:
    try:
        path = Path(path_str).expanduser().resolve(strict=True)
        require(path.is_file() and path.stat().st_size <= limit, code)
        return path, path.read_bytes()
    except TaskError:
        raise
    except OSError:
        raise TaskError(code) from None


def normalize_spec(repo: str | Path, spec_file: str | Path) -> NormalizedSpec:
    path, data = read_regular_file(spec_file, code="INVALID_SPEC")
    raw = strict_json(data, "INVALID_JSON")
    require("schema_version" in raw and type(raw["schema_version"]) is int, "INVALID_SPEC")
    require(raw["schema_version"] == 1, "UNSUPPORTED_VERSION")
    _fields(raw, ("schema_version", "repo", "base_ref", "worker_contract"),
            ("reviewer", "worktree", "guardrails"), code="INVALID_SPEC")
    repository = normalize_repository(repo)
    spec_repo_raw = _text(raw["repo"], 4096)
    spec_repo = normalize_repository(path.parent / spec_repo_raw)
    require(spec_repo == repository, "INVALID_REPO")
    root = Path(repository.root)
    selector = _text(raw["base_ref"], 255)
    require(not selector.startswith("-") and not any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "$`\\;&|<>()" for c in selector), "INVALID_SPEC")
    try:
        base = _git_quick(root, "rev-parse", "--verify", "--end-of-options", selector + "^{commit}")
        require(COMMIT_RE.fullmatch(base) is not None, "INVALID_SPEC")
    except (ValueError, OSError, subprocess.SubprocessError):
        raise TaskError("INVALID_SPEC") from None

    worktree_raw = raw.get("worktree") or {}
    _fields(worktree_raw, (), ("shared_env_links",), code="INVALID_SPEC")
    links = worktree_raw.get("shared_env_links", [])
    require(isinstance(links, list) and all(name in SHARED_ENV_LINKS for name in links)
            and len(links) == len(set(links)), "INVALID_SPEC")

    doc: dict = {
        "schema_version": 1,
        "repo": repository.root,
        "repo_key": repository.key,
        "base_ref": base,
        "worker_contract": _normalize_worker_contract(raw["worker_contract"]),
        "reviewer": _normalize_reviewer(raw.get("reviewer"), root, base),
        "worktree": {"shared_env_links": list(links)},
    }
    if raw.get("guardrails") is not None:
        doc["guardrails"] = _normalize_guardrails(raw["guardrails"])
    canonical = canonical_json(doc)
    require(len(canonical) <= MAX_SPEC_BYTES, "INVALID_SPEC")
    return NormalizedSpec(
        repository=repository, document=doc, canonical=canonical, spec_sha256=sha256_bytes(canonical),
        timeout_source="spec" if "timeout_seconds" in raw["worker_contract"] else "default",
        reviewer_timeout_source=(
            "spec" if "timeout_seconds" in raw["reviewer"] else "default"
        ) if doc["reviewer"] is not None else None,
    )
