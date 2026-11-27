"""Lightweight Pi CLI worker adapter (zero third-party dependencies)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from ato import config
from ato.adapters.base import (
    SANDBOX_EXEC,
    WorkerRequest,
    WorkerResult,
    run_bounded_subprocess,
    validate_worker_result,
    worker_sandbox_profile,
)
from ato.spec import WORKER_RESULT_SCHEMA


def _path_guard_mjs() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "pi-path-guard.mjs"
    if candidate.is_file():
        return candidate
    shared = Path(sys.prefix) / "share/ato" / "pi-path-guard.mjs"
    if shared.is_file():
        return shared
    return candidate


def _decode_pi_cli(stdout: bytes) -> tuple[dict, dict]:
    records: list[dict] = []
    for line in stdout.splitlines():
        if line.strip():
            item = json.loads(line)
            if isinstance(item, dict):
                records.append(item)
    if not any(r.get("type") in ("agent_end", "agent_settled") for r in records):
        raise ValueError("Pi stream has no terminal lifecycle record")
    messages = [
        r["message"]
        for r in records
        if r.get("type") == "message_end"
        and isinstance(r.get("message"), dict)
        and r["message"].get("role") == "assistant"
    ]
    if not messages or messages[-1].get("stopReason") != "stop":
        raise ValueError("Pi final assistant response missing or truncated")
    msg = messages[-1]
    text = "".join(
        c.get("text", "")
        for c in msg.get("content", [])
        if isinstance(c, dict) and c.get("type") == "text"
    )
    session = next((r for r in records if r.get("type") == "session"), {})
    return json.loads(text), {"session_id": session.get("id"), "usage": msg.get("usage")}


def run_worker(req: WorkerRequest) -> WorkerResult:
    exe = config.resolve_executable("pi")
    if not exe:
        return WorkerResult(
            status="blocked",
            summary="pi CLI is not installed",
            blocker="EXECUTABLE_UNAVAILABLE",
        )
    if not SANDBOX_EXEC.is_file():
        return WorkerResult(
            status="blocked",
            summary="macOS sandbox-exec unavailable",
            blocker="UNSUPPORTED_CAPABILITY",
        )

    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    profile = req.runtime_dir / "sandbox.sb"
    profile.write_text(
        worker_sandbox_profile(
            req.worktree,
            req.runtime_dir,
            req.sandbox,
            writable_native=(config.pi_agent_dir(),),
        ),
        encoding="utf-8",
    )
    guard = _path_guard_mjs()
    tools = "read,grep,find,ls" if req.sandbox == "read-only" else "read,grep,find,ls,edit,write"
    argv = [
        str(SANDBOX_EXEC),
        "-f",
        str(profile),
        exe,
        "--mode",
        "json",
        "--print",
        "--tools",
        tools,
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-mcp",
        "--no-context-files",
        "--no-approve",
        "--offline",
        "--extension",
        str(guard),
        "--session",
        str(req.runtime_dir / "session.jsonl"),
    ]
    if req.model:
        argv.extend(["--model", req.model])
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE")
    }
    env.update(
        TMPDIR=str(req.runtime_dir / "tmp"),
        JITI_CACHE=str(req.runtime_dir / "jiti"),
        PI_TELEMETRY="0",
        CTASK_AGENT_REPO=str(req.worktree),
        CTASK_AGENT_SANDBOX=req.sandbox,
    )
    prompt = (
        req.prompt
        + "\nFinal response must be one JSON object matching this schema:\n"
        + json.dumps(WORKER_RESULT_SCHEMA)
    )
    rc, stdout, stderr, code = run_bounded_subprocess(
        argv,
        cwd=req.worktree,
        env=env,
        prompt=prompt,
        timeout=req.timeout_seconds,
        stop_check=req.stop_check,
    )
    (req.runtime_dir / "stdout.jsonl").write_bytes(stdout)
    (req.runtime_dir / "stderr.log").write_bytes(stderr)
    if code in ("STOPPED", "TIMEOUT"):
        return WorkerResult(status="blocked", summary=f"pi worker {code.lower()}", blocker=code, raw_exit=rc)
    if b"No API key found" in stderr:
        return WorkerResult(status="blocked", summary="Pi authentication required", blocker="AUTH_BLOCKED", raw_exit=rc)
    if rc != 0:
        return WorkerResult(
            status="failed",
            summary=f"pi exited {rc}",
            blocker=stderr.decode("utf-8", "replace")[-500:],
            raw_exit=rc,
        )
    try:
        raw_val, meta = _decode_pi_cli(stdout)
        parsed = validate_worker_result(raw_val)
        return WorkerResult(
            status=parsed["status"],
            summary=parsed["summary"],
            blocker=parsed["blocker"],
            next_step=parsed["next_step"],
            raw_exit=rc,
            metadata=meta,
        )
    except Exception as exc:
        return WorkerResult(status="failed", summary=f"invalid pi result: {exc}", raw_exit=rc)
