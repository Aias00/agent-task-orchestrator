"""Pi CLI worker, ACP Pi worker, deterministic pi-acp derivation, and slim PiCoordinator (2B)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from ato import config
from ato.adapters.base import (
    SANDBOX_EXEC, WorkerRequest, WorkerResult,
    run_bounded_subprocess, validate_worker_result, worker_sandbox_profile,
)
from ato.process import terminate_popen
from ato.spec import WORKER_RESULT_SCHEMA


def _assets_root() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "integrations/pi-acp/compatibility.json").is_file():
        return repo_root
    shared = Path(sys.prefix) / "share/ato"
    if (shared / "integrations/pi-acp/compatibility.json").is_file():
        return shared
    return repo_root


ROOT = _assets_root()
COMPAT_FILE = ROOT / "integrations/pi-acp/compatibility.json"
PI_ACP_SOURCE = ROOT / "integrations/pi-acp/node_modules/pi-acp/dist/index.js"
BACKEND = "pi-acp-0.0.34-compat-v1"
MAX_SOURCE_BYTES = 32 * 1024 * 1024


class CompatibilityError(ValueError):
    pass


@dataclass(frozen=True)
class _Transform:
    name: str
    before: str
    after: str

    def apply(self, src: str) -> str:
        if src.count(self.before) != 1:
            raise CompatibilityError(f"{BACKEND}: transform {self.name} requires one match")
        return src.replace(self.before, self.after, 1)


BASE_TRANSFORMS = (
    _Transform(
        "isolated_state",
        '  return join(homedir(), ".pi", "pi-acp");',
        '  if (!process.env.CTASK_PI_ACP_STATE_DIR) throw new Error("isolated state directory required");\n'
        "  return process.env.CTASK_PI_ACP_STATE_DIR;",
    ),
    _Transform(
        "disable_update_probe",
        "const updateNotice = buildUpdateNotice();",
        "const updateNotice = null; // No npm/network version probe in this profile.",
    ),
    _Transform(
        "prompt_nonce",
        "  async prompt(message, images = []) {\n    const expandedMessage",
        "  async prompt(message, images = [], ctaskTurn) {\n    const expandedMessage",
    ),
    _Transform(
        "queue_nonce",
        "const queued = { message: expandedMessage, images, resolve: resolve4, reject };",
        "const queued = { message: expandedMessage, images, ctaskTurn, resolve: resolve4, reject };",
    ),
    _Transform(
        "turn_initialization",
        "    this.cancelRequested = false;\n    this.inAgentLoop = false;\n    this.pendingTurn = { resolve: t.resolve, reject: t.reject };",
        "    this.cancelRequested = false;\n    this.inAgentLoop = false;\n    this.ctaskTurn = t.ctaskTurn;\n"
        "    this.ctaskNativeError = false;\n    this.ctaskNativeTerminal = null;\n    this.ctaskNativeResult = null;\n"
        "    this.pendingTurn = { resolve: t.resolve, reject: t.reject };",
    ),
    _Transform(
        "settle_native_receipt",
        "  async settleTurn() {\n    await this.publishContextUsage();",
        '  async settleTurn() {\n    if (this.ctaskNativeError) this.ctaskReceipt("error");\n'
        '    else if (this.ctaskNativeTerminal === "stop") this.ctaskReceipt("stop");\n'
        "    await this.publishContextUsage();",
    ),
    _Transform(
        "native_prompt_error",
        "    this.proc.prompt(t.message, t.images).catch((err) => {",
        "    this.proc.prompt(t.message, t.images).catch((err) => {\n"
        '      this.ctaskNativeError = true;\n      this.ctaskReceipt("error");',
    ),
    _Transform(
        "native_event_receipt",
        '  handlePiEvent(ev) {\n    const type = String(ev.type ?? "");',
        '  ctaskReceipt(reason) {\n    if (this.pendingTurn && typeof this.ctaskTurn === "string") {\n'
        '      this.emit({sessionUpdate: "session_info_update", _meta: {\n'
        "        ctaskNativeReceipt: {turn: this.ctaskTurn, stopReason: reason,\n"
        '          ...(reason === "stop" && this.ctaskNativeResult !== null ? {result: this.ctaskNativeResult} : {})}\n'
        "      }});\n    }\n  }\n  handlePiEvent(ev) {\n"
        '    if (ev.type === "message_end" && ev.message?.role === "assistant" && this.pendingTurn) {\n'
        '      const reason = ev.message.stopReason;\n'
        '      if (["error", "aborted", "cancelled"].includes(reason)) {\n        this.ctaskNativeError = true;\n      }\n'
        "      this.ctaskNativeTerminal = reason;\n      this.ctaskNativeResult = null;\n"
        '      if (reason === "stop" && Array.isArray(ev.message.content)) {\n'
        '        const parts = ev.message.content.filter(x => x?.type === "text" && typeof x.text === "string");\n'
        '        const result = parts.map(x => x.text).join("");\n'
        "        if (result.length <= 1048576) this.ctaskNativeResult = result;\n      }\n    }\n"
        '    const type = String(ev.type ?? "");',
    ),
    _Transform(
        "acp_prompt_metadata",
        "const result = await session.prompt(message, images);",
        "const result = await session.prompt(message, images, params._meta?.ctaskTurn);",
    ),
)

COORDINATOR_TRANSFORMS = (
    _Transform(
        "require_native_identity",
        'const sessionId = typeof state?.sessionId === "string" ? state.sessionId : crypto.randomUUID();',
        'if (typeof state?.sessionId !== "string" || !state.sessionId || !state.sessionFile) throw new Error("native identity required");\n'
        "    const sessionId = state.sessionId;",
    ),
    _Transform(
        "publish_native_identity",
        "    _meta: {\n        piAcp: {\n          startupInfo: preludeText || null",
        "    _meta: {\n        atoIdentity: {nativeId: state.sessionId, project: params.cwd},\n"
        "        piAcp: {\n          startupInfo: preludeText || null",
    ),
    _Transform(
        "correlated_updates",
        "        sessionId: this.sessionId,\n        update\n",
        "        sessionId: this.sessionId,\n        update: {...update, _meta: {...(update._meta ?? {}), ctaskTurn}}\n",
    ),
    _Transform(
        "capture_emit_nonce",
        "  emit(update) {\n    this.lastEmit",
        "  emit(update) {\n    const ctaskTurn = this.ctaskTurn;\n    this.lastEmit",
    ),
    _Transform(
        "disable_slash_commands",
        "function loadSlashCommands(cwd) {\n  const commands = [];",
        "function loadSlashCommands(cwd) {\n  return [];\n  const commands = [];",
    ),
)


def derive_source(source: str, *, role: str = "worker") -> str:
    """Apply pinned transforms to pi-acp dist/index.js and verify SHA-256 digests."""
    if role not in ("worker", "coordinator"):
        raise CompatibilityError("unknown pi-acp role")
    entry = json.loads(COMPAT_FILE.read_text(encoding="utf-8"))["adapter"]
    raw = source.encode("utf-8")
    if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != entry["source_sha256"]:
        raise CompatibilityError(f"{BACKEND}: source fingerprint differs")
    transforms = BASE_TRANSFORMS + (COORDINATOR_TRANSFORMS if role == "coordinator" else ())
    out = source
    for t in transforms:
        out = t.apply(out)
    if hashlib.sha256(out.encode("utf-8")).hexdigest() != entry["derived_sha256"][role]:
        raise CompatibilityError(f"{BACKEND}: output fingerprint differs")
    return out


def derive_directory(output: Path, *, role: str = "worker") -> Path:
    if not PI_ACP_SOURCE.is_file():
        raise CompatibilityError("integrations/pi-acp/node_modules/pi-acp/dist/index.js not installed")
    derived = derive_source(PI_ACP_SOURCE.read_text(encoding="utf-8"), role=role)
    entry = json.loads(COMPAT_FILE.read_text(encoding="utf-8"))["adapter"]
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    (output / "pi-acp.mjs").write_text(derived, encoding="utf-8")
    (output / "package.json").write_text(
        json.dumps({"name": entry["name"], "version": entry["version"], "type": "module"}) + "\n",
        encoding="utf-8",
    )
    nm = output / "node_modules"
    nm.mkdir(exist_ok=True, mode=0o700)
    base_nm = PI_ACP_SOURCE.parents[2]
    for dep in ("@agentclientprotocol", "cross-spawn"):
        src = base_nm / dep
        if not src.is_dir():
            src = PI_ACP_SOURCE.parents[1] / "node_modules" / dep
        if not src.is_dir():
            raise CompatibilityError(f"missing pinned pi-acp dependency: {dep}")
        link = nm / dep
        if not link.exists():
            link.symlink_to(src.resolve(), target_is_directory=True)
    return output / "pi-acp.mjs"


def _write_pi_wrapper(runtime: Path, pi_bin: str, guard_js: Path, *, sandbox: str, model: str | None) -> Path:
    tools = "read,grep,find,ls" if sandbox == "read-only" else "read,grep,find,ls,edit,write"
    wrapper = runtime / "pi-launcher.sh"
    extra_model = f' --model "{model}"' if model else ""
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{pi_bin}" --tools "{tools}" --no-extensions --no-skills --no-prompt-templates '
        f'--no-themes --no-mcp --no-context-files --no-approve --offline '
        f'--extension "{guard_js}"{extra_model} "$@"\n',
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o700)
    return wrapper


def _decode_pi_cli(stdout: bytes) -> tuple[dict, dict]:
    records = []
    for line in stdout.splitlines():
        if line.strip():
            item = json.loads(line)
            if isinstance(item, dict):
                records.append(item)
    if not any(r.get("type") in ("agent_end", "agent_settled") for r in records):
        raise ValueError("Pi stream has no terminal lifecycle record")
    messages = [
        r["message"] for r in records
        if r.get("type") == "message_end" and isinstance(r.get("message"), dict)
        and r["message"].get("role") == "assistant"
    ]
    if not messages or messages[-1].get("stopReason") != "stop":
        raise ValueError("Pi final assistant response missing or truncated")
    msg = messages[-1]
    text = "".join(c.get("text", "") for c in msg.get("content", []) if isinstance(c, dict) and c.get("type") == "text")
    session = next((r for r in records if r.get("type") == "session"), {})
    return json.loads(text), {"session_id": session.get("id"), "usage": msg.get("usage")}


def _run_pi_cli_worker(req: WorkerRequest) -> WorkerResult:
    exe = config.resolve_executable("pi")
    if not exe:
        return WorkerResult(status="blocked", summary="pi CLI is not installed", blocker="EXECUTABLE_UNAVAILABLE")
    if not SANDBOX_EXEC.is_file():
        return WorkerResult(status="blocked", summary="macOS sandbox-exec unavailable", blocker="UNSUPPORTED_CAPABILITY")

    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    profile = req.runtime_dir / "sandbox.sb"
    profile.write_text(
        worker_sandbox_profile(req.worktree, req.runtime_dir, req.sandbox, writable_native=(config.pi_agent_dir(),)),
        encoding="utf-8",
    )
    tools = "read,grep,find,ls" if req.sandbox == "read-only" else "read,grep,find,ls,edit,write"
    argv = [
        str(SANDBOX_EXEC), "-f", str(profile), exe,
        "--mode", "json", "--print", "--tools", tools,
        "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
        "--no-mcp", "--no-context-files", "--no-approve", "--offline",
        "--extension", str(ROOT / "pi-path-guard.mjs"),
        "--session", str(req.runtime_dir / "session.jsonl"),
    ]
    if req.model:
        argv.extend(["--model", req.model])
    env = dict(
        os.environ,
        TMPDIR=str(req.runtime_dir / "tmp"),
        JITI_CACHE=str(req.runtime_dir / "jiti"),
        PI_TELEMETRY="0",
        CTASK_AGENT_REPO=str(req.worktree),
        CTASK_AGENT_SANDBOX=req.sandbox,
    )
    prompt = req.prompt + "\nFinal response must be one JSON object matching this schema:\n" + json.dumps(WORKER_RESULT_SCHEMA)
    rc, stdout, stderr, code = run_bounded_subprocess(
        argv, cwd=req.worktree, env=env, prompt=prompt, timeout=req.timeout_seconds, stop_check=req.stop_check,
    )
    (req.runtime_dir / "stdout.jsonl").write_bytes(stdout)
    (req.runtime_dir / "stderr.log").write_bytes(stderr)
    if code in ("STOPPED", "TIMEOUT"):
        return WorkerResult(status="blocked", summary=f"pi worker {code.lower()}", blocker=code, raw_exit=rc)
    if b"No API key found" in stderr:
        return WorkerResult(status="blocked", summary="Pi authentication required", blocker="AUTH_BLOCKED", raw_exit=rc)
    if rc != 0:
        return WorkerResult(status="failed", summary=f"pi exited {rc}",
                            blocker=stderr.decode("utf-8", "replace")[-500:], raw_exit=rc)
    try:
        raw_val, meta = _decode_pi_cli(stdout)
        parsed = validate_worker_result(raw_val)
        return WorkerResult(
            status=parsed["status"], summary=parsed["summary"],
            blocker=parsed["blocker"], next_step=parsed["next_step"], raw_exit=rc, metadata=meta,
        )
    except Exception as exc:
        return WorkerResult(status="failed", summary=f"invalid pi result: {exc}", raw_exit=rc)


async def _run_acp_worker_async(req: WorkerRequest, command: list[str], env: dict[str, str]) -> WorkerResult:
    import acp
    from acp.schema import DeniedOutcome, RequestPermissionResponse, TextContentBlock

    turn = uuid.uuid4().hex
    native_reason: dict[str, str | None] = {"reason": None, "result": None}
    chunks = io.StringIO()

    class _Client:
        def __init__(self):
            self.session_id: str | None = None

        async def request_permission(self, **kwargs):
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        async def session_update(self, session_id: str, update, **kwargs):
            if session_id != self.session_id:
                return
            kind = getattr(update, "session_update", "")
            if kind == "agent_message_chunk":
                content = getattr(update, "content", None)
                if getattr(content, "type", None) == "text":
                    chunks.write(content.text)
            elif kind == "session_info_update":
                meta = getattr(update, "field_meta", None) or {}
                receipt = meta.get("ctaskNativeReceipt")
                if isinstance(receipt, dict) and receipt.get("turn") == turn:
                    native_reason["reason"] = receipt.get("stopReason")
                    if receipt.get("stopReason") == "stop" and isinstance(receipt.get("result"), str):
                        native_reason["result"] = receipt["result"]

    client = _Client()
    prompt_text = req.prompt + "\nReturn only a JSON object matching this schema: " + json.dumps(WORKER_RESULT_SCHEMA)
    async with acp.spawn_agent_process(
        client, *command, cwd=req.worktree, env=env,
        transport_kwargs={"stderr": asyncio.subprocess.DEVNULL},
    ) as (conn, _proc):
        await conn.initialize(protocol_version=1)
        session = await conn.new_session(cwd=str(req.worktree))
        client.session_id = session.session_id
        response = await asyncio.wait_for(
            conn.prompt(session_id=client.session_id, prompt=[TextContentBlock(type="text", text=prompt_text)], ctaskTurn=turn),
            timeout=req.timeout_seconds,
        )
    if response.stop_reason != "end_turn" or native_reason["reason"] != "stop":
        return WorkerResult(status="failed", summary=f"acp_pi ended with transport={response.stop_reason}, native={native_reason['reason']}")
    text = native_reason["result"] if native_reason["result"] is not None else chunks.getvalue()
    parsed = validate_worker_result(json.loads(text))
    return WorkerResult(status=parsed["status"], summary=parsed["summary"],
                        blocker=parsed["blocker"], next_step=parsed["next_step"])


def _run_acp_pi_worker(req: WorkerRequest) -> WorkerResult:
    try:
        import acp  # noqa: F401
    except ImportError:
        return WorkerResult(status="blocked", summary="Python package agent-client-protocol is not installed",
                            blocker="UNSUPPORTED_CAPABILITY")
    pi_bin = config.resolve_executable("pi")
    node_bin = config.resolve_executable("node")
    if not pi_bin or not node_bin:
        return WorkerResult(status="blocked", summary="pi or node executable not installed", blocker="EXECUTABLE_UNAVAILABLE")
    if not SANDBOX_EXEC.is_file():
        return WorkerResult(status="blocked", summary="macOS sandbox-exec unavailable", blocker="UNSUPPORTED_CAPABILITY")

    req.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (req.runtime_dir / "tmp").mkdir(exist_ok=True)
    derived_js = derive_directory(req.runtime_dir / "adapter", role="worker")
    launcher = _write_pi_wrapper(req.runtime_dir, pi_bin, ROOT / "pi-path-guard.mjs",
                                 sandbox=req.sandbox, model=req.model)
    profile = req.runtime_dir / "sandbox.sb"
    profile.write_text(worker_sandbox_profile(req.worktree, req.runtime_dir, req.sandbox), encoding="utf-8")
    env = dict(
        os.environ,
        CTASK_PI_ACP_STATE_DIR=str(req.runtime_dir / "state"),
        PI_ACP_PI_COMMAND=str(launcher),
        CTASK_AGENT_REPO=str(req.worktree),
        CTASK_AGENT_SANDBOX=req.sandbox,
        CTASK_PI_RUNTIME=str(req.runtime_dir),
        PI_CODING_AGENT_DIR=str(config.pi_agent_dir()),
        TMPDIR=str(req.runtime_dir / "tmp"),
        JITI_CACHE=str(req.runtime_dir / "jiti"),
        PI_TELEMETRY="0",
    )
    command = [str(SANDBOX_EXEC), "-f", str(profile), node_bin, str(derived_js)]
    try:
        return asyncio.run(_run_acp_worker_async(req, command, env))
    except Exception as exc:
        return WorkerResult(status="failed", summary=f"acp_pi failed: {exc}")


def run_worker(req: WorkerRequest) -> WorkerResult:
    if req.kind == "acp_pi":
        return _run_acp_pi_worker(req)
    return _run_pi_cli_worker(req)


class PiCoordinator:
    """Slim foreground Pi ACP coordinator session (Choice 2B): native identity + correlated turns + file confinement."""

    def __init__(self, project: str | Path, *, sandbox: str = "read-only",
                 model: str | None = None, timeout: float = 60.0,
                 command_override: list[str] | None = None):
        self.project = Path(project).resolve(strict=True)
        if sandbox not in ("read-only", "workspace-write"):
            raise ValueError("sandbox must be read-only or workspace-write")
        self.sandbox = sandbox
        self.model = model
        self.timeout = timeout
        self.command_override = command_override
        self.runtime = Path(tempfile.mkdtemp(prefix="ato-pi-coord-"))
        os.chmod(self.runtime, 0o700)
        self.session_id: str | None = None
        self.native_id: str | None = None
        self._conn = None
        self._proc = None
        self._ctx = None
        self._events: list[dict] = []
        self._turn_text: dict[str, io.StringIO] = {}
        self._turn_receipt: dict[str, dict] = {}

    async def start(self) -> dict:
        import acp

        if self.command_override is not None:
            command = list(self.command_override)
            env = dict(os.environ, CTASK_AGENT_REPO=str(self.project), CTASK_AGENT_SANDBOX=self.sandbox)
        else:
            pi_bin = config.resolve_executable("pi")
            node_bin = config.resolve_executable("node")
            if not pi_bin or not node_bin:
                raise RuntimeError("pi and node binaries are required for PiCoordinator")
            derived_js = derive_directory(self.runtime / "adapter", role="coordinator")
            launcher = _write_pi_wrapper(self.runtime, pi_bin, ROOT / "pi-coordinator-guard.mjs",
                                         sandbox=self.sandbox, model=self.model)
            (self.runtime / "tmp").mkdir(exist_ok=True)
            profile = self.runtime / "sandbox.sb"
            profile.write_text(worker_sandbox_profile(self.project, self.runtime, self.sandbox), encoding="utf-8")
            env = dict(
                os.environ,
                CTASK_PI_ACP_STATE_DIR=str(self.runtime / "state"),
                PI_ACP_PI_COMMAND=str(launcher),
                CTASK_AGENT_REPO=str(self.project),
                CTASK_AGENT_SANDBOX=self.sandbox,
                CTASK_PI_RUNTIME=str(self.runtime),
                PI_CODING_AGENT_DIR=str(config.pi_agent_dir()),
                TMPDIR=str(self.runtime / "tmp"),
                JITI_CACHE=str(self.runtime / "jiti"),
                PI_TELEMETRY="0",
            )
            command = [str(SANDBOX_EXEC), "-f", str(profile), node_bin, str(derived_js)] if SANDBOX_EXEC.is_file() else [node_bin, str(derived_js)]

        coord = self

        class _CoordClient:
            async def request_permission(self, **kwargs):
                from acp.schema import DeniedOutcome, RequestPermissionResponse
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

            async def session_update(self, session_id: str, update, **kwargs):
                if coord.session_id and session_id != coord.session_id:
                    return
                meta = getattr(update, "field_meta", None) or {}
                turn = meta.get("ctaskTurn")
                kind = getattr(update, "session_update", "")
                coord._events.append({"session_id": session_id, "kind": kind, "turn": turn, "at": time.time()})
                if kind == "agent_message_chunk":
                    content = getattr(update, "content", None)
                    if getattr(content, "type", None) == "text" and isinstance(turn, str) and turn in coord._turn_text:
                        coord._turn_text[turn].write(content.text)
                elif kind == "session_info_update":
                    receipt = meta.get("ctaskNativeReceipt")
                    if isinstance(receipt, dict) and isinstance(receipt.get("turn"), str):
                        coord._turn_receipt[receipt["turn"]] = receipt

        self._ctx = acp.spawn_agent_process(
            _CoordClient(), *command, cwd=self.project, env=env,
            transport_kwargs={"stderr": asyncio.subprocess.DEVNULL},
        )
        self._conn, self._proc = await self._ctx.__aenter__()
        await asyncio.wait_for(self._conn.initialize(protocol_version=1), timeout=self.timeout)
        created = await asyncio.wait_for(self._conn.new_session(cwd=str(self.project)), timeout=self.timeout)
        self.session_id = created.session_id
        meta = getattr(created, "field_meta", None) or {}
        ato_id = meta.get("atoIdentity") if isinstance(meta, dict) else None
        self.native_id = ato_id.get("nativeId") if isinstance(ato_id, dict) else self.session_id
        return {"session_id": self.session_id, "native_id": self.native_id, "project": str(self.project), "sandbox": self.sandbox}

    async def prompt(self, text: str) -> dict:
        from acp.schema import TextContentBlock
        if self._conn is None or self.session_id is None:
            raise RuntimeError("PiCoordinator not started")
        turn = uuid.uuid4().hex
        self._turn_text[turn] = io.StringIO()
        response = await asyncio.wait_for(
            self._conn.prompt(session_id=self.session_id, prompt=[TextContentBlock(type="text", text=text)], ctaskTurn=turn),
            timeout=self.timeout,
        )
        receipt = self._turn_receipt.get(turn, {})
        reply = receipt.get("result") if isinstance(receipt.get("result"), str) else self._turn_text[turn].getvalue()
        return {
            "turn": turn,
            "transport_stop_reason": response.stop_reason,
            "native_stop_reason": receipt.get("stopReason"),
            "text": reply,
        }

    async def cancel(self) -> None:
        if self._conn is not None and self.session_id is not None:
            await self._conn.cancel(session_id=self.session_id)

    async def close(self) -> None:
        try:
            if self._ctx is not None:
                await self._ctx.__aexit__(None, None, None)
        finally:
            self._conn = None
            self._proc = None
            self._ctx = None
            shutil.rmtree(self.runtime, ignore_errors=True)
