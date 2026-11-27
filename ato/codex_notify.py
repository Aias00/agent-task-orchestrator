"""Codex local session verification, turn config extraction, and queue/app-server delivery."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import struct
import subprocess
import time
import uuid

from ato import config

MAX_LINE = 8 * 1024 * 1024
MAX_TAIL_BYTES = 16 * 1024 * 1024


def normalize_thread_id(value: str) -> str:
    return str(uuid.UUID(value))


def session_path(codex_home: str | Path, thread_id: str) -> str:
    """Verify thread_id in local Codex read-only sqlite index and rollout header."""
    tid = normalize_thread_id(thread_id)
    for db in sorted(Path(codex_home).glob("state_*.sqlite"), reverse=True):
        try:
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2) as con:
                row = con.execute("SELECT rollout_path FROM threads WHERE id = ?", (tid,)).fetchone()
            if row:
                path = Path(row[0]).resolve()
                with path.open("rb") as fh:
                    meta = json.loads(fh.readline(MAX_LINE + 1))
                if isinstance(meta, dict) and meta.get("payload", {}).get("id") == tid:
                    return str(path)
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            continue
    raise ValueError("Codex session could not be verified in CODEX_HOME")


def worker_config_args(codex_home: str | Path, thread_id: str | None) -> list[str]:
    """Read last recorded turn's model/effort/approval/sandbox policy for Codex-submitted tasks."""
    if not thread_id:
        return []
    try:
        with Path(session_path(codex_home, thread_id)).open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - MAX_TAIL_BYTES))
            lines = fh.read(MAX_TAIL_BYTES).splitlines()
    except (OSError, ValueError, TypeError):
        return []
    turn = None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "turn_context" and isinstance(rec.get("payload"), dict):
            turn = rec["payload"]
            break
    if turn is None:
        return []
    args: list[str] = []
    for field, key in (("model", "model"), ("effort", "model_reasoning_effort"), ("approval_policy", "approval_policy")):
        val = turn.get(field)
        if isinstance(val, str) and val and "\0" not in val:
            args.extend(["-c", f"{key}={json.dumps(val)}"])
    sandbox = turn.get("sandbox_policy")
    if isinstance(sandbox, dict):
        mode = sandbox.get("type")
        if mode in ("read-only", "workspace-write", "danger-full-access"):
            args.extend(["--sandbox", mode])
        if mode == "workspace-write":
            for field in ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"):
                val = sandbox.get(field)
                if type(val) is bool:
                    args.extend(["-c", f"sandbox_workspace_write.{field}={json.dumps(val)}"])
            roots = sandbox.get("writable_roots")
            if isinstance(roots, list) and all(isinstance(r, str) and os.path.isabs(r) and "\0" not in r for r in roots):
                args.extend(["-c", f"sandbox_workspace_write.writable_roots={json.dumps(roots)}"])
    return args


def send_queue_notice(codex_home: str | Path, thread_id: str, message: str, *,
                      queue_bin: str | None = None, cwd: str | Path | None = None) -> tuple[bool, str | None, str]:
    """Deliver a compact notification into a verified Codex session via `codex queue`."""
    tid = normalize_thread_id(thread_id)
    # Verify session exists locally first.
    session_path(codex_home, tid)
    binary = queue_bin or os.environ.get("ATO_QUEUE_BIN") or os.environ.get("CTASK_QUEUE_BIN") or config.resolve_executable("codex")
    if not binary:
        return False, None, "codex binary unavailable"
    env = dict(os.environ, CODEX_HOME=str(Path(codex_home).resolve()))
    proc = subprocess.run(
        [binary, "queue", "--thread", tid, "--message", message],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=25,
    )
    receipts = re.findall(r"Queued message ([0-9a-f-]+) for thread ([0-9a-f-]+)(?=[.\s]|$)", proc.stdout)
    if proc.returncode == 0 and len(receipts) == 1:
        msg_id, target = receipts[0]
        try:
            if normalize_thread_id(msg_id) == msg_id and normalize_thread_id(target) == tid:
                return True, msg_id, ""
        except ValueError:
            pass
    if proc.returncode == 0:
        return True, None, ""
    detail = (proc.stderr or proc.stdout).strip()[:240] or f"exit {proc.returncode}"
    return False, None, detail


class AppServerClient:
    """Bounded JSON-RPC client over the local Codex daemon Unix WebSocket socket."""

    def __init__(self, codex_home: str | Path, timeout: float = 20.0):
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.buffer = b""
        self.sequence = 0
        try:
            self.sock.connect(str(Path(codex_home) / "app-server-control/app-server-control.sock"))
            key = base64.b64encode(os.urandom(16)).decode()
            req = (
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n"
            )
            self.sock.sendall(req.encode())
            while b"\r\n\r\n" not in self.buffer:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("daemon closed handshake")
                self.buffer += chunk
                if len(self.buffer) > 65536:
                    raise ValueError("oversized daemon handshake")
            headers, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            lines = headers.decode().split("\r\n")
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            fields = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip() for line in lines[1:] if ":" in line}
            if " 101 " not in lines[0] or fields.get("sec-websocket-accept") != expected:
                raise ValueError("daemon rejected WebSocket handshake")
            self.request("initialize", {"clientInfo": {"name": "ato-v2", "version": "2.0"}, "capabilities": {"experimentalApi": False}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.sock.close()
            raise

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "AppServerClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _frame(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size <= 65535:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, value: dict) -> None:
        self.sock.settimeout(self.timeout)
        self._frame(json.dumps(value, ensure_ascii=False).encode())

    def _read(self, n: int, deadline: float) -> bytes:
        while len(self.buffer) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("daemon response timeout")
            self.sock.settimeout(left)
            chunk = self.sock.recv(min(65536, n - len(self.buffer)))
            if not chunk:
                raise ConnectionError("daemon closed connection")
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def _receive(self, deadline: float) -> dict:
        chunks = bytearray()
        while True:
            first, second = self._read(2, deadline)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read(2, deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8, deadline))[0]
            data = self._read(size, deadline)
            op = first & 0x0F
            if op == 8:
                raise ConnectionError("daemon closed WebSocket")
            if op == 9:
                self._frame(data, 10)
                continue
            if op == 10:
                continue
            chunks.extend(data)
            if first & 0x80:
                return json.loads(chunks)

    def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        seq = self.sequence
        self.send({"id": seq, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            msg = self._receive(deadline)
            if msg.get("id") == seq and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise ValueError(f"daemon {method}: {msg['error']}")
                return msg["result"]
            if "id" in msg and "method" in msg:
                self.send({"id": msg["id"], "error": {"code": -32601, "message": "Not supported"}})
