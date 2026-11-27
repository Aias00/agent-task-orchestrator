"""Claude CLI worker and read-only reviewer adapter under macOS sandbox-exec."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from ato import config
from ato.adapters.base import (
    SANDBOX_EXEC, ReviewRequest, ReviewResult, WorkerRequest, WorkerResult,
    has_secret, readonly_sandbox_profile, run_bounded_subprocess, safe_review_detail, secret_values,
    validate_worker_result, worker_sandbox_profile,
)
from ato.spec import REVIEW_VERDICT_SCHEMA, WORKER_RESULT_SCHEMA, strict_json, validate_review_verdict


def _claude_writable_native() -> tuple[Path, ...]:
    home = Path.home()
    return (home / ".claude", home / ".claude.json", Path("/tmp") / f"claude-{os.getuid()}")


def _decode_stream(stdout: bytes) -> tuple[dict | None, dict | None]:
    terminal = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "result":
            terminal = row
    if terminal is None:
        return None, None
    return terminal, terminal.get("structured_output") if isinstance(terminal.get("structured_output"), dict) else None


def run_worker(req: WorkerRequest) -> WorkerResult:
    exe = config.resolve_executable("claude")
    if not exe:
        return WorkerResult(status="blocked", summary="claude CLI is not installed", blocker="EXECUTABLE_UNAVAILABLE")
    if not SANDBOX_EXEC.is_file():
        return WorkerResult(status="blocked", summary="macOS sandbox-exec unavailable", blocker="UNSUPPORTED_CAPABILITY")

    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    profile = req.runtime_dir / "sandbox.sb"
    profile.write_text(
        worker_sandbox_profile(req.worktree, req.runtime_dir, req.sandbox, writable_native=_claude_writable_native()),
        encoding="utf-8",
    )
    readonly = req.sandbox == "read-only"
    tools = "Read,Glob,Grep" if readonly else "Read,Glob,Grep,Edit,Write"
    sid = str(uuid.uuid4())
    argv = [
        str(SANDBOX_EXEC), "-f", str(profile), exe,
        "--safe-mode", "--restricted", "--no-session-persistence", "--disable-slash-commands",
        "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
        "--tools", tools, "--allowedTools", tools, "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}', "--session-id", sid,
        "--json-schema", json.dumps(WORKER_RESULT_SCHEMA),
    ]
    if req.model:
        argv.extend(["--model", req.model])
    env = dict(os.environ, TMPDIR=str(req.runtime_dir / "tmp"),
               CLAUDE_CODE_TMPDIR=str(req.runtime_dir / "tmp"), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    prompt = req.prompt + "\nFinal response must match this JSON schema:\n" + json.dumps(WORKER_RESULT_SCHEMA)
    rc, stdout, stderr, code = run_bounded_subprocess(
        argv, cwd=req.worktree, env=env, prompt=prompt, timeout=req.timeout_seconds, stop_check=req.stop_check, on_child=req.on_child,
    )
    (req.runtime_dir / "stdout.jsonl").write_bytes(stdout)
    (req.runtime_dir / "stderr.log").write_bytes(stderr)
    if code in ("STOPPED", "TIMEOUT"):
        return WorkerResult(status="blocked", summary=f"claude worker {code.lower()}", blocker=code, raw_exit=rc)
    terminal, output = _decode_stream(stdout)
    diag = stderr.lower() + (json.dumps(terminal).lower().encode() if terminal else b"")
    if any(m in diag for m in (b"not logged in", b"no api key", b"authentication required")):
        return WorkerResult(status="blocked", summary="Claude authentication required", blocker="AUTH_BLOCKED", raw_exit=rc)
    if rc != 0 or terminal is None or terminal.get("is_error") or output is None:
        return WorkerResult(status="failed", summary=f"claude exited {rc}",
                            blocker=stderr.decode("utf-8", "replace")[-500:], raw_exit=rc)
    try:
        parsed = validate_worker_result(output)
        return WorkerResult(
            status=parsed["status"], summary=parsed["summary"],
            blocker=parsed["blocker"], next_step=parsed["next_step"],
            raw_exit=rc, metadata={"session_id": terminal.get("session_id"), "cost_usd": terminal.get("total_cost_usd")},
        )
    except Exception as exc:
        return WorkerResult(status="failed", summary=f"invalid claude result: {exc}", raw_exit=rc)


def run_review(req: ReviewRequest) -> ReviewResult:
    exe = config.resolve_executable("claude")
    if not exe:
        return ReviewResult(ok=False, code="EXECUTABLE_UNAVAILABLE")
    if not SANDBOX_EXEC.is_file():
        return ReviewResult(ok=False, code="UNSUPPORTED_CAPABILITY")
    if req.session_mode == "require_resume":
        return ReviewResult(ok=False, code="RESUME_NOT_QUALIFIED")

    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    profile = req.runtime_dir / "review.sb"
    profile.write_text(
        readonly_sandbox_profile(req.runtime_dir, req.worktree, writable_native=_claude_writable_native()),
        encoding="utf-8",
    )
    sid = str(uuid.uuid4())
    argv = [
        str(SANDBOX_EXEC), "-f", str(profile), exe,
        "--safe-mode", "--restricted", "--disable-slash-commands", "-p",
        "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
        "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--json-schema", json.dumps(REVIEW_VERDICT_SCHEMA),
        "--no-session-persistence", "--session-id", sid,
    ]
    if req.model:
        argv.extend(["--model", req.model])
    if req.effort:
        argv.extend(["--effort", req.effort])
    env = dict(os.environ, TMPDIR=str(req.runtime_dir / "tmp"), CLAUDE_CODE_TMPDIR=str(req.runtime_dir / "tmp"))
    secrets = secret_values()
    rc, stdout, stderr, code = run_bounded_subprocess(
        argv, cwd=req.worktree, env=env, prompt=req.prompt, timeout=req.timeout_seconds, stop_check=req.stop_check, on_child=req.on_child,
    )
    if code not in ("OK", "PROCESS_ERROR"):
        return ReviewResult(ok=False, code=code)
    terminal, output = _decode_stream(stdout)
    diag = stderr.lower() + (json.dumps(terminal).lower().encode() if terminal else b"")
    if any(m in diag for m in (b"not logged in", b"not authenticated", b"login required")):
        return ReviewResult(ok=False, code="AUTH_BLOCKED")
    if rc != 0 or (terminal is not None and terminal.get("is_error")):
        return ReviewResult(ok=False, code="PROCESS_ERROR")
    if terminal is None or output is None:
        return ReviewResult(ok=False, code="INVALID_PROTOCOL", session_id=sid,
                            detail="receipt: expected a JSON result envelope with structured_output as an object.")
    try:
        verdict = validate_review_verdict(strict_json(json.dumps(output)))
        if has_secret(verdict, secrets):
            return ReviewResult(ok=False, code="SECRET_IN_REVIEW")
        return ReviewResult(ok=True, code="OK", verdict=verdict, session_id=sid)
    except Exception as exc:
        return ReviewResult(ok=False, code="INVALID_PROTOCOL", session_id=sid,
                            detail=safe_review_detail(exc, secrets))
