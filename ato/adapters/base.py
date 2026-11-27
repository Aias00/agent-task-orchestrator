"""Shared adapter contracts, macOS sandbox profiles, result validation, and doctor."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from ato import config, process
from ato.process import terminate_popen
from ato.spec import TaskError, WORKER_RESULT_SCHEMA, require
from ato.store import Store

SENSITIVE_PATHS = (".git", ".codex", ".cursor", ".ato", ".pi", ".aws", ".ssh", ".env")
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class WorkerRequest:
    task_id: str
    kind: str
    worktree: Path
    runtime_dir: Path
    prompt: str
    sandbox: str = "workspace-write"
    model: str | None = None
    permissions: dict | None = None
    controller_args: tuple[str, ...] = ()
    timeout_seconds: int = 600
    stop_check: object = None
    on_child: object = None


@dataclass(frozen=True)
class WorkerResult:
    status: str  # completed | blocked | needs_input | failed
    summary: str
    blocker: str = ""
    next_step: str = ""
    raw_exit: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewRequest:
    task_id: str
    kind: str
    worktree: Path
    runtime_dir: Path
    prompt: str
    model: str | None = None
    effort: str | None = None
    session_mode: str = "fresh_only"
    resume_session_id: str | None = None
    timeout_seconds: int = 300
    stop_check: object = None
    on_child: object = None
    store: Store | None = None
    run_id: str | None = None
    round_num: int = 1


@dataclass(frozen=True)
class ReviewResult:
    ok: bool
    code: str
    verdict: dict | None = None
    session_id: str | None = None
    metadata: dict = field(default_factory=dict)
    detail: str = ""


def safe_review_detail(exc: Exception, secrets: tuple[str, ...]) -> str:
    if isinstance(exc, TaskError) and exc.code != "INVALID_EVIDENCE":
        return "receipt: must be valid UTF-8 JSON with one object, no duplicate keys or nonfinite numbers."
    detail = str(exc) or "Receipt must be a strict JSON object matching REVIEW_VERDICT_SCHEMA."
    for secret in sorted(secrets, key=len, reverse=True):
        for form in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
            detail = detail.replace(form, "[redacted]")
    return detail[:8192]


def validate_worker_result(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("worker result must be a JSON object")
    required = set(WORKER_RESULT_SCHEMA["required"])
    if set(value) != required:
        raise ValueError(f"worker result fields mismatch: {sorted(value)}")
    if value["status"] not in ("completed", "blocked", "needs_input"):
        raise ValueError("invalid worker status")
    for key in required:
        if not isinstance(value[key], str):
            raise ValueError(f"worker result field {key} must be string")
        if len(value[key]) > WORKER_RESULT_SCHEMA["properties"][key].get("maxLength", float("inf")):
            raise ValueError(f"worker result field {key} is too long")
    if not value["summary"].strip():
        raise ValueError("worker summary must be non-empty")
    return value


def secret_values(env: dict[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if env is None else env
    return tuple(
        str(val) for k, val in source.items()
        if val and len(str(val)) >= 8 and re.search(r"(secret|token|password|credential|api.?key|actor)", k, re.I)
    )


def has_secret(value: object, secrets: tuple[str, ...]) -> bool:
    if not secrets:
        return False
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    return any(s in encoded for s in secrets)


def worker_sandbox_profile(repo: Path, runtime: Path, mode: str, *, writable_native: tuple[Path, ...] = ()) -> str:
    q = lambda p: json.dumps(str(Path(p).resolve()))
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* (literal "/dev/null") (subpath {q(runtime)}))',
    ]
    for path in writable_native:
        rules.append(f"(allow file-write* (subpath {q(path)}))")
    if mode == "workspace-write":
        exclusions = " ".join(f"(require-not (subpath {q(repo / name)}))" for name in SENSITIVE_PATHS)
        pattern = "^" + re.escape(str(repo.resolve())) + "/([^/]+/)*(" + "|".join(map(re.escape, SENSITIVE_PATHS)) + ")(/|$)"
        rules.append(
            f"(allow file-write* (require-all (subpath {q(repo)}) {exclusions} "
            f"(require-not (regex {json.dumps(pattern)}))))"
        )
    return "\n".join(rules) + "\n"


def readonly_sandbox_profile(runtime: Path, workspace: Path, *, writable_native: tuple[Path, ...] = ()) -> str:
    q = lambda p: json.dumps(str(Path(p).resolve()))
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null"))',
        f"(allow file-write* (subpath {q(runtime)}))",
    ]
    for path in writable_native:
        rules.append(f"(allow file-write* (subpath {q(path)}))")
    rules.append(f"(deny file-write* (subpath {q(workspace)}))")
    return "\n".join(rules) + "\n"


def run_bounded_subprocess(argv: list[str], *, cwd: Path, env: dict[str, str],
                           prompt: str | None = None, timeout: int = 300,
                           stop_check=None, on_child: object = None) -> tuple[int, bytes, bytes, str]:
    """Poll an isolated child; sweep its group before reaping on every exit path."""
    with tempfile.TemporaryFile() as in_fh, tempfile.TemporaryFile() as out_fh, tempfile.TemporaryFile() as err_fh:
        if prompt is not None:
            in_fh.write(prompt.encode("utf-8"))
            in_fh.flush()
            in_fh.seek(0)
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=in_fh if prompt is not None else subprocess.DEVNULL,
            stdout=out_fh, stderr=err_fh, start_new_session=True,
        )
        code = "OK"
        try:
            if on_child is not None:
                on_child(proc.pid, process.identity(proc.pid))
            deadline = time.monotonic() + timeout
            while True:
                if stop_check is not None and stop_check():
                    code = "STOPPED"
                    break
                if process.probe_exited(proc):
                    break
                if time.monotonic() >= deadline:
                    code = "TIMEOUT"
                    break
                time.sleep(0.05)
        finally:
            try:
                terminate_popen(proc)
            except (OSError, subprocess.SubprocessError):
                raise TaskError("CLEANUP_BLOCKED") from None
            if on_child is not None:
                on_child(None, None)
        out_fh.seek(0)
        err_fh.seek(0)
        stdout = out_fh.read(MAX_OUTPUT_BYTES)
        stderr = err_fh.read(MAX_OUTPUT_BYTES)
        if code != "OK":
            return -1, stdout, stderr, code
        return proc.returncode, stdout, stderr, "OK" if proc.returncode == 0 else "PROCESS_ERROR"


def get_worker_runner(kind: str):
    if kind == "codex":
        from ato.adapters.codex import run_worker
        return run_worker
    if kind == "claude":
        from ato.adapters.claude import run_worker
        return run_worker
    if kind == "cursor":
        from ato.adapters.cursor import run_worker
        return run_worker
    if kind == "pi":
        from ato.adapters.pi import run_worker
        return run_worker
    raise TaskError("UNSUPPORTED_CAPABILITY", detail=f"Unknown worker kind: {kind}")


def get_reviewer_runner(kind: str):
    if kind == "codex":
        from ato.adapters.codex import run_review
        return run_review
    if kind == "codex_acpx":
        from ato.adapters.acpx import run_review
        return run_review
    if kind == "claude":
        from ato.adapters.claude import run_review
        return run_review
    if kind == "agy":
        from ato.adapters.agy import run_review
        return run_review
    raise TaskError("UNSUPPORTED_CAPABILITY", detail=f"Unknown reviewer kind: {kind}")


def doctor_all(*, probe_versions: bool = False) -> dict:
    """Inspect local availability for codex, claude, cursor, agy, and pi."""
    def _probe(kind: str) -> dict:
        exe = config.resolve_executable(kind)
        info: dict = {"kind": kind, "available": bool(exe), "executable": exe, "version": None}
        if exe and probe_versions:
            try:
                res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
                if res.returncode == 0:
                    info["version"] = (res.stdout.strip().splitlines() or [""])[0][:120]
            except (OSError, subprocess.SubprocessError):
                pass
        return info

    from ato.adapters.acpx import host_configuration

    return {
        "worker": {
            "codex": _probe("codex"),
            "claude": _probe("claude"),
            "cursor": _probe("cursor"),
            "pi": _probe("pi"),
        },
        "reviewer_kinds": ["codex", "claude", "agy", "codex_acpx"],
        "reviewers": {**{k: _probe(k) for k in ("codex", "claude", "agy")},
                      "codex_acpx": {"kind": "codex_acpx", "available": host_configuration() is not None,
                                     "executable": None, "version": None}},
        "sandbox_available": SANDBOX_EXEC.is_file(),
    }
