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

SCHEMA_VERSION = 3

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
  reviewer_sources_json TEXT,
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
  worker_timed_out INTEGER,
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
  attempt INTEGER,
  diagnostic_json TEXT,
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
  product_id TEXT,
  reviewer_sha256 TEXT,
  session_id TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS review_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  round INTEGER NOT NULL,
  product_id TEXT NOT NULL,
  product_fingerprint TEXT NOT NULL,
  config_sha256 TEXT NOT NULL,
  policy TEXT NOT NULL CHECK(policy = 'all'),
  state TEXT NOT NULL DEFAULT 'running',
  verdict_json TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  finished_at REAL,
  UNIQUE(run_id, round)
);
CREATE INDEX IF NOT EXISTS idx_review_groups_task ON review_groups(task_id, id);
CREATE TABLE IF NOT EXISTS review_members (
  group_id INTEGER NOT NULL REFERENCES review_groups(id) ON DELETE CASCADE,
  member_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  config_sha256 TEXT NOT NULL,
  config_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  verdict_json TEXT,
  error TEXT,
  session_id TEXT,
  started_at REAL,
  finished_at REAL,
  PRIMARY KEY(group_id, member_id),
  UNIQUE(group_id, position)
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
  member_id TEXT NOT NULL DEFAULT '',
  UNIQUE (run_id, round, member_id)
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
                    ("tasks", (("spec_timeout_source", "TEXT"), ("reviewer_sources_json", "TEXT"))),
                    ("runs", (("timeout_seconds", "INTEGER"), ("timeout_source", "TEXT"),
                              ("child_pid", "INTEGER"), ("child_pid_identity", "TEXT"),
                              ("worker_timed_out", "INTEGER"))),
                    ("checks", (("attempt", "INTEGER"), ("diagnostic_json", "TEXT"))),
                    ("reviews", (("product_id", "TEXT"), ("reviewer_sha256", "TEXT"))),
                ):
                    columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                    for name, sql_type in additions:
                        if name not in columns:
                            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                # Rebuild the old ACP uniqueness constraint atomically. No live DB
                # is opened by development tooling; this runs on a future Store open.
                if "member_id" not in {r["name"] for r in con.execute("PRAGMA table_info(acpx_reviews)")}:
                    con.execute("ALTER TABLE acpx_reviews RENAME TO acpx_reviews_legacy")
                    start = _DDL.index("CREATE TABLE IF NOT EXISTS acpx_reviews (")
                    end = _DDL.index(";", start)
                    con.execute(_DDL[start:end])
                    columns = [r["name"] for r in con.execute("PRAGMA table_info(acpx_reviews_legacy)")]
                    names = ", ".join(columns)
                    con.execute(f"INSERT INTO acpx_reviews ({names}, member_id) SELECT {names}, '' FROM acpx_reviews_legacy ORDER BY rowid")
                    con.execute("DROP TABLE acpx_reviews_legacy")
                    con.execute("CREATE INDEX idx_acpx_reviews_task ON acpx_reviews(task_id)")
                con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_checks_attempt
                               ON checks(task_id, run_id, attempt, criterion_id) WHERE attempt IS NOT NULL""")
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
                    spec_timeout_source: str | None = None, reviewer_sources: dict | None = None) -> dict:
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    """INSERT INTO tasks(id, repo_root, repo_key, base_commit, branch, worktree_path,
                       worktree_state, spec_json, spec_sha256, origin_kind, origin_session_id,
                       status, reason, stop_requested, revision, reviewer_json, created_at, updated_at,
                       spec_timeout_source, reviewer_sources_json)
                       VALUES (?, ?, ?, ?, ?, ?, 'present', ?, ?, ?, ?, 'queued', NULL, 0, 1, ?, ?, ?, ?, ?)""",
                    (task_id, repo_root, repo_key, base_commit, branch, worktree_path,
                     json.dumps(spec_doc, ensure_ascii=False), spec_sha256, origin_kind, origin_session_id,
                     json.dumps(spec_doc["reviewer"]) if spec_doc.get("reviewer") else None, now, now,
                     spec_timeout_source, json.dumps(reviewer_sources) if reviewer_sources else None),
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
        d["reviewer_sources"] = json.loads(d.pop("reviewer_sources_json") or "{}")
        d["stop_requested"] = bool(d["stop_requested"])
        d["legacy_advisory_completion"] = d["status"] == "completed" and d["reason"] == "CHECKS_FAILED"
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
                    state = "stop_requested = 0, status = 'running', reason = NULL, next_step = NULL, stage_json = NULL,"
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

    @classmethod
    def _acpx_member_owned(cls, con, row) -> bool:
        if not cls._acpx_owner(con, row["task_id"], row["run_id"]):
            return False
        if not row["member_id"]:
            return True
        return con.execute("""SELECT 1 FROM review_groups g JOIN review_members m ON m.group_id = g.id
                              WHERE g.task_id = ? AND g.run_id = ? AND g.round = ? AND g.state = 'running'
                                AND m.member_id = ? AND m.state = 'running'""",
                           (row["task_id"], row["run_id"], row["round"], row["member_id"])).fetchone() is not None

    def begin_acpx_review(self, task_id: str, run_id: str, round_num: int, request_id: str, bridge: dict, *, member_id: str = "") -> bool:
        with self.tx(admission=True) as con:
            if not self._acpx_owner(con, task_id, run_id):
                return False
            cfg = json.loads(con.execute("SELECT reviewer_json FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] or "null")
            if bool(cfg and "members" in cfg) != bool(member_id):
                return False
            if member_id and not con.execute("""SELECT 1 FROM review_groups g JOIN review_members m ON m.group_id = g.id
                    WHERE g.task_id = ? AND g.run_id = ? AND g.round = ? AND g.state = 'running'
                    AND m.member_id = ? AND m.state = 'running'""", (task_id, run_id, round_num, member_id)).fetchone():
                return False
            if con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                           (task_id,)).fetchone():
                return False
            con.execute("INSERT INTO acpx_reviews(request_id, task_id, run_id, round, bridge_json, member_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (request_id, task_id, run_id, round_num, json.dumps(bridge), member_id))
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
                    or not self._acpx_member_owned(con, row)):
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
                    and self._acpx_member_owned(con, row)):
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

    def acpx_review(self, task_id: str, *, unresolved: bool = False, request_id: str | None = None) -> dict | None:
        with self.tx(write=False) as con:
            where = f"AND {_ACPX_UNRESOLVED}" if unresolved else ""
            if request_id is not None:
                where += " AND request_id = ?"
            row = con.execute(f"SELECT * FROM acpx_reviews WHERE task_id = ? {where} ORDER BY rowid DESC LIMIT 1",
                              (task_id, request_id) if request_id is not None else (task_id,)).fetchone()
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
            self._require_panel_approval(con, task_id, run_id, product)
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

    def settle_owned_run(self, task_id: str, run_id: str, *, reason: str | None,
                         product: dict | None, next_step: str | None, stage: dict | None = None,
                         worker_timeout: dict | None = None, expected_revision: int | None = None,
                         acpx: bool = False, notice: dict | None = None) -> bool:
        """Settle a result without overwriting STOP or another controller's Run."""
        now = time.time()

        def op():
            with self.tx() as con:
                row = con.execute(
                    """SELECT t.revision FROM tasks t JOIN runs r ON r.id = t.active_run_id AND r.task_id = t.id
                       WHERE t.id = ? AND r.id = ? AND r.phase = 'running'
                         AND t.stop_requested = 0 AND t.worktree_state = 'present'""",
                    (task_id, run_id),
                ).fetchone()
                if row is None:
                    return False
                require(expected_revision is None or row["revision"] == expected_revision, "BUSY", task_id)
                cfg = json.loads(con.execute("SELECT reviewer_json FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] or "null")
                if cfg and "members" in cfg and not self._acpx_owner(con, task_id, run_id):
                    return False
                if acpx:
                    if not self._acpx_owner(con, task_id, run_id):
                        return False
                    require(con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                                        (task_id,)).fetchone() is None, "CLEANUP_BLOCKED", task_id)
                if reason is None:
                    self._require_panel_approval(con, task_id, run_id, product)
                status = "blocked" if reason else "completed"
                con.execute("""UPDATE tasks SET status = ?, reason = ?, product_json = ?,
                               next_step = ?, stage_json = ?, revision = revision + 1, updated_at = ?
                               WHERE id = ?""",
                            (status, reason, json.dumps(product) if product is not None else None, next_step,
                             json.dumps(stage) if stage is not None else None, now, task_id))
                con.execute("UPDATE runs SET phase = 'settled', outcome = ?, error_code = ?, settled_at = ? WHERE id = ?",
                            (status, reason, now, run_id))
                if worker_timeout is not None:
                    con.execute("""UPDATE runs SET worker_timed_out = 1, timeout_seconds = ?, timeout_source = ?
                                   WHERE id = ?""",
                                (worker_timeout["timeout_seconds"], worker_timeout["timeout_source"], run_id))
                if notice is not None:
                    self._insert_notification(con, **notice)
                return True

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
                rows = con.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY rowid", (task_id,)).fetchall()
                return [dict(r) for r in rows]
        return _retry(op)

    def save_checks(self, task_id: str, run_id: str, rows: list[dict], *, attempt: int, passed: bool) -> None:
        now = time.time()
        detail = {"attempt": attempt, "passed": passed,
                  "failed_criteria": sorted(r["criterion_id"] for r in rows if r["state"] != "passed")}
        receipts = [(r["criterion_id"], r["state"], r["receipt_id"], json.dumps(r.get("diagnostic"), sort_keys=True))
                    for r in rows]

        def op():
            with self.tx(write=True) as con:
                existing = con.execute(
                    """SELECT detail_json FROM events WHERE task_id = ? AND run_id = ? AND kind = 'checks_finished'
                       AND json_extract(detail_json, '$.attempt') = ?
                       AND json_type(detail_json, '$.failed_criteria') = 'array'""",
                    (task_id, run_id, attempt),
                ).fetchone()
                if existing:
                    saved = con.execute(
                        """SELECT criterion_id, state, receipt_id, diagnostic_json FROM checks
                           WHERE task_id = ? AND run_id = ? AND attempt = ? ORDER BY id""",
                        (task_id, run_id, attempt),
                    )
                    require(json.loads(existing["detail_json"]) == detail
                            and [tuple(row) for row in saved] == receipts, "STATE_CORRUPT", task_id)
                    return
                con.executemany(
                    """INSERT INTO checks(task_id, run_id, attempt, criterion_id, state, receipt_id,
                                          diagnostic_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(task_id, run_id, attempt, *receipt, now) for receipt in receipts],
                )
                con.execute(
                    "INSERT INTO events(task_id, run_id, kind, detail_json, created_at) VALUES (?, ?, 'checks_finished', ?, ?)",
                    (task_id, run_id, json.dumps(detail), now),
                )
        _retry(op)

    def latest_checks(self, task_id: str, run_id: str | None = None) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                rows = con.execute(
                    """WITH latest AS (
                           SELECT c.run_id, c.attempt FROM checks c
                           LEFT JOIN runs r ON r.id = c.run_id AND r.task_id = c.task_id
                           WHERE c.task_id = ? AND (? IS NULL OR c.run_id = ?)
                           ORDER BY r.rowid DESC, c.attempt DESC, c.id DESC LIMIT 1
                       )
                       SELECT c.criterion_id, c.state, c.receipt_id FROM checks c JOIN latest l
                         ON c.run_id = l.run_id AND c.attempt IS l.attempt
                       WHERE c.task_id = ? ORDER BY c.id""", (task_id, run_id, run_id, task_id),
                )
                return [dict(row) for row in rows]
        return _retry(op)

    def check_history(self, task_id: str) -> list[dict]:
        def op():
            with self.tx(write=False) as con:
                rows = con.execute(
                    """SELECT c.run_id, c.attempt, c.criterion_id, c.state, c.receipt_id, c.diagnostic_json
                       FROM checks c LEFT JOIN runs r ON r.id = c.run_id AND r.task_id = c.task_id
                       WHERE c.task_id = ? ORDER BY r.rowid, c.attempt, c.id""", (task_id,),
                )
                rounds: dict[tuple[str, int | None], list[dict]] = {}
                for row in rows:
                    check = dict(row)
                    identity = (check.pop("run_id"), check.pop("attempt"))
                    diagnostic = check.pop("diagnostic_json")
                    check["diagnostic"] = json.loads(diagnostic) if diagnostic else None
                    rounds.setdefault(identity, []).append(check)
                return [{"run_id": run_id, "attempt": attempt, "checks": checks}
                        for (run_id, attempt), checks in rounds.items()]
        return _retry(op)

    def settle_panel_stop(self, task_id: str, run_id: str, stage: dict | None = None) -> None:
        from ato import process
        with self.tx() as con:
            row = con.execute("""SELECT r.pid, r.pid_identity FROM runs r JOIN tasks t ON t.active_run_id = r.id
                               WHERE t.id = ? AND r.id = ? AND r.phase = 'running'""", (task_id, run_id)).fetchone()
            if not row or row["pid"] != os.getpid() or process.identity(os.getpid()) != row["pid_identity"]:
                return
            pending = con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                                  (task_id,)).fetchone()
            state, reason = ("blocked", "CLEANUP_BLOCKED") if pending else ("stopped", "STOPPED")
            now = time.time()
            con.execute("UPDATE runs SET phase = 'settled', outcome = ?, error_code = ?, settled_at = ? WHERE id = ?",
                        (state, reason, now, run_id))
            con.execute("""UPDATE tasks SET status = ?, reason = ?, stage_json = ?, revision = revision + 1,
                           updated_at = ? WHERE id = ?""", (state, reason, json.dumps(stage), now, task_id))

    def set_reviewer(self, task_id: str, reviewer: dict, *, expected_revision: int) -> None:
        """CAS under the same worktree lease as run admission and cleanup."""
        from ato.executor import _ensure_idle
        with self._worktree_lease(task_id):
            task = self.get_task(task_id)
            _ensure_idle(self, task)
            with self.tx() as con:
                require(con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                                    (task_id,)).fetchone() is None, "CLEANUP_BLOCKED", task_id)
                invalidate = task["reviewer"] != reviewer and task["status"] == "completed"
                changed = con.execute("""UPDATE tasks SET reviewer_json = ?, revision = revision + 1, updated_at = ?,
                                         status = CASE WHEN ? THEN 'blocked' ELSE status END,
                                         reason = CASE WHEN ? THEN 'REVIEW_REQUIRED' ELSE reason END,
                                         next_step = CASE WHEN ? THEN ? ELSE next_step END
                                         WHERE id = ? AND revision = ?""",
                                      (json.dumps(reviewer), time.time(), invalidate, invalidate, invalidate,
                                       f"Run ato project recheck {task_id} for the changed reviewer configuration.",
                                       task_id, expected_revision)).rowcount
                require(changed == 1, "BUSY", task_id)

    @staticmethod
    def _decode_group(row, members) -> dict:
        group = dict(row)
        group["verdict"] = json.loads(group.pop("verdict_json") or "null")
        group["members"] = []
        for row in members:
            member = dict(row)
            member["verdict"] = json.loads(member.pop("verdict_json") or "null")
            member["config"] = json.loads(member.pop("config_json"))
            group["members"].append(member)
        return group

    def review_group(self, task_id: str, group_id: int | None = None) -> dict | None:
        with self.tx(write=False) as con:
            row = con.execute("SELECT * FROM review_groups WHERE task_id = ? " +
                              ("AND id = ? " if group_id is not None else "") + "ORDER BY id DESC LIMIT 1",
                              (task_id, group_id) if group_id is not None else (task_id,)).fetchone()
            if row is None:
                return None
            group = self._decode_group(row, con.execute(
                "SELECT * FROM review_members WHERE group_id = ? ORDER BY position", (row["id"],)).fetchall())
            acpx_rows = con.execute("SELECT * FROM acpx_reviews WHERE task_id = ? AND run_id = ? AND round = ?",
                                    (task_id, row["run_id"], row["round"])).fetchall()
            by_member = {r["member_id"]: r for r in acpx_rows}
            for member in group["members"]:
                acpx = by_member.get(member["member_id"])
                if acpx:
                    receipt = json.loads(acpx["receipt_json"] or "{}")
                    member["acpx"] = {"request_id": acpx["request_id"],
                                      "dispatch_recorded": acpx["dispatch_json"] is not None,
                                      "delivery": receipt.get("delivery", "unknown" if acpx["dispatch_json"] else "not_dispatched"),
                                      "cleanup": acpx["cleanup"], "bridge_closed": bool(acpx["bridge_closed"]),
                                      **{k: receipt.get(k) for k in ("transport", "cancel_requested", "cancel_written", "cancel_acknowledged")}}
            return group

    def review_group_ids(self, task_id: str, *, cursor: int, limit: int) -> tuple[list[int], int]:
        with self.tx(write=False) as con:
            rows = con.execute("SELECT id FROM review_groups WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
                               (task_id, cursor, limit)).fetchall()
            total = con.execute("SELECT COUNT(*) FROM review_groups WHERE task_id = ?", (task_id,)).fetchone()[0]
            return [r["id"] for r in rows], total

    def begin_review_group(self, task_id: str, run_id: str, round_num: int, *, product_id: str,
                           product_fingerprint: str, config_sha256: str, members: list[dict]) -> int:
        from ato.spec import canonical_json, sha256_bytes
        with self.tx(admission=True) as con:
            require(self._acpx_owner(con, task_id, run_id), "STOPPED", task_id)
            require(con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                                (task_id,)).fetchone() is None, "CLEANUP_BLOCKED", task_id)
            cur = con.execute("""INSERT INTO review_groups(task_id, run_id, round, product_id,
                                 product_fingerprint, config_sha256, policy, created_at)
                                 VALUES (?, ?, ?, ?, ?, ?, 'all', ?)""",
                              (task_id, run_id, round_num, product_id, product_fingerprint, config_sha256, time.time()))
            group_id = cur.lastrowid
            for position, member in enumerate(members):
                con.execute("""INSERT INTO review_members(group_id, member_id, position, config_sha256, config_json)
                               VALUES (?, ?, ?, ?, ?)""",
                            (group_id, member["id"], position, sha256_bytes(canonical_json(member)), json.dumps(member)))
            return group_id

    def review_group_owned(self, task_id: str, group_id: int) -> bool:
        from ato.spec import canonical_json, sha256_bytes
        with self.tx(write=False) as con:
            group = con.execute("SELECT * FROM review_groups WHERE id = ? AND task_id = ?",
                                (group_id, task_id)).fetchone()
            task = con.execute("SELECT reviewer_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return bool(group and group["state"] == "running" and self._acpx_owner(con, task_id, group["run_id"])
                        and sha256_bytes(canonical_json(json.loads(task["reviewer_json"]))) == group["config_sha256"])

    def start_review_member(self, task_id: str, group_id: int, member_id: str) -> bool:
        with self.tx(admission=True) as con:
            group = con.execute("SELECT * FROM review_groups WHERE id = ? AND task_id = ?",
                                (group_id, task_id)).fetchone()
            if not group or group["state"] != "running" or not self._acpx_owner(con, task_id, group["run_id"]):
                return False
            return con.execute("""UPDATE review_members SET state = 'running', started_at = ?
                                  WHERE group_id = ? AND member_id = ? AND state = 'pending'""",
                               (time.time(), group_id, member_id)).rowcount == 1

    def save_review_member(self, task_id: str, group_id: int, member_id: str, result) -> bool:
        with self.tx(admission=True) as con:
            group = con.execute("SELECT * FROM review_groups WHERE id = ? AND task_id = ?",
                                (group_id, task_id)).fetchone()
            if not group or group["state"] != "running" or not self._acpx_owner(con, task_id, group["run_id"]):
                return False
            return con.execute("""UPDATE review_members SET state = ?, verdict_json = ?, error = ?, session_id = ?,
                                  finished_at = ? WHERE group_id = ? AND member_id = ? AND state = 'running'""",
                               ("completed" if result.ok else "error", json.dumps(result.verdict) if result.verdict else None,
                                None if result.ok else result.code, result.session_id, time.time(), group_id, member_id)).rowcount == 1

    def interrupt_review_group(self, task_id: str, group_id: int, code: str, *, member_id=None, result=None) -> None:
        # Evidence only: never settle a task, grant approval, or overwrite completed members.
        with self.tx(admission=True) as con:
            row = con.execute("SELECT state FROM review_groups WHERE id = ? AND task_id = ?",
                              (group_id, task_id)).fetchone()
            if row is None or row["state"] != "running":
                return
            now = time.time()
            if member_id is not None and result is not None:
                from ato.adapters.base import has_secret, secret_values
                # Store a late receipt as unaccepted evidence only. Never revive a group.
                verdict = result.verdict if not has_secret(result.verdict, secret_values()) else None
                con.execute("""UPDATE review_members SET verdict_json = ?, session_id = ?
                               WHERE group_id = ? AND member_id = ? AND state = 'running'""",
                            (json.dumps(verdict) if verdict else None, result.session_id, group_id, member_id))
            con.execute("UPDATE review_groups SET state = 'incomplete', error = ?, finished_at = ? WHERE id = ?",
                        (code, now, group_id))
            con.execute("""UPDATE review_members SET state = CASE WHEN state = 'pending' THEN 'not_run' ELSE 'error' END,
                           error = ?, finished_at = ? WHERE group_id = ? AND state IN ('pending', 'running')""",
                        (code, now, group_id))

    def finish_review_group(self, task_id: str, group_id: int) -> dict | None:
        from ato.joint_review import aggregate, effective_member
        from ato.spec import canonical_json, sha256_bytes
        with self.tx(admission=True) as con:
            row = con.execute("SELECT * FROM review_groups WHERE id = ? AND task_id = ?", (group_id, task_id)).fetchone()
            if row is None or row["state"] != "running" or not self._acpx_owner(con, task_id, row["run_id"]):
                return None
            cfg = con.execute("SELECT reviewer_json FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
            require(sha256_bytes(canonical_json(json.loads(cfg))) == row["config_sha256"], "BUSY", task_id)
            require(con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                                (task_id,)).fetchone() is None, "CLEANUP_BLOCKED", task_id)
            group = self._decode_group(row, con.execute(
                "SELECT * FROM review_members WHERE group_id = ? ORDER BY position", (group_id,)).fetchall())
            configured = json.loads(cfg)
            require(len(group["members"]) == len(configured["members"]) and all(
                m["state"] == "completed" and m["member_id"] == configured["members"][i]["id"]
                and m["position"] == i and m["config_sha256"] == sha256_bytes(canonical_json(
                    effective_member(configured, configured["members"][i])))
                and m["config_sha256"] == sha256_bytes(canonical_json(m["config"]))
                for i, m in enumerate(group["members"])), "INVALID_EVIDENCE", task_id)
            verdict = aggregate(group["members"])
            now = time.time()
            con.execute("UPDATE review_groups SET state = 'completed', verdict_json = ?, finished_at = ? WHERE id = ?",
                        (json.dumps(verdict), now, group_id))
            con.execute("""INSERT INTO reviews(task_id, run_id, round, reviewer_kind, verdict, verdict_json,
                           product_id, reviewer_sha256, created_at) VALUES (?, ?, ?, 'panel', ?, ?, ?, ?, ?)""",
                        (task_id, row["run_id"], row["round"], verdict["verdict"], json.dumps(verdict),
                         row["product_id"], row["config_sha256"], now))
            return verdict

    @staticmethod
    def _require_panel_approval(con, task_id: str, run_id: str, product: dict | None) -> None:
        from ato.spec import canonical_json, sha256_bytes
        cfg = json.loads(con.execute("SELECT reviewer_json FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] or "null")
        if not cfg or "members" not in cfg:
            return
        task = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        from ato.worktree import tree_fingerprint, shared_links_for
        task_doc = {**dict(task), "spec": json.loads(task["spec_json"])}
        require(product is not None and product["product_id"] == "product-" + tree_fingerprint(
                    task["worktree_path"], ignored=shared_links_for(task_doc)), "PRODUCT_CHANGED", task_id)
        require(Store._acpx_owner(con, task_id, run_id), "STOPPED", task_id)
        require(con.execute(f"SELECT 1 FROM acpx_reviews WHERE task_id = ? AND {_ACPX_UNRESOLVED}",
                            (task_id,)).fetchone() is None, "CLEANUP_BLOCKED", task_id)
        group = con.execute("""SELECT g.*, r.outcome FROM review_groups g JOIN runs r ON r.id = g.run_id
                               WHERE g.task_id = ? ORDER BY g.id DESC LIMIT 1""", (task_id,)).fetchone()
        require(group is not None and group["state"] == "completed" and product is not None
                and group["product_id"] == product["product_id"]
                and group["config_sha256"] == sha256_bytes(canonical_json(cfg))
                and json.loads(group["verdict_json"])["verdict"] == "approve"
                and (group["run_id"] == run_id or group["outcome"] == "completed"), "INVALID_EVIDENCE", task_id)
        from ato.joint_review import effective_member
        members = con.execute("SELECT * FROM review_members WHERE group_id = ? ORDER BY position", (group["id"],)).fetchall()
        require(len(members) == len(cfg["members"]) and all(
                m["state"] == "completed" and json.loads(m["verdict_json"])["verdict"] == "approve"
                and m["member_id"] == cfg["members"][i]["id"] and m["position"] == i
                and m["config_sha256"] == sha256_bytes(canonical_json(effective_member(cfg, cfg["members"][i])))
                and m["config_sha256"] == sha256_bytes(canonical_json(json.loads(m["config_json"])))
                for i, m in enumerate(members)), "INVALID_EVIDENCE", task_id)

    def add_review(self, task_id: str, run_id: str, round_num: int, reviewer_kind: str,
                   verdict: dict, session_id: str | None = None, *, product_id: str,
                   reviewer_sha256: str) -> None:
        now = time.time()
        def op():
            with self.tx(write=True) as con:
                con.execute(
                    """INSERT INTO reviews(task_id, run_id, round, reviewer_kind, verdict, verdict_json,
                                            session_id, product_id, reviewer_sha256, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, run_id, round_num, reviewer_kind, verdict["verdict"],
                     json.dumps(verdict, ensure_ascii=False), session_id, product_id, reviewer_sha256, now),
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

    @staticmethod
    def _insert_notification(con, *, dedupe_key: str, task_id: str, kind: str,
                             target_session: str | None, body: str, initial_state: str = "pending") -> None:
        now = time.time()
        con.execute(
            """INSERT OR IGNORE INTO notifications
               (id, dedupe_key, task_id, kind, target_session, body, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id("notice"), dedupe_key, task_id, kind, target_session, body, initial_state, now, now),
        )

    def enqueue_notification(self, *, dedupe_key: str, task_id: str, kind: str,
                             target_session: str | None, body: str, initial_state: str = "pending") -> dict:
        def op():
            with self.tx(write=True) as con:
                self._insert_notification(con, dedupe_key=dedupe_key, task_id=task_id, kind=kind,
                                          target_session=target_session, body=body, initial_state=initial_state)
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
