"""Cursor CLI worker with strict results and an outer macOS write sandbox."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from ato import config
from ato.adapters.base import (
    SANDBOX_EXEC, WorkerRequest, WorkerResult, has_secret, readonly_sandbox_profile,
    run_bounded_subprocess, secret_values, validate_worker_result, worker_sandbox_profile,
)
from ato.spec import MODEL_RE, WORKER_RESULT_SCHEMA, strict_json


def _result_object(value: object) -> dict:
    if isinstance(value, str):
        text = value.strip()
        fence = re.fullmatch(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
        value = strict_json(fence.group(1) if fence else text)
    return validate_worker_result(value)


def _decode_stream(stdout: bytes) -> tuple[object, bool, str]:
    """Use the last result or assistant message, never tool output or an earlier answer."""
    text = stdout.decode("utf-8").strip()
    if text.startswith("```json"):
        return _result_object(text), False, ""
    try:
        whole = strict_json(text)
    except ValueError:
        whole = None
    if whole is not None and "type" not in whole:
        return whole, False, ""

    assistant = None
    terminal = None
    errors = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = strict_json(line)
        kind = row.get("type")
        if kind == "assistant":
            message = row.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                assistant = "".join(
                    part["text"] for part in content
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                )
            else:
                assistant = content
        elif kind == "result":
            terminal = row
        if kind == "error" or row.get("is_error") or (
            kind == "result" and str(row.get("subtype", "")).startswith("error")
        ):
            errors.append(json.dumps(row))
    output = terminal.get("result", assistant) if terminal is not None else assistant
    return output, bool(errors), "\n".join(errors)


def _redact(data: bytes, secrets: tuple[str, ...]) -> bytes:
    # Cover both literal CLI diagnostics and JSON-escaped stream content.
    for secret in sorted(secrets, key=len, reverse=True):
        for form in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
            data = data.replace(form.encode("utf-8"), b"[REDACTED]")
    return data


def run_worker(req: WorkerRequest) -> WorkerResult:
    exe = config.resolve_executable("cursor")
    if not exe:
        return WorkerResult(status="blocked", summary="Cursor CLI is not installed",
                            blocker="EXECUTABLE_UNAVAILABLE", next_step="Install cursor-agent and resume.")
    if not SANDBOX_EXEC.is_file() or not os.access(SANDBOX_EXEC, os.X_OK):
        return WorkerResult(status="blocked", summary="macOS sandbox-exec unavailable",
                            blocker="UNSUPPORTED_CAPABILITY")
    if req.sandbox not in ("read-only", "workspace-write"):
        return WorkerResult(status="blocked", summary="Unsupported Cursor sandbox policy",
                            blocker="UNSUPPORTED_CAPABILITY")
    if req.model is not None and not MODEL_RE.fullmatch(req.model):
        return WorkerResult(status="failed", summary="Invalid Cursor model", blocker="INVALID_SPEC")

    secrets = secret_values()
    profile = req.runtime_dir / "sandbox.sb"
    argv = [str(SANDBOX_EXEC), "-f", str(profile), exe,
            "-p", "--output-format", "stream-json", "--workspace", str(req.worktree),
            "--sandbox", "disabled"]
    if req.sandbox == "read-only":
        argv.extend(["--mode", "plan"])
    else:
        argv.extend(["--trust", "--force"])
    if req.model:
        argv.extend(["--model", req.model])
    if has_secret(argv, secrets):
        return WorkerResult(status="blocked", summary="Cursor arguments contain a secret",
                            blocker="SECRET_IN_ARGUMENTS")

    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    native = (Path.home() / ".cursor",)
    if req.sandbox == "read-only":
        policy = readonly_sandbox_profile(req.runtime_dir, req.worktree, writable_native=native)
    else:
        policy = worker_sandbox_profile(req.worktree, req.runtime_dir, req.sandbox, writable_native=native)
    if (req.permissions or {}).get("network_access") is False:
        policy += "(deny network*)\n"
    profile.write_text(policy, encoding="utf-8")
    env = {key: val for key, val in os.environ.items() if key not in (
        "CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE",
    )}
    env["TMPDIR"] = str(req.runtime_dir / "tmp")
    prompt = req.prompt + (
        "\nFinal response must be exactly one JSON object (or one ```json code block) "
        "matching this schema. Do not include surrounding prose:\n" + json.dumps(WORKER_RESULT_SCHEMA)
    )
    try:
        rc, stdout, stderr, code = run_bounded_subprocess(
            argv, cwd=req.worktree, env=env, prompt=prompt,
            timeout=req.timeout_seconds, stop_check=req.stop_check,
        )
    except OSError:
        return WorkerResult(status="blocked", summary="Cursor worker executable could not be launched",
                            blocker="EXECUTABLE_UNAVAILABLE")

    safe_stdout, safe_stderr = _redact(stdout, secrets), _redact(stderr, secrets)
    output = None
    stream_error = False
    diagnostic = ""
    invalid = False
    parsed = None
    try:
        output, stream_error, diagnostic = _decode_stream(stdout)
        parsed = _result_object(output)
    except (ValueError, TypeError, RecursionError):
        invalid = True
    leaked = (safe_stdout != stdout or safe_stderr != stderr
              or has_secret(output, secrets) or has_secret(parsed, secrets))
    # Suppress both streams on leakage, including unusually escaped JSON secrets.
    (req.runtime_dir / "stdout.jsonl").write_bytes(b"[REDACTED]\n" if leaked else safe_stdout)
    (req.runtime_dir / "stderr.log").write_bytes(b"[REDACTED]\n" if leaked else safe_stderr)
    if code in ("STOPPED", "TIMEOUT"):
        return WorkerResult(status="blocked", summary=f"Cursor worker {code.lower()}", blocker=code, raw_exit=rc)
    if leaked:
        return WorkerResult(status="failed", summary="Cursor output contained a secret",
                            blocker="SECRET_IN_WORKER_OUTPUT", raw_exit=rc)

    diag = (stderr.decode("utf-8", "replace") + "\n" + diagnostic).lower()
    if rc != 0:
        diag += "\n" + stdout.decode("utf-8", "replace").lower()
    if any(mark in diag for mark in (
        "not logged in", "not authenticated", "authentication required", "auth required",
        "login required", "please log in", "please login", "unauthorized", "invalid api key", "no api key",
    )):
        return WorkerResult(status="blocked", summary="Cursor authentication required", blocker="AUTH_BLOCKED",
                            next_step="Run cursor-agent login and resume.", raw_exit=rc)
    if any(mark in diag for mark in (
        "quota", "rate limit", "rate_limit", "rate-limit", "too many requests", "usage limit", "resource_exhausted",
    )):
        return WorkerResult(status="blocked", summary="Cursor provider quota or rate limit reached",
                            blocker="PROVIDER_UNAVAILABLE", next_step="Restore provider availability and resume.", raw_exit=rc)
    if "sandbox" in diag and any(mark in diag for mark in (
        "operation not permitted", "sandbox_init", "failed to initialize", "not supported",
    )):
        return WorkerResult(status="blocked", summary="Cursor outer sandbox could not be initialized",
                            blocker="UNSUPPORTED_CAPABILITY", raw_exit=rc)
    if rc != 0 or stream_error or code != "OK":
        return WorkerResult(status="failed", summary=f"Cursor worker process failed (exit {rc})",
                            blocker="PROCESS_ERROR", raw_exit=rc)
    if invalid:
        return WorkerResult(status="failed", summary="Cursor did not return valid worker JSON",
                            blocker="INVALID_PROTOCOL", raw_exit=rc)
    return WorkerResult(**parsed, raw_exit=rc)
