"""Single-source-of-truth SQLite WAL store for ATO v2."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from ato import config
from ato.spec import TaskError, new_id, require, require_id

SCHEMA_VERSION = 2

_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  repo_root TEXT NOT NULL,
  repo_key TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  branch TEXT NOT NULL,
  worktree_path TEXT,
  worktree_state TEXT NOT NULL DEFAULT 'present',
  spec_json TEXT NOT NULL,
  spec_sha256 TEXT NOT NULL,
  spec_timeout_source TEXT,
  origin_kind TEXT NOT NULL DEFAULT 'cli',
  origin_session_id TEXT,
  status TEXT NOT NULL,
  reason TEXT,
  stop_requested INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  active_run_id TEXT,
  product_json TEXT,
  acceptance_json TEXT,
  stage_json TEXT,
  next_step TEXT,
  reviewer_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  phase TEXT NOT NULL,
  pid INTEGER,
  pid_identity TEXT,
  child_pid INTEGER,
  child_pid_identity TEXT,
  outcome TEXT,
  error_code TEXT,
  timeout_seconds INTEGER,
  timeout_source TEXT,
  created_at REAL NOT NULL,
  settled_at REAL
);

CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  criterion_id TEXT NOT NULL,
  state TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  output_excerpt TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  round INTEGER NOT NULL,
  reviewer_kind TEXT NOT NULL,
  verdict TEXT NOT NULL,
  verdict_json TEXT NOT NULL,
  session_id TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS acpx_reviews (
  request_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  round INTEGER NOT NULL,
  bridge_json TEXT NOT NULL,
  launch_json TEXT,
  dispatch_json TEXT,
  receipt_json TEXT,
  cleanup TEXT NOT NULL DEFAULT 'pending',
  bridge_closed INTEGER NOT NULL DEFAULT 0,
  UNIQUE (run_id, round)
);

CREATE INDEX IF NOT EXISTS idx_acpx_reviews_task ON acpx_reviews(task_id);

CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  question TEXT NOT NULL,
  reason TEXT NOT NULL,
  continuation TEXT,
  purpose_ref TEXT,
  answer TEXT,
  state TEXT NOT NULL,
  created_at REAL NOT NULL,
  answered_at REAL
);

CREATE TABLE IF NOT EXISTS watchdog_state (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  revision INTEGER,
  run_id TEXT,
  status TEXT,
  sampled_at REAL,
  sample_error TEXT,
  diff_error TEXT,
  evidence_complete INTEGER NOT NULL DEFAULT 0,
  last_progress_at REAL,
  progress_hash TEXT,
  metrics_json TEXT,
  pending_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS watchdog_alerts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT,
  code TEXT NOT NULL,
  state TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  resolved_at REAL,
  notification_state TEXT NOT NULL DEFAULT 'pending',
  notification_error TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  dedupe_key TEXT UNIQUE NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  target_session TEXT,
  body TEXT NOT NULL,
  state TEXT NOT NULL,
  receipt_id TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT,
  kind TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checks_task ON checks(task_id, run_id);
CREATE INDEX IF NOT EXISTS idx_reviews_task ON reviews(task_id, run_id);
CREATE INDEX IF NOT EXISTS idx_questions_task ON questions(task_id, state);
CREATE INDEX IF NOT EXISTS idx_alerts_task ON watchdog_alerts(task_id, state);
CREATE INDEX IF NOT EXISTS idx_notifications_state ON notifications(state, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""


_ACPX_UNRESOLVED = "(bridge_closed = 0 OR (dispatch_json IS NOT NULL AND receipt_json IS NULL) OR json_extract(receipt_json, '$.delivery') = 'unknown')"


def _is_busy(exc: sqlite3.Error) -> bool:
    code = (getattr(exc, "sqlite_errorcode", None) or 0) & 0xFF
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _retry(fn):
    deadline = time.monotonic() + 1.0
    delay = 0.01
    while True:
        try:
            return fn()
        except sqlite3.Error as exc:
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise TaskError("BUSY" if _is_busy(exc) else "STATE_CORRUPT") from None
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 0.1)


class Store:
    """Thin ACID SQLite persistence layer for all ATO v2 state."""

    def __init__(self, db_file: Path | None = None):
        self.db_file = Path(db_file) if db_file is not None else config.db_path()
        self._init_schema()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if not readonly:
            config.ensure_private_dir(self.db_file.parent)
            con = sqlite3.connect(str(self.db_file), timeout=5.0, isolation_level=None)
        else:
            con = sqlite3.connect(self.db_file.as_uri() + "?mode=ro", uri=True, timeout=5.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        return con

    def _init_schema(self) -> None:
        def op():
            con = self._connect()
            try:
                con.execute("PRAGMA journal_mode = WAL")
                con.execute("PRAGMA synchronous = NORMAL")
                con.executescript(_DDL)
                # Serialize inspection and alteration so concurrent openers are safe.
                con.execute("BEGIN IMMEDIATE")
                for table, additions in (
                    ("tasks", (("spec_timeout_source", "TEXT"),)),
                    ("runs", (("timeout_seconds", "INTEGER"), ("timeout_source", "TEXT"),
                              ("child_pid", "INTEGER"), ("child_pid_identity", "TEXT"))),
                ):
                    columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                    for name, sql_type in additions:
                        if name not in columns:
                            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                con.execute("COMMIT")
                try:
                    os.chmod(self.db_file, 0o600)
                except OSError:
                    pass
            finally:
                con.close()
        _retry(op)

    @contextmanager
    def tx(self, *, write: bool = True, admission: bool = False):
        con = self._connect(readonly=not write and self.db_file.exists())
        try:
            if admission:
                con.execute("PRAGMA synchronous = FULL")
                con.execute("PRAGMA busy_timeout = 200")
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.execute("COMMIT")
        except Exception:
            if write:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            con.close()

    def create_task(self, *, task_id: str, repo_root: str, repo_key: str, base_commit: str,
                    branch: str, worktree_path: str, spec_doc: dict, spec_sha256: str,
                    origin_kind: str, origin_session_id: str | None,
                    spec_timeout_source: str | None = None) -> dict:
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    """INSERT INTO tasks(id, repo_root, repo_key, base_commit, branch, worktree_path,
                       worktree_state, spec_json, spec_sha256, origin_kind, origin_session_id,
                       status, reason, stop_requested, revision, reviewer_json, created_at, updated_at,
                       spec_timeout_source)
                       VALUES (?, ?, ?, ?, ?, ?, 'present', ?, ?, ?, ?, 'queued', NULL, 0, 1, ?, ?, ?, ?)""",
                    (task_id, repo_root, repo_key, base_commit, branch, worktree_path,
                     json.dumps(spec_doc, ensure_ascii=False), spec_sha256, origin_kind, origin_session_id,
                     json.dumps(spec_doc["reviewer"]) if spec_doc.get("reviewer") else None, now, now,
                     spec_timeout_source),
                )
        _retry(op)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict:
        require_id(task_id, "task")
        def op():
            with self.tx(write=False) as con:
                row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                require(row is not None, "NOT_FOUND")
                return self._decode_task(row)
        return _retry(op)

    def _decode_task(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["spec"] = json.loads(d.pop("spec_json"))
        d["product"] = json.loads(d["product_json"]) if d["product_json"] else None
        d["acceptance"] = json.loads(d["acceptance_json"]) if d["acceptance_json"] else None
        d["stage"] = json.loads(d["stage_json"]) if d["stage_json"] else None
        d["reviewer"] = json.loads(d["reviewer_json"]) if d["reviewer_json"] else None
        d["stop_requested"] = bool(d["stop_requested"])
        return d

    def list_tasks(self, repo_key: str | None = None) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                if repo_key:
                    rows = con.execute("SELECT * FROM tasks WHERE repo_key = ? ORDER BY updated_at DESC",
                                       (repo_key,)).fetchall()
                else:
                    rows = con.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
                return [self._decode_task(r) for r in rows]
        return _retry(op)

    def update_task(self, task_id: str, **fields) -> dict:
        allowed = {
            "status", "reason", "stop_requested", "active_run_id", "worktree_state",
            "worktree_path", "product", "acceptance", "stage", "next_step", "reviewer",
        }
        unknown = set(fields) - allowed
        require(not unknown, "INTERNAL_ERROR")
        cols, vals = [], []
        for k, v in fields.items():
            if k in ("product", "acceptance", "stage", "reviewer"):
                cols.append(f"{k}_json = ?")
                vals.append(json.dumps(v, ensure_ascii=False) if v is not None else None)
            elif k == "stop_requested":
                cols.append("stop_requested = ?")
                vals.append(1 if v else 0)
            else:
                cols.append(f"{k} = ?")
                vals.append(v)
        now = time.time()
        cols.extend(["revision = revision + 1", "updated_at = ?"])
        vals.extend([now, task_id])
        def op():
            with self.tx(write=True) as con:
                cur = con.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE id = ?", vals)
                require(cur.rowcount == 1, "NOT_FOUND")
        _retry(op)
        return self.get_task(task_id)

    def create_run(self, task_id: str, kind: str, *, timeout_seconds: int | None = None,
                   timeout_source: str | None = None, expected_revision: int | None = None,
                   owner: tuple[int, str] | None = None) -> dict:
        run_id = new_id("run")
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                if expected_revision is not None:
                    task = con.execute("SELECT revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
                    require(task is not None and task["revision"] == expected_revision, "BUSY", task_id)
                    pending = con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                                          (task_id,)).fetchone()
                    require(pending is None, "CLEANUP_BLOCKED", task_id)
                con.execute(
                    """INSERT INTO runs(id, task_id, kind, phase, created_at, timeout_seconds, timeout_source, pid, pid_identity)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, task_id, kind, 'running' if owner else 'launching', now,
                     timeout_seconds, timeout_source, *(owner or (None, None))),
                )
                state = ""
                if kind == "resume":
                    state = "stop_requested = 0, status = 'queued', reason = NULL,"
                elif kind == "recheck" and owner:
                    state = "stop_requested = 0, status = 'running', reason = NULL,"
                con.execute(f"UPDATE tasks SET {state} active_run_id = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                            (run_id, now, task_id))
        with self._worktree_lease(task_id) if expected_revision is not None else nullcontext():
            _retry(op)
        return self.get_run(run_id)

    @staticmethod
    def _acpx_owner(con, task_id: str, run_id: str) -> bool:
        from ato import process

        row = con.execute("""SELECT r.pid, r.pid_identity FROM tasks t JOIN runs r ON r.id = t.active_run_id
                             WHERE t.id = ? AND r.id = ? AND r.task_id = t.id
                               AND t.stop_requested = 0 AND t.worktree_state = 'present' AND r.phase = 'running'""",
                          (task_id, run_id)).fetchone()
        return bool(row and row['pid'] == os.getpid() and row['pid_identity']
                    and process.identity(row['pid']) == row['pid_identity'])

    def begin_acpx_review(self, task_id: str, run_id: str, round_num: int, request_id: str, bridge: dict) -> bool:
        with self.tx(admission=True) as con:
            if not self._acpx_owner(con, task_id, run_id):
                return False
            if con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                           (task_id,)).fetchone():
                return False
            con.execute("INSERT INTO acpx_reviews(request_id, task_id, run_id, round, bridge_json) VALUES (?, ?, ?, ?, ?)",
                        (request_id, task_id, run_id, round_num, json.dumps(bridge)))
            return True

    def record_acpx_launch(self, request_id: str, launch: dict) -> bool:
        # Journal a verified child even if STOP already revoked dispatch authority.
        with self.tx(admission=True) as con:
            return con.execute("""UPDATE acpx_reviews SET launch_json = ?
                                  WHERE request_id = ? AND launch_json IS NULL AND bridge_closed = 0""",
                               (json.dumps(launch), request_id)).rowcount == 1

    def authorize_acpx_dispatch(self, request_id: str, dispatch: dict, witnesses: dict[int, str]) -> bool:
        with self.tx(admission=True) as con:
            row = con.execute("SELECT * FROM acpx_reviews WHERE request_id = ?", (request_id,)).fetchone()
            if (not row or row['bridge_closed'] or row['dispatch_json'] or not row['launch_json']
                    or json.loads(row['launch_json'])['launch_id'] != dispatch['launch_id']
                    or not self._acpx_owner(con, row['task_id'], row['run_id'])):
                return False
            launch = json.loads(row['launch_json'])
            launch['witnesses'] = witnesses
            con.execute("UPDATE acpx_reviews SET dispatch_json = ?, launch_json = ? WHERE request_id = ?",
                        (json.dumps(dispatch), json.dumps(launch), request_id))
            return True

    def ack_acpx(self, request_id: str, record: str, send) -> bool:
        # The prior commit is durable. Serialize the final nonblocking ACK with STOP.
        with self.tx(admission=True) as con:
            row = con.execute("SELECT * FROM acpx_reviews WHERE request_id = ?", (request_id,)).fetchone()
            if (row and not row['bridge_closed'] and row[record] and row['cleanup'] == 'pending'
                    and self._acpx_owner(con, row['task_id'], row['run_id'])):
                send()
                return True
            return False

    def record_acpx_receipt(self, request_id: str, receipt: dict) -> None:
        with self.tx(admission=True) as con:
            con.execute("UPDATE acpx_reviews SET receipt_json = ? WHERE request_id = ? AND receipt_json IS NULL",
                        (json.dumps(receipt), request_id))

    def record_acpx_cleanup(self, request_id: str, cleanup: str, *, bridge_closed: bool = False) -> None:
        with self.tx(admission=True) as con:
            con.execute("""UPDATE acpx_reviews SET cleanup = CASE
                             WHEN cleanup IN ('verified_gone', 'no_process') THEN cleanup ELSE ? END,
                             bridge_closed = MAX(bridge_closed, ?) WHERE request_id = ?""",
                        (cleanup, bridge_closed, request_id))

    def acpx_review(self, task_id: str, *, unresolved: bool = False) -> dict | None:
        with self.tx(write=False) as con:
            where = f"AND {_ACPX_UNRESOLVED}" if unresolved else ""
            row = con.execute(f"SELECT * FROM acpx_reviews WHERE task_id = ? {where} ORDER BY rowid DESC LIMIT 1",
                              (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ('bridge', 'launch', 'dispatch', 'receipt'):
            raw = result.pop(field + '_json')
            result[field] = json.loads(raw) if raw is not None else None
        return result

    def complete_acpx_run(self, task_id: str, run_id: str, product: dict, *,
                          stage: dict | None = None, reason: str | None = None) -> bool:
        now = time.time()
        with self.tx() as con:
            if not self._acpx_owner(con, task_id, run_id):
                return False
            require(con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                                (task_id,)).fetchone() is None, "CLEANUP_BLOCKED", task_id)
            con.execute("UPDATE runs SET phase = 'settled', outcome = 'completed', settled_at = ? WHERE id = ?",
                        (now, run_id))
            con.execute("""UPDATE tasks SET status = 'completed', reason = ?, product_json = ?,
                             stage_json = COALESCE(?, stage_json), next_step = NULL,
                             revision = revision + 1, updated_at = ? WHERE id = ?""",
                        (reason, json.dumps(product, ensure_ascii=False),
                         json.dumps(stage) if stage is not None else None, now, task_id))
            return True

    def settle_acpx_stop(self, task_id: str, run_id: str, blocker: str | None) -> None:
        now = time.time()
        status, reason = ("blocked", blocker) if blocker else ("stopped", "STOPPED")
        with self.tx() as con:
            row = con.execute("SELECT active_run_id, status, reason, stop_requested FROM tasks WHERE id = ?",
                              (task_id,)).fetchone()
            if row['active_run_id'] != run_id or not row['stop_requested'] or row['status'] == 'completed':
                return
            if row['reason'] == 'MUTATION_VIOLATION' or (row['status'], row['reason']) == (status, reason):
                return
            con.execute("UPDATE runs SET phase = 'settled', outcome = ?, error_code = ?, settled_at = ? WHERE id = ?",
                        (status, reason, now, run_id))
            con.execute("UPDATE tasks SET status = ?, reason = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                        (status, reason, now, task_id))

    @contextmanager
    def _worktree_lease(self, task_id: str):
        require_id(task_id, "task")
        path = config.ensure_private_dir(self.db_file.parent / "worktree-locks") / task_id
        with path.open("ab") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TaskError("BUSY", task_id) from None
            yield

    @contextmanager
    def remove_worktree(self, task_id: str, expected_revision: int):
        # Fence recovery Run claims through the commit and removal without holding the SQLite writer.
        with self._worktree_lease(task_id):
            task = self.get_task(task_id)
            require(task['revision'] == expected_revision, "BUSY", task_id)
            require(self.acpx_review(task_id, unresolved=True) is None, "CLEANUP_BLOCKED", task_id)
            yield
            self.update_task(task_id, worktree_state="removed", stage=None)

    def get_run(self, run_id: str) -> dict:
        def op():
            with self.tx(write=False) as con:
                row = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                require(row is not None, "NOT_FOUND")
                return dict(row)
        return _retry(op)

    def _transition_running(self, task_id: str, run_id: str, pid: int,
                            pid_identity: str | None, *, launching_only: bool,
                            match_owner: bool = False) -> bool:
        def op():
            with self.tx(write=True) as con:
                phase = "phase = 'launching'" if launching_only else "phase != 'settled'"
                status = "AND status = 'queued'" if launching_only else ""
                owner = ""
                params = (pid, pid_identity, run_id, task_id, task_id, run_id)
                if match_owner:
                    # An ACP dispatch is bound to the controller that owns the run.
                    owner = "AND (phase = 'launching' OR (pid = ? AND (pid_identity IS NULL OR pid_identity = ?)))"
                    params += (pid, pid_identity)
                cur = con.execute(
                    f"""UPDATE runs SET phase = 'running', pid = ?, pid_identity = ?
                        WHERE id = ? AND task_id = ? AND {phase}
                        AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND stop_requested = 0
                                    AND active_run_id = ? {status}) {owner}""",
                    params,
                )
                if cur.rowcount != 1:
                    return False
                con.execute(
                    """UPDATE tasks SET status = 'running', reason = NULL,
                       revision = revision + 1, updated_at = ? WHERE id = ?""",
                    (time.time(), task_id),
                )
                return True
        return _retry(op)

    def mark_run_running(self, task_id: str, run_id: str, pid: int, pid_identity: str | None) -> bool:
        return self._transition_running(task_id, run_id, pid, pid_identity, launching_only=True)

    def claim_run_execution(self, task_id: str, run_id: str, pid: int, pid_identity: str | None,
                            *, match_owner: bool = False) -> bool:
        return self._transition_running(task_id, run_id, pid, pid_identity,
                                        launching_only=False, match_owner=match_owner)

    def update_run(self, run_id: str, **fields) -> dict:
        allowed = {"phase", "pid", "pid_identity", "child_pid", "child_pid_identity", "outcome", "error_code", "settled_at"}
        require(set(fields) <= allowed, "INTERNAL_ERROR")
        cols = [f"{k} = ?" for k in fields]
        vals = list(fields.values()) + [run_id]
        def op():
            with self.tx(write=True) as con:
                con.execute(f"UPDATE runs SET {', '.join(cols)} WHERE id = ?", vals)
        _retry(op)
        return self.get_run(run_id)

    def list_runs(self, task_id: str) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                rows = con.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY created_at ASC", (task_id,)).fetchall()
                return [dict(r) for r in rows]
        return _retry(op)

    def save_checks(self, task_id: str, run_id: str, rows: list[dict]) -> None:
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute("DELETE FROM checks WHERE task_id = ? AND run_id = ?", (task_id, run_id))
                for r in rows:
                    con.execute(
                        """INSERT INTO checks(task_id, run_id, criterion_id, state, receipt_id, output_excerpt, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (task_id, run_id, r["criterion_id"], r["state"], r["receipt_id"],
                         r.get("output_excerpt", "")[:4096], now),
                    )
        _retry(op)

    def latest_checks(self, task_id: str, run_id: str | None = None) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                target_run = run_id
                if target_run is None:
                    row = con.execute(
                        "SELECT run_id FROM checks WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
                    ).fetchone()
                    if row is None:
                        return []
                    target_run = row["run_id"]
                rows = con.execute(
                    "SELECT criterion_id, state, receipt_id, output_excerpt FROM checks WHERE task_id = ? AND run_id = ? ORDER BY id ASC",
                    (task_id, target_run),
                ).fetchall()
                return [dict(r) for r in rows]
        return _retry(op)

    def add_review(self, task_id: str, run_id: str, round_num: int, reviewer_kind: str,
                   verdict: dict, session_id: str | None = None) -> None:
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    """INSERT INTO reviews(task_id, run_id, round, reviewer_kind, verdict, verdict_json, session_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, run_id, round_num, reviewer_kind, verdict["verdict"],
                     json.dumps(verdict, ensure_ascii=False), session_id, now),
                )
        _retry(op)

    def latest_review(self, task_id: str) -> dict | None:
        def op():
            with self.tx(write=False) as con:
                row = con.execute(
                    "SELECT * FROM reviews WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
                ).fetchone()
                if row is None:
                    return None
                d = dict(row)
                d["detail"] = json.loads(d.pop("verdict_json"))
                return d
        return _retry(op)

    def create_question(self, task_id: str, kind: str, question: str, reason: str,
                        continuation: str | None = None, purpose_ref: str | None = None) -> dict:
        qid = new_id("request")
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    """INSERT INTO questions(id, task_id, kind, question, reason, continuation, purpose_ref, state, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
                    (qid, task_id, kind, question, reason, continuation, purpose_ref, now),
                )
        _retry(op)
        return self.get_question(qid)

    def get_question(self, request_id: str) -> dict:
        require_id(request_id, "request")
        def op():
            with self.tx(write=False) as con:
                row = con.execute("SELECT * FROM questions WHERE id = ?", (request_id,)).fetchone()
                require(row is not None, "NOT_FOUND")
                return dict(row)
        return _retry(op)

    def list_questions(self, task_id: str | None = None, *, state: str | None = None) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                clauses, vals = [], []
                if task_id is not None:
                    clauses.append("task_id = ?")
                    vals.append(task_id)
                if state is not None:
                    clauses.append("state = ?")
                    vals.append(state)
                where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                rows = con.execute(f"SELECT * FROM questions{where} ORDER BY created_at ASC", vals).fetchall()
                return [dict(r) for r in rows]
        return _retry(op)

    def answer_question(self, request_id: str, answer: str) -> dict:
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                row = con.execute("SELECT * FROM questions WHERE id = ?", (request_id,)).fetchone()
                require(row is not None, "NOT_FOUND")
                if row["state"] == "answered":
                    require(row["answer"] == answer, "DECISION_CONFLICT")
                    return
                require(row["state"] == "open", "STALE_REQUEST")
                con.execute(
                    "UPDATE questions SET answer = ?, state = 'answered', answered_at = ? WHERE id = ?",
                    (answer, now, request_id),
                )
        _retry(op)
        return self.get_question(request_id)

    def enqueue_notification(self, *, dedupe_key: str, task_id: str, kind: str,
                             target_session: str | None, body: str, initial_state: str = "pending") -> dict:
        nid = new_id("notice")
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, dedupe_key, task_id, kind, target_session, body, state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (nid, dedupe_key, task_id, kind, target_session, body, initial_state, now, now),
                )
                row = con.execute("SELECT * FROM notifications WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
                return dict(row)
        return _retry(op)

    def pending_notifications(self) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                rows = con.execute(
                    "SELECT * FROM notifications WHERE state = 'pending' ORDER BY created_at ASC"
                ).fetchall()
                return [dict(r) for r in rows]
        return _retry(op)

    def update_notification(self, notice_id: str, *, state: str, receipt_id: str | None = None,
                            error: str | None = None) -> None:
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    "UPDATE notifications SET state = ?, receipt_id = COALESCE(?, receipt_id), error = ?, updated_at = ? WHERE id = ?",
                    (state, receipt_id, error, now, notice_id),
                )
        _retry(op)

    def list_notifications(self, task_id: str | None = None) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                if task_id:
                    rows = con.execute("SELECT * FROM notifications WHERE task_id = ? ORDER BY created_at ASC",
                                       (task_id,)).fetchall()
                else:
                    rows = con.execute("SELECT * FROM notifications ORDER BY created_at ASC").fetchall()
                return [dict(r) for r in rows]
        return _retry(op)

    def notification_counts(self) -> dict[str, int]:
        def op():
            with self.tx(write=False) as con:
                rows = con.execute("SELECT state, COUNT(*) AS n FROM notifications GROUP BY state").fetchall()
                return {r["state"]: r["n"] for r in rows}
        return _retry(op)

    def append_event(self, task_id: str, kind: str, detail: dict, *, run_id: str | None = None) -> None:
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    "INSERT INTO events(task_id, run_id, kind, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, run_id, kind, json.dumps(detail, ensure_ascii=False), now),
                )
        _retry(op)

    def list_events(self, task_id: str, *, limit: int = 200) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                rows = con.execute(
                    "SELECT id, run_id, kind, detail_json, created_at FROM events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
                return [
                    {"id": r["id"], "run_id": r["run_id"], "kind": r["kind"],
                     "detail": json.loads(r["detail_json"]), "created_at": r["created_at"]}
                    for r in reversed(rows)
                ]
        return _retry(op)

    def get_watchdog(self, task_id: str) -> tuple[dict | None, list[dict]]:
        def op():
            with self.tx(write=False) as con:
                st = con.execute("SELECT * FROM watchdog_state WHERE task_id = ?", (task_id,)).fetchone()
                alerts = con.execute(
                    "SELECT * FROM watchdog_alerts WHERE task_id = ? ORDER BY first_seen ASC", (task_id,)
                ).fetchall()
                state_dict = None
                if st is not None:
                    state_dict = dict(st)
                    state_dict["evidence_complete"] = bool(state_dict["evidence_complete"])
                    state_dict["metrics"] = json.loads(state_dict.pop("metrics_json")) if state_dict["metrics_json"] else None
                    state_dict["pending"] = json.loads(state_dict.pop("pending_json") or "{}")
                alert_list = []
                for a in alerts:
                    item = dict(a)
                    item["evidence"] = json.loads(item.pop("evidence_json"))
                    alert_list.append(item)
                return state_dict, alert_list
        return _retry(op)

    def save_watchdog(self, task_id: str, state: dict, alerts: list[dict]) -> None:
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    """INSERT INTO watchdog_state
                       (task_id, revision, run_id, status, sampled_at, sample_error, diff_error,
                        evidence_complete, last_progress_at, progress_hash, metrics_json, pending_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(task_id) DO UPDATE SET
                        revision=excluded.revision, run_id=excluded.run_id, status=excluded.status,
                        sampled_at=excluded.sampled_at, sample_error=excluded.sample_error,
                        diff_error=excluded.diff_error, evidence_complete=excluded.evidence_complete,
                        last_progress_at=excluded.last_progress_at, progress_hash=excluded.progress_hash,
                        metrics_json=excluded.metrics_json, pending_json=excluded.pending_json""",
                    (task_id, state.get("revision"), state.get("run_id"), state.get("status"),
                     state.get("sampled_at"), state.get("sample_error"), state.get("diff_error"),
                     1 if state.get("evidence_complete") else 0, state.get("last_progress_at"),
                     state.get("progress_hash"),
                     json.dumps(state["metrics"]) if state.get("metrics") is not None else None,
                     json.dumps(state.get("pending") or {})),
                )
                con.execute("DELETE FROM watchdog_alerts WHERE task_id = ?", (task_id,))
                for a in alerts:
                    con.execute(
                        """INSERT INTO watchdog_alerts
                           (id, task_id, run_id, code, state, evidence_json, first_seen, last_seen,
                            resolved_at, notification_state, notification_error)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (a["id"], task_id, a.get("run_id"), a["code"], a["state"],
                         json.dumps(a["evidence"], ensure_ascii=False), a["first_seen"], a["last_seen"],
                         a.get("resolved_at"), a.get("notification_state", "pending"),
                         a.get("notification_error")),
                    )
        _retry(op)
