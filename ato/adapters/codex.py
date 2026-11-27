"""Codex CLI worker and read-only reviewer adapter (preserves P0-3 workspace-write defaults)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ato import config
from ato.adapters.base import (
    ReviewRequest, ReviewResult, WorkerRequest, WorkerResult,
    has_secret, run_bounded_subprocess, secret_values, validate_worker_result,
)
from ato.spec import REVIEW_VERDICT_SCHEMA, WORKER_RESULT_SCHEMA, strict_json, validate_review_verdict


def build_worker_argv(executable: str, req: WorkerRequest, schema_path: Path, output_path: Path) -> list[str]:
    """Preserve explicit controller overrides while keeping sandbox/approval/model defaults (P0-3)."""
    controller = list(req.controller_args)
    has_sandbox = False
    has_approval = False
    has_model = False
    idx = 0
    while idx < len(controller):
        tok = controller[idx]
        if tok in ("--sandbox", "-s", "--dangerously-bypass-approvals-and-sandbox") or tok.startswith(("--sandbox=", "-s=")):
            has_sandbox = True
        elif tok in ("--model", "-m") or tok.startswith(("--model=", "-m=")):
            has_model = True
        elif tok == "-c" and idx + 1 < len(controller):
            setting = controller[idx + 1]
            if setting.startswith("sandbox_mode="):
                has_sandbox = True
            elif setting.startswith("approval_policy="):
                has_approval = True
            elif setting.startswith("model="):
                has_model = True
            idx += 1
        idx += 1

    defaults: list[str] = []
    if not has_sandbox:
        defaults.extend(["--sandbox", req.sandbox])
        perms = req.permissions or {}
        if req.sandbox == "workspace-write":
            if "network_access" in perms:
                defaults.extend(["-c", f"sandbox_workspace_write.network_access={json.dumps(perms['network_access'])}"])
            if perms.get("writable_roots"):
                defaults.extend(["-c", f"sandbox_workspace_write.writable_roots={json.dumps(perms['writable_roots'])}"])
    if not has_approval:
        defaults.extend(["-c", 'approval_policy="never"'])
    if not has_model and req.model:
        defaults.extend(["--model", req.model])

    return [
        executable, "exec", *defaults, *controller,
        "--json", "--output-schema", str(schema_path), "-o", str(output_path), "-",
    ]


def _clean_env(runtime: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE")}
    env["TMPDIR"] = str(runtime / "tmp")
    return env


def run_worker(req: WorkerRequest) -> WorkerResult:
    exe = config.resolve_executable("codex")
    if not exe:
        return WorkerResult(status="blocked", summary="codex CLI is not installed",
                            blocker="EXECUTABLE_UNAVAILABLE", next_step="Install codex CLI and resume.")
    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    schema_path = req.runtime_dir / "worker-schema.json"
    output_path = req.runtime_dir / "worker-result.json"
    schema_path.write_text(json.dumps(WORKER_RESULT_SCHEMA), encoding="utf-8")

    argv = build_worker_argv(exe, req, schema_path, output_path)
    rc, stdout, stderr, code = run_bounded_subprocess(
        argv, cwd=req.worktree, env=_clean_env(req.runtime_dir),
        prompt=req.prompt, timeout=req.timeout_seconds, stop_check=req.stop_check,
    )
    (req.runtime_dir / "stdout.jsonl").write_bytes(stdout)
    (req.runtime_dir / "stderr.log").write_bytes(stderr)
    if code in ("STOPPED", "TIMEOUT"):
        return WorkerResult(status="blocked", summary=f"codex worker {code.lower()}", blocker=code, raw_exit=rc)
    if b"not logged in" in stderr.lower():
        return WorkerResult(status="blocked", summary="Codex authentication required",
                            blocker="AUTH_BLOCKED", next_step="Run codex login and resume.", raw_exit=rc)
    if rc != 0 or not output_path.is_file():
        return WorkerResult(status="failed", summary=f"codex exited {rc}",
                            blocker=stderr.decode("utf-8", "replace")[-500:], raw_exit=rc)
    try:
        parsed = validate_worker_result(strict_json(output_path.read_bytes()))
        return WorkerResult(
            status=parsed["status"], summary=parsed["summary"],
            blocker=parsed["blocker"], next_step=parsed["next_step"], raw_exit=rc,
        )
    except Exception as exc:
        return WorkerResult(status="failed", summary=f"invalid worker JSON: {exc}", raw_exit=rc)


def run_review(req: ReviewRequest) -> ReviewResult:
    exe = config.resolve_executable("codex")
    if not exe:
        return ReviewResult(ok=False, code="EXECUTABLE_UNAVAILABLE")
    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    schema_path = req.runtime_dir / "review-schema.json"
    output_path = req.runtime_dir / "review-output.json"
    schema_path.write_text(json.dumps(REVIEW_VERDICT_SCHEMA), encoding="utf-8")

    resumed = req.session_mode != "fresh_only" and bool(req.resume_session_id)
    argv = [exe, "exec"]
    if resumed and req.resume_session_id:
        argv.extend(["resume", req.resume_session_id])
        argv.extend(["-c", 'sandbox_mode="read-only"'])
    else:
        argv.extend(["--sandbox", "read-only"])
    argv.extend(["-c", 'approval_policy="never"', "--json",
                 "--output-schema", str(schema_path), "-o", str(output_path)])
    if req.model:
        argv.extend(["--model", req.model])
    if req.effort:
        argv.extend(["-c", f"model_reasoning_effort={json.dumps(req.effort)}"])
    argv.append("-")

    secrets = secret_values()
    rc, stdout, stderr, code = run_bounded_subprocess(
        argv, cwd=req.worktree, env=_clean_env(req.runtime_dir),
        prompt=req.prompt, timeout=req.timeout_seconds, stop_check=req.stop_check,
    )
    if code != "OK":
        if b"not logged in" in stderr.lower():
            return ReviewResult(ok=False, code="AUTH_BLOCKED")
        return ReviewResult(ok=False, code=code)
    try:
        events = [strict_json(line) for line in stdout.splitlines() if line.strip()]
        started = [e.get("thread_id") for e in events if e.get("type") == "thread.started"]
        sid = started[0] if len(started) == 1 and isinstance(started[0], str) else None
        if resumed and sid != req.resume_session_id:
            return ReviewResult(ok=False, code="SESSION_MISMATCH")
        verdict = validate_review_verdict(strict_json(output_path.read_bytes()))
        if has_secret(verdict, secrets):
            return ReviewResult(ok=False, code="SECRET_IN_REVIEW")
        return ReviewResult(ok=True, code="OK", verdict=verdict, session_id=sid)
    except Exception:
        return ReviewResult(ok=False, code="INVALID_PROTOCOL")
