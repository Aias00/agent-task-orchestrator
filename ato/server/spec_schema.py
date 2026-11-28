"""Inline task discovery schemas and MCP-only admission rules; stdlib/core only.

Normalization remains authoritative for UTF-8 byte limits, Git bindings, unique
criterion IDs, allowed reviewer membership and normalized path uniqueness.
Schema validation must not run ahead of the journal's historical replay lookup.
"""
from ato.spec import (
    DEFAULT_MAX_NO_PROGRESS_ROUNDS, DEFAULT_MAX_REVIEW_ROUNDS,
    DEFAULT_REVIEWER_TIMEOUT_SECONDS, DEFAULT_WORKER_TIMEOUT_SECONDS,
    EFFORTS, ID_TOKEN, MAX_NO_PROGRESS_ROUNDS, MAX_REVIEW_ROUNDS,
    MAX_REVIEWER_TIMEOUT_SECONDS, MAX_SPEC_BYTES, MAX_WORKER_TIMEOUT_SECONDS,
    MIN_REVIEWER_TIMEOUT_SECONDS, MIN_WORKER_TIMEOUT_SECONDS, MODEL_RE,
    REVIEWER_KINDS, SESSION_MODES, SHARED_ENV_LINKS, WORKER_KINDS,
    WORKER_SANDBOXES, require,
)
from ato.server.config import _NAME


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


def text(maximum, *, pattern=r"^(?=[\s\S]*\S)[^\u0000]+$"):
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": pattern}


def integer(minimum, maximum, **annotations):
    return {"type": "integer", "minimum": minimum, "maximum": maximum, **annotations}


def array(items, maximum, minimum=0, **annotations):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum, **annotations}


def nullable(shape):
    return {**shape, "type": [shape["type"], "null"]}


ALIAS = text(64, pattern="^" + _NAME.pattern.replace(r"\Z", "$"))
RELATIVE_PATH = text(1024, pattern=r"^(?![/\\-])(?!.*\\)(?!\.{1,2}(?:/|$))(?!.*\/\.{1,2}(?:/|$))[^\u0000/]+(?:/[^\u0000/]+)*$")
RELATIVE_PATH["description"] = "Repository-relative path; no empty, dot, dot-dot or backslash segments."
MODEL = nullable(text(256, pattern="^" + MODEL_RE.pattern.replace(r"\Z", "$") + "$"))


def task_spec_schema(*, mcp: bool):
    """REST keeps historical optional defaults; new MCP submissions require timeouts."""
    worker_timeout = integer(MIN_WORKER_TIMEOUT_SECONDS, MAX_WORKER_TIMEOUT_SECONDS,
                             description="Worker execution deadline in seconds, not an MCP network timeout.")
    reviewer_timeout = integer(MIN_REVIEWER_TIMEOUT_SECONDS, MAX_REVIEWER_TIMEOUT_SECONDS)
    if not mcp:
        worker_timeout["default"] = DEFAULT_WORKER_TIMEOUT_SECONDS
        reviewer_timeout["default"] = DEFAULT_REVIEWER_TIMEOUT_SECONDS
    criterion = schema({
        "id": text(128, pattern="^" + ID_TOKEN.pattern + "$"),
        "kind": {"type": "string", "enum": ["command", "manual"]},
        "description": text(4096),
        "checks": array(array(text(MAX_SPEC_BYTES, pattern=r"^[^\u0000]+$"), 128, 1), 32, 1),
    }, ("id", "kind", "description"))
    # Manual checks may only carry an empty/falsy value in the existing normalizer.
    criterion["properties"]["checks"] = {"anyOf": [criterion["properties"]["checks"],
                                                     {"enum": [None, False, 0, "", [], {}]}]}
    criterion["allOf"] = [{
        "if": {"properties": {"kind": {"const": "command"}}},
        "then": {"required": ["checks"], "properties": {"checks": {"type": "array", "minItems": 1}}},
        "else": {"properties": {"checks": {"enum": [None, False, 0, "", [], {}]}}},
    }]
    worker = schema({
        "objective": text(32768), "scope": text(16384), "authorization": text(16384),
        "acceptance": array(criterion, 64, 1, description="Unique criterion IDs; command checks are argv arrays; manual criteria await human acceptance."),
        "agent": {**nullable(schema({"kind": {"type": "string", "enum": list(WORKER_KINDS)},
                                     "model": MODEL}, ("kind",))), "default": {"kind": "codex"}},
        "model": {**MODEL, "description": "Legacy model fallback; agent.model takes precedence."},
        "sandbox": {"type": "string", "enum": list(WORKER_SANDBOXES), "default": "workspace-write"},
        "permissions": nullable(schema({
            "network_access": {"type": "boolean", "description": "Omit to retain the adapter/host policy."},
            "writable_roots": array({"type": "string"}, 0, description="Inline submissions cannot choose writable roots; only an empty list is accepted."),
        })),
        "max_attempts": integer(1, 10, default=3),
        "max_no_progress_rounds": integer(1, MAX_NO_PROGRESS_ROUNDS, default=DEFAULT_MAX_NO_PROGRESS_ROUNDS),
        "timeout_seconds": worker_timeout,
        "memory": {"description": "Legacy compatibility field, ignored by normalization; does not load files or alter execution.", "deprecated": True},
    }, ("objective", "scope", "authorization", "acceptance") + (("timeout_seconds",) if mcp else ()))
    reviewer = nullable(schema({
        "agent": schema({"kind": {"type": "string", "enum": list(REVIEWER_KINDS)}, "model": MODEL,
                         "effort": {"type": ["string", "null"], "enum": list(EFFORTS)}}, ("kind",)),
        "allowed_kinds": array({"type": "string", "enum": list(REVIEWER_KINDS)}, len(REVIEWER_KINDS), 1,
                               uniqueItems=True, description="Defaults to [agent.kind]; must include agent.kind."),
        "session_mode": {"type": "string", "enum": list(SESSION_MODES), "default": "fresh_only"},
        "max_review_rounds": integer(1, MAX_REVIEW_ROUNDS, default=DEFAULT_MAX_REVIEW_ROUNDS),
        "timeout_seconds": reviewer_timeout,
        "checklist": array(text(4096), 128, default=[]),
        "design_docs": array(RELATIVE_PATH, 128, default=[], description="Tracked repository blobs at base_ref, at most 1 MiB each; never arbitrary host files."),
    }, ("agent",) + (("timeout_seconds",) if mcp else ())))
    reviewer["description"] = "Omit or null to disable. codex_acpx requires fresh_only and host launcher model/effort defaults."
    reviewer["allOf"] = [{
        "if": {"type": "object", "required": ["agent"], "properties": {
            "agent": {"required": ["kind"], "properties": {"kind": {"const": "codex_acpx"}}}}},
        "then": {"properties": {"session_mode": {"const": "fresh_only"}, "agent": {
            "properties": {"model": {"type": "null"}, "effort": {"type": "null"}}}}},
    }]
    guard_path = {**RELATIVE_PATH, "maxLength": 1025,
                  "description": "Repository-relative path, optional trailing slash; no control characters, .git segments or .codex/tasks subtree."}
    # Guardrails allow a trailing slash which normalization removes.
    guard_path["pattern"] = RELATIVE_PATH["pattern"].removesuffix("$") + "/?$"
    return schema({
        "schema_version": {"type": "integer", "const": 1},
        "repo": {**ALIAS, "description": "Must equal the outer repo alias returned by ato_repositories; never a filesystem path."},
        "base_ref": text(255, pattern=r"^(?!-)[^\s\u0000-\u001f\u007f$`\\;&|<>()]+$"),
        "worker_contract": worker,
        "reviewer": reviewer,
        "worktree": nullable(schema({"shared_env_links": array(
            {"type": "string", "enum": sorted(SHARED_ENV_LINKS)}, len(SHARED_ENV_LINKS),
            uniqueItems=True, default=[])})),
        "guardrails": nullable(schema({name: array(guard_path, 128, uniqueItems=True, default=[])
                                       for name in ("protected_paths", "forbid_delete_paths")})),
    }, ("schema_version", "repo", "base_ref", "worker_contract"))


def require_mcp_timeouts(raw: dict) -> None:
    """New intents only: never coerce values or inject a hidden timeout default."""
    worker = raw.get("worker_contract")
    timeout = worker.get("timeout_seconds") if isinstance(worker, dict) else None
    require(type(timeout) is int and MIN_WORKER_TIMEOUT_SECONDS <= timeout <= MAX_WORKER_TIMEOUT_SECONDS,
            "MCP_WORKER_TIMEOUT_REQUIRED")
    reviewer = raw.get("reviewer")
    if reviewer is not None:
        timeout = reviewer.get("timeout_seconds") if isinstance(reviewer, dict) else None
        require(type(timeout) is int and MIN_REVIEWER_TIMEOUT_SECONDS <= timeout <= MAX_REVIEWER_TIMEOUT_SECONDS,
                "MCP_REVIEWER_TIMEOUT_REQUIRED")
