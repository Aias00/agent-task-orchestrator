"""AGY read-only reviewer adapter under macOS sandbox-exec."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ato import config
from ato.adapters.base import (
    SANDBOX_EXEC, ReviewRequest, ReviewResult,
    record_review_output, review_environment, has_secret, readonly_sandbox_profile, run_bounded_subprocess, safe_review_detail, secret_values,
)
from ato.spec import REVIEW_VERDICT_SCHEMA, strict_json, validate_review_verdict


def run_review(req: ReviewRequest) -> ReviewResult:
    exe = config.resolve_executable("agy")
    if not exe:
        return ReviewResult(ok=False, code="EXECUTABLE_UNAVAILABLE")
    if not SANDBOX_EXEC.is_file():
        return ReviewResult(ok=False, code="UNSUPPORTED_CAPABILITY")

    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    schema_path = req.runtime_dir / "review-schema.json"
    schema_path.write_text(json.dumps(REVIEW_VERDICT_SCHEMA), encoding="utf-8")

    persistent = req.session_mode != "fresh_only"
    writable = (Path.home() / ".gemini/antigravity-cli",) if persistent else ()
    profile = req.runtime_dir / "review.sb"
    profile.write_text(
        readonly_sandbox_profile(req.runtime_dir, req.worktree, writable_native=writable),
        encoding="utf-8",
    )
    argv = [
        str(SANDBOX_EXEC), "-f", str(profile), exe,
        "-p", req.prompt, "--output-format", "json",
        "--json-schema", str(schema_path), "--disable-slash-commands",
        "--print-timeout", f"{req.timeout_seconds}s",
    ]
    if persistent and req.resume_session_id:
        argv.extend(["--conversation", req.resume_session_id])
    if req.model:
        argv.extend(["--model", req.model])
    if req.effort:
        argv.extend(["--effort", req.effort])

    env = dict(os.environ, TMPDIR=str(req.runtime_dir / "tmp"))
    secrets = secret_values()
    try:
        rc, stdout, stderr, code = run_bounded_subprocess(
            argv, cwd=req.worktree, env=review_environment(req, env), timeout=req.timeout_seconds, stop_check=req.stop_check, on_child=req.on_child,
        )
    except OSError as exc:
        if getattr(exc, "errno", None) == 7:  # E2BIG
            return ReviewResult(ok=False, code="REVIEW_PROMPT_TOO_LARGE")
        return ReviewResult(ok=False, code="PROCESS_ERROR")

    record_review_output(req, stdout, stderr)
    if code != "OK":
        if b"not logged in" in stderr.lower():
            return ReviewResult(ok=False, code="AUTH_BLOCKED")
        return ReviewResult(ok=False, code=code)
    sid = None
    try:
        envelope = strict_json(stdout)
        if envelope.get("status") != "SUCCESS":
            return ReviewResult(ok=False, code="INVALID_PROTOCOL", detail="envelope.status must be SUCCESS.")
        sid = envelope.get("conversation_id")
        if persistent and req.resume_session_id and sid != req.resume_session_id:
            return ReviewResult(ok=False, code="SESSION_MISMATCH")
        verdict = validate_review_verdict(envelope.get("structured_output"))
        if has_secret(verdict, secrets):
            return ReviewResult(ok=False, code="SECRET_IN_REVIEW")
        return ReviewResult(ok=True, code="OK", verdict=verdict, session_id=sid)
    except Exception as exc:
        return ReviewResult(ok=False, code="INVALID_PROTOCOL", session_id=sid,
                            detail=safe_review_detail(exc, secrets))
