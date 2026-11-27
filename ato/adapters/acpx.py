"""Host journal and bounded stdio transport for the opt-in Codex ACP reviewer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from ato import config, process
from ato.adapters.base import ReviewRequest, ReviewResult, has_secret, secret_values
from ato.spec import TaskError, strict_json, validate_review_verdict
from ato.store import Store


BRIDGE = Path(__file__).with_name("acpx_bridge.mjs")
START_LIMIT = 2 * 1024 * 1024
FRAME_LIMIT = 512 * 1024  # a 64 KiB verdict can expand under JSON escaping
VERDICT_LIMIT = 64 * 1024
STOP_GRACE = 2.0
SHA256 = re.compile(r"[a-f0-9]{64}")
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
DELIVERY_TRANSPORT = {
    "unsent": "not_dispatched",
    "finished": "response",
    "failed": "rpc_error",
    "unknown": "no_response",
}
DONE_CODES = frozenset({
    "OK", "STOPPED", "ACPX_SETUP_FAILED", "ACPX_DISPATCH_REJECTED",
    "ACPX_READ_ONLY_UNVERIFIED", "ACPX_PERMISSION_DENIED", "ACPX_REVIEW_FAILED",
})
CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{16,}|github_pat_[a-z0-9_]{16,}|"
    r"bearer\s+[a-z0-9._~+/=-]{16,}|eyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})"
)
CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def host_configuration(worktree: Path | None = None) -> dict[str, str] | None:
    raw = config.load_settings().get("acpx_reviewer")
    if sys.platform not in ("darwin", "linux") or not isinstance(raw, dict):
        return None
    result = {}
    try:
        for key in ("node", "runtime", "launcher"):
            value = raw.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute():
                return None
            path = Path(value).resolve(strict=True)
            mode = os.R_OK if key == "runtime" else os.X_OK
            if (not path.is_file() or not os.access(path, mode)
                    or (worktree is not None and path.is_relative_to(worktree))):
                return None
            result[key] = str(path)
    except (OSError, RuntimeError, ValueError):
        return None
    return result if BRIDGE.is_file() else None


def _bridge_argv(host: dict) -> list[str]:
    return [host["node"], str(BRIDGE)]


def _bridge_environment(home: Path, node: str, worktree: Path) -> dict[str, str]:
    env = {
        "PATH": os.pathsep.join((str(Path(node).parent), "/usr/bin", "/bin")),
        "HOME": str(home),
        "CODEX_HOME": str(home / "codex"),
        "TMPDIR": str(home / "tmp"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "LANG": "C.UTF-8",
        "INITIAL_AGENT_MODE": "read-only",
        "NO_BROWSER": "1",
    }
    for key in ("CODEX_HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        config.ensure_private_dir(Path(env[key]))
    (Path(env["CODEX_HOME"]) / "config.toml").write_text(
        f'[projects.{json.dumps(str(worktree), ensure_ascii=False)}]\ntrust_level = "untrusted"\n', encoding="utf-8"
    )
    if os.environ.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        env["DEFAULT_AUTH_REQUEST"] = '{"methodId":"api-key"}'
    return env


def _frame(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _write_start(fd: int, frame: bytes) -> None:
    if len(frame) > START_LIMIT:
        raise ValueError("review prompt exceeds IPC limit")
    pending = memoryview(frame)
    deadline = time.monotonic() + 2
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise TimeoutError("bridge start write timed out")
        pending = pending[os.write(fd, pending):]


def _identity(frame: dict, request_id: str, *, dispatch: bool = False) -> dict | None:
    if frame.get("request_id") != request_id or frame.get("record_id") != request_id:
        return None
    turn = frame.get("turn_id")
    session = frame.get("session_sha256")
    rpc = frame.get("rpc_id")
    if not isinstance(turn, str) or not UUID.fullmatch(turn):
        return None
    if not isinstance(session, str) or not SHA256.fullmatch(session):
        return None
    # The fork's stdio JSON-RPC client assigns nonnegative numeric IDs.
    if (dispatch or rpc is not None) and (type(rpc) is not int or not 0 <= rpc < 2**53):
        return None
    return {"request_id": request_id, "record_id": request_id, "turn_id": turn,
            "session_sha256": session, "rpc_id": rpc}


def _receipt(frame: dict, request_id: str, dispatch: dict | None) -> dict | None:
    identity = _identity(frame, request_id)
    delivery = frame.get("delivery")
    if not identity or not isinstance(delivery, str) or DELIVERY_TRANSPORT.get(delivery) != frame.get("transport"):
        return None
    if delivery != "unsent" and not dispatch:
        return None
    if dispatch and any(dispatch[key] != value for key, value in identity.items()
                        if key != "rpc_id" or delivery != "unsent" or value is not None):
        return None
    status = frame.get("turn_status")
    cancel = {key: frame.get(key) for key in ("cancel_requested", "cancel_written", "cancel_acknowledged")}
    if status not in ("completed", "cancelled", "failed") or any(type(v) is not bool for v in cancel.values()):
        return None
    if cancel["cancel_acknowledged"] and delivery != "finished":
        return None
    return {"delivery": delivery, "transport": frame["transport"], "turn_status": status, **cancel}


def _verdict_text(verdict: dict):
    yield verdict["summary"]
    yield from verdict["checked_items"]
    yield from verdict["residual_risks"]
    for finding in verdict["findings"]:
        yield from finding.values()


def delivery_of(row: dict) -> str:
    if row["receipt"] is not None:
        return row["receipt"]["delivery"]
    return "unknown" if row["dispatch"] else "unsent"


def recovery_blocker(row: dict) -> str | None:
    if delivery_of(row) == "unknown":
        return "ACPX_DELIVERY_UNKNOWN"
    if not row["bridge_closed"]:
        return "CLEANUP_BLOCKED"
    return None


def reconcile_cleanup(store: Store, row: dict, *, stop: bool = False) -> bool:
    """Join independent OS evidence to a durable launch even after bridge loss."""
    if row["bridge_closed"]:
        return True
    bridge = row["bridge"]
    bridge_gone = process.owned_group_exited(bridge["pid"], bridge["birth"])
    if not bridge_gone and stop:
        bridge_gone = process.stop_owned_group(bridge["pid"], bridge["birth"])
    launch = row["launch"]
    cleanup = row["cleanup"]
    if launch and cleanup != "verified_gone":
        pid, birth = launch["pid"], launch["birth"]
        group_gone = process.owned_group_exited(pid, birth)
        if not group_gone and stop:
            witnesses = {int(member): stamp for member, stamp in launch.get("witnesses", {}).items()}
            group_gone = process.stop_owned_group(pid, birth, witnesses=witnesses)
        if group_gone:
            cleanup = "verified_gone"
    closed = bridge_gone and cleanup in ("verified_gone", "no_process")
    store.record_acpx_cleanup(row["request_id"], cleanup if closed else "unknown", bridge_closed=closed)
    if closed:
        home = config.runtime_dir() / row["task_id"] / row["run_id"] / f"review-{row['round']}" / "native"
        shutil.rmtree(home, ignore_errors=True)
    return closed


class _BridgeReview:
    def __init__(self, req: ReviewRequest, proc: subprocess.Popen, request_id: str):
        self.req = req
        self.store = req.store
        self.proc = proc
        self.request_id = request_id
        self.observed_launch = None
        self.launch = None
        self.dispatch = None
        self.receipt = None
        self.done = None
        self.stopping = False
        self.input_fd = proc.stdin.fileno()
        self.output_fd = proc.stdout.fileno()
        os.set_blocking(self.input_fd, False)
        os.set_blocking(self.output_fd, False)

    def _ack(self, frame_type: str, record: str, accepted: bool) -> None:
        success = _frame({"type": "ack", "for": frame_type, "ok": True})
        if accepted and not self.stopping and self.store.ack_acpx(
            self.request_id, record, lambda: os.write(self.input_fd, success)
        ):
            return
        os.write(self.input_fd, _frame({"type": "ack", "for": frame_type, "ok": False}))

    def handle(self, frame: dict) -> None:
        kind = frame.get("type")
        if self.done is not None:
            raise ValueError("unexpected frame after final result")
        if kind == "launch" and self.launch is None:
            pid, launch_id = frame.get("pid"), frame.get("launch_id")
            accepted = False
            if (type(pid) is int and 0 < pid < 2**31 and pid != self.proc.pid
                    and frame.get("group_id") == pid
                    and isinstance(launch_id, str) and UUID.fullmatch(launch_id)):
                birth = process.group_birth(pid)
                if birth and process.parent_pid(pid) == self.proc.pid and process.identity(pid) == birth:
                    launch = {"launch_id": launch_id, "pid": pid, "group_id": pid, "birth": birth}
                    self.observed_launch = launch
                    accepted = self.store.record_acpx_launch(self.request_id, launch)
                    if accepted:
                        self.launch = launch
            self._ack("launch", "launch_json", accepted)
        elif kind == "dispatch" and self.dispatch is None and self.launch:
            identity = _identity(frame, self.request_id, dispatch=True)
            accepted = False
            if identity and frame.get("launch_id") == self.launch["launch_id"]:
                pid, birth = self.launch["pid"], self.launch["birth"]
                if process.identity(pid) == birth:
                    peers = process.group_members(pid) or {}
                    if process.group_birth(pid) == birth and process.identity(pid) == birth:
                        dispatch = {**identity, "launch_id": self.launch["launch_id"]}
                        witnesses = dict(sorted(peers.items())[:64])
                        accepted = self.store.authorize_acpx_dispatch(self.request_id, dispatch, witnesses)
                        if accepted:
                            self.dispatch = dispatch
            self._ack("dispatch", "dispatch_json", accepted)
        elif kind == "cleanup":
            if (self.launch and frame.get("launch_id") == self.launch["launch_id"]
                    and frame.get("root_exit_observed") is True
                    and frame.get("group_disposition") == "verified_gone"
                    and process.owned_group_exited(self.launch["pid"], self.launch["birth"])):
                self.store.record_acpx_cleanup(self.request_id, "verified_gone")
        elif kind == "receipt" and self.receipt is None:
            self.receipt = _receipt(frame, self.request_id, self.dispatch)
            if self.receipt:
                self.store.record_acpx_receipt(self.request_id, self.receipt)
        elif kind == "done" and isinstance(frame.get("code"), str) and frame["code"] in DONE_CODES:
            self.done = frame
            if self.launch is None and frame.get("no_process") is True:
                self.store.record_acpx_cleanup(self.request_id, "no_process")
        else:
            raise ValueError("unexpected bridge frame")

    def exchange(self, host: dict) -> None:
        _write_start(self.input_fd, _frame({
            "type": "start", "launcher": host["launcher"], "runtime": host["runtime"],
            "cwd": str(self.req.worktree), "request_id": self.request_id,
            "timeout_ms": self.req.timeout_seconds * 1000, "prompt": self.req.prompt,
        }))
        deadline = time.monotonic() + self.req.timeout_seconds + STOP_GRACE
        buffer = bytearray()
        frames = 0
        while time.monotonic() < deadline:
            if not self.stopping and self.req.stop_check and self.req.stop_check():
                self.stopping = True
                deadline = min(deadline, time.monotonic() + STOP_GRACE)
                try:
                    os.write(self.input_fd, _frame({"type": "stop"}))
                except BrokenPipeError:
                    pass  # Drain any already written receipt.
            if not select.select([self.output_fd], [], [], 0.05)[0]:
                continue
            data = os.read(self.output_fd, 65536)
            if not data:
                return
            buffer.extend(data)
            while (end := buffer.find(b"\n")) >= 0:
                frames += 1
                if end > FRAME_LIMIT or frames > 5:
                    raise ValueError("bridge IPC limit exceeded")
                self.handle(strict_json(bytes(buffer[:end])))
                del buffer[:end + 1]
            if len(buffer) > FRAME_LIMIT:
                raise ValueError("oversized bridge frame")
            if self.done:
                deadline = min(deadline, time.monotonic() + 0.5)

    def result(self, row: dict) -> ReviewResult:
        code = recovery_blocker(row)
        if code:
            return ReviewResult(False, code)
        if self.stopping or (self.req.stop_check and self.req.stop_check()):
            return ReviewResult(False, "STOPPED")
        code = self.done["code"] if self.done else "ACPX_BRIDGE_FAILED"
        if code != "OK":
            return ReviewResult(False, code)
        if (not self.receipt or self.receipt["delivery"] != "finished"
                or self.receipt["turn_status"] != "completed"):
            return ReviewResult(False, "ACPX_REVIEW_FAILED")
        try:
            raw = self.done.get("verdict")
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > VERDICT_LIMIT:
                return ReviewResult(False, "INVALID_EVIDENCE")
            verdict = validate_review_verdict(strict_json(raw, "INVALID_EVIDENCE"))
            if has_secret(verdict, secret_values()) or any(CREDENTIAL_PATTERN.search(s) for s in _verdict_text(verdict)):
                return ReviewResult(False, "SECRET_IN_REVIEW")
            if any(CONTROL_PATTERN.search(s) for s in _verdict_text(verdict)):
                return ReviewResult(False, "INVALID_EVIDENCE")
            return ReviewResult(True, "OK", verdict)
        except (TaskError, UnicodeError):
            return ReviewResult(False, "INVALID_EVIDENCE")


def run_review(req: ReviewRequest) -> ReviewResult:
    if req.session_mode != "fresh_only" or req.model or req.effort:
        return ReviewResult(False, "ACPX_UNSUPPORTED_CONFIGURATION")
    host = host_configuration(req.worktree)
    if not host:
        return ReviewResult(False, "ACPX_UNCONFIGURED")
    if req.store is None or req.run_id is None:
        return ReviewResult(False, "ACPX_DISPATCH_REJECTED")
    if req.stop_check and req.stop_check():
        return ReviewResult(False, "STOPPED")
    store = req.store
    request_id = "review-" + uuid.uuid4().hex
    home = config.ensure_private_dir(req.runtime_dir / "native")
    try:
        proc = subprocess.Popen(
            _bridge_argv(host), cwd=home, env=_bridge_environment(home, host["node"], req.worktree),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError:
        shutil.rmtree(home, ignore_errors=True)
        return ReviewResult(False, "ACPX_BRIDGE_FAILED")

    admitted = False
    bridge = None
    try:
        birth = process.group_birth(proc.pid)
        if not birth or not store.begin_acpx_review(
            req.task_id, req.run_id, req.round_num, request_id, {"pid": proc.pid, "birth": birth}
        ):
            return ReviewResult(False, "ACPX_DISPATCH_REJECTED")
        admitted = True
        bridge = _BridgeReview(req, proc, request_id)
        bridge.exchange(host)
    except (OSError, ValueError, sqlite3.Error):
        if not admitted:
            return ReviewResult(False, "ACPX_DISPATCH_REJECTED")
        if bridge:
            bridge.done = None
    finally:
        process.terminate_popen(proc, grace_seconds=0.2)
        proc.stdin.close()
        proc.stdout.close()
        if admitted:
            row = store.acpx_review(req.task_id)
            if row["launch"] is None and bridge and bridge.observed_launch:
                # The first launch commit may have contended. Keep the identity
                # already verified while the bridge still parented the child.
                observed = bridge.observed_launch
                try:
                    store.record_acpx_launch(request_id, observed)
                finally:
                    process.stop_owned_group(observed["pid"], observed["birth"])
                row = store.acpx_review(req.task_id)
            if not row["bridge_closed"]:
                reconcile_cleanup(store, row, stop=True)
        else:
            shutil.rmtree(home, ignore_errors=True)

    row = store.acpx_review(req.task_id)
    return bridge.result(row) if bridge else ReviewResult(False, recovery_blocker(row) or "ACPX_BRIDGE_FAILED")
