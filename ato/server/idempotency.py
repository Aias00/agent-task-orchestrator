"""Durable intent-before-effect journal. Pending is unresolved, never a replay lease."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from ato import config
from ato.spec import TaskError, canonical_json, require, sha256_bytes


class Journal:
    def __init__(self, path: Path):
        self.path = path
        config.ensure_private_dir(path.parent)
        # Establish private permissions before SQLite can put anything in the file.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self.connection() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS requests (
                principal TEXT NOT NULL, key_hash TEXT NOT NULL, args_hash TEXT NOT NULL,
                operation TEXT NOT NULL, task_id TEXT, state TEXT NOT NULL,
                result_json TEXT, created_at REAL NOT NULL,
                PRIMARY KEY(principal, key_hash))""")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA synchronous=FULL")
        try:
            yield con
        finally:
            con.close()

    def perform(self, principal: str, key: str, operation: str, args: dict, task_id: str | None, effect,
                *, prepare=None):
        key_hash = sha256_bytes(key.encode())
        digest = sha256_bytes(canonical_json({"operation": operation, "arguments": args}))
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM requests WHERE principal=? AND key_hash=?",
                              (principal, key_hash)).fetchone()
            if row:
                con.execute("COMMIT")
                require(row["args_hash"] == digest, "IDEMPOTENCY_CONFLICT")
                if row["state"] == "complete":
                    return json.loads(row["result_json"])
                raise TaskError("REQUEST_UNRESOLVED", task_id=row["task_id"])
            # Read-only validation happens only for a new intent. A replay must not resolve
            # a moving Git HEAD again, and invalid input must not consume its request key.
            if prepare is not None:
                prepare()
            con.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, 'unresolved', NULL, ?)",
                        (principal, key_hash, digest, operation, task_id, time.time()))
            con.execute("COMMIT")
        # No transaction/lock spans execution. Duplicates see unresolved while this call runs.
        try:
            result = effect()
            encoded = canonical_json(result).decode()
            with self.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("UPDATE requests SET state='complete', result_json=? WHERE principal=? AND key_hash=?",
                            (encoded, principal, key_hash))
                con.execute("COMMIT")
            return result
        except Exception:
            # Even an engine error may follow task/worktree creation or a delivered STOP.
            raise TaskError("REQUEST_UNRESOLVED", task_id=task_id) from None
