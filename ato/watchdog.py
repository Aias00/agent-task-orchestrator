"""Advisory execution watchdog backed by SQLite, independent of task control and settlement."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from ato import config, executor, process, product, worktree
from ato.spec import TaskError, canonical_json, require, sha256_bytes
from ato.store import Store

INTERVAL = 60
SAMPLE_TIMEOUT = 120
STALE_SECONDS = 180
MAX_RECORD = 1024 * 1024
MAX_ALERTS = 64
WAITING = {"waiting_input"}
TERMINAL = {"completed", "stopped"}


def public_status(store: Store, task_id: str, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    try:
        task = store.get_task(task_id)
        rec, alerts = store.get_watchdog(task_id)
        if rec is None:
            return {"state": "not_observed", "alerts": []}
        sampled = rec.get("sampled_at")
        matches = rec.get("revision") == task["revision"] and rec.get("run_id") == task["active_run_id"]
        state = "observing"
        if sampled is None:
            state = "unavailable"
        elif not matches:
            state = "awaiting_sample"
        elif not (0 <= now - sampled <= STALE_SECONDS) or rec.get("sample_error"):
            state = "stale"
        pub_alerts = [
            {
                "id": a["id"],
                "code": a["code"],
                "state": a["state"],
                "run_id": a["run_id"],
                "first_seen": a["first_seen"],
                "last_seen": a["last_seen"],
                "resolved_at": a["resolved_at"],
                "evidence": deepcopy(a["evidence"]),
                "notification": {"state": a["notification_state"], "error": a["notification_error"]},
            }
            for a in alerts
        ]
        return {
            "state": state,
            "sampled_at": sampled,
            "sample_error": rec.get("sample_error"),
            "diff_error": rec.get("diff_error"),
            "alerts": pub_alerts,
            "metrics": rec.get("metrics"),
            "last_progress_at": rec.get("last_progress_at"),
            "evidence_complete": rec.get("evidence_complete", False),
        }
    except Exception:
        return {"state": "unavailable", "alerts": [], "sample_error": "MONITOR_RECORD_UNAVAILABLE"}


def collect(store: Store, task_id: str, *, now: float | None = None) -> dict:
    """Read-only observation sample executed in a disposable child process."""
    now = time.time() if now is None else now
    task = store.get_task(task_id)
    status = executor.task_status_view(store, task_id)
    diff = None
    diff_error = None
    if task["worktree_path"]:
        try:
            if task["worktree_state"] == "present":
                worktree.verify_worktree(task)
            diff = product.collect_diff(task)
        except (OSError, ValueError, TaskError, subprocess.SubprocessError):
            diff_error = "DIFF_UNAVAILABLE"
    latest = store.get_task(task_id)
    require(latest["revision"] == task["revision"] and latest["active_run_id"] == task["active_run_id"], "BUSY")
    complete = diff is not None and not diff["summary"]["truncated"]
    violations = product.check_guardrails(task, task["spec"], diff=diff) if diff else []
    stage = status.get("stage") or {}
    progress_obj = {
        "run": task["active_run_id"],
        "status": status["status"],
        "steps": stage.get("steps", []),
        "checks": status["checks"],
        "summary": diff["summary"] if diff else None,
    }
    return {
        "task_id": task_id,
        "revision": task["revision"],
        "run_id": task["active_run_id"],
        "sampled_at": now,
        "status": status["status"],
        "reason": status["reason"],
        "execution": status["execution"],
        "stage": stage,
        "run_history": status["run_history"],
        "created_at": task["created_at"],
        "evidence_complete": complete,
        "diff_error": diff_error,
        "violations": violations[:32],
        "progress": sha256_bytes(canonical_json(progress_obj)),
    }


def findings(sample: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    execution, stage, history = sample["execution"], sample["stage"], sample["run_history"]
    if execution["state"] == "unknown" and history and history[-1]["phase"] != "settled":
        found["EXECUTION_UNVERIFIED"] = {"run_id": execution["run_id"], "state": execution["state"]}
    repeated = [
        {"stage_id": s["stage_id"], "step_id": s["step_id"], "attempts": s["attempts"]}
        for s in stage.get("steps", [])
        if s["attempts"] >= 3 and s["state"] not in {"completed", "passed", "succeeded"}
    ]
    if repeated and sample["status"] not in TERMINAL:
        found["REPEATED_ATTEMPTS"] = {"steps": repeated[:32]}
    if sample["violations"]:
        found["DECLARED_SCOPE_CHANGED"] = {"violations": sample["violations"], "complete": sample["evidence_complete"]}
    remaining = stage.get("remaining_stage_ids", [])
    if sample["reason"] == "STAGES_INCOMPLETE" or (sample["status"] == "completed" and remaining):
        found["STAGES_INCOMPLETE"] = {"remaining_stage_ids": remaining[:32]}
    if sample["status"] == "blocked" and execution["state"] != "active":
        found["UNHANDLED_FAILURE"] = {"reason": sample["reason"], "next_action": "inspect_status"}
    return found


def reduce_sample(prev_state: dict | None, prev_alerts: list[dict], sample: dict) -> tuple[dict, list[dict]]:
    now = sample["sampled_at"]
    alerts = deepcopy(prev_alerts)
    prev = deepcopy(prev_state) if prev_state else {}
    same_run = prev.get("run_id") == sample["run_id"]
    consecutive = (
        same_run
        and prev.get("sample_error") not in {"SAMPLE_UNAVAILABLE", "SAMPLE_START_FAILED"}
        and prev.get("sampled_at") is not None
        and 0 <= now - prev["sampled_at"] <= STALE_SECONDS
    )
    candidates = findings(sample)
    prev_pending = prev.get("pending") or {}
    pending: dict[str, int] = {}
    for code in list(candidates):
        pending[code] = (prev_pending.get(code, 0) + 1) if consecutive else 1
        if code == "EXECUTION_UNVERIFIED" and pending[code] < 2:
            del candidates[code]

    active = {a["code"]: a for a in alerts if a["state"] == "active"}
    for code, row in active.items():
        if not same_run or (
            code not in candidates and not (code == "DECLARED_SCOPE_CHANGED" and not sample["evidence_complete"])
        ):
            if same_run and code == "EXECUTION_UNVERIFIED" and code in pending:
                continue
            row["state"] = "resolved"
            row["resolved_at"] = now

    for code, evidence in candidates.items():
        old = active.get(code)
        if same_run and old and old["state"] == "active":
            old["last_seen"] = now
            old["evidence"] = evidence
            continue
        alert_id = sha256_bytes(canonical_json([sample["task_id"], sample["run_id"], code, now]))[:32]
        historical = (
            (prev_state is None or prev.get("sampled_at") is None)
            and sample.get("baseline", False)
            and sample["status"] in (TERMINAL | {"blocked"})
        )
        alerts.append({
            "id": f"alert-{alert_id}",
            "task_id": sample["task_id"],
            "run_id": sample["run_id"],
            "code": code,
            "state": "active",
            "evidence": evidence,
            "first_seen": now,
            "last_seen": now,
            "resolved_at": None,
            "notification_state": "baseline_only" if historical else "pending",
            "notification_error": None,
        })

    retained = [a for a in alerts if a["state"] == "active" or a["notification_state"] == "pending"]
    archived = [a for a in alerts if a not in retained]
    slots = max(0, MAX_ALERTS - len(retained))
    alerts = ((archived[-slots:] if slots else []) + retained)[-MAX_ALERTS:]

    history = sample["run_history"]
    end = history[-1].get("settled_at") if sample["status"] in TERMINAL and history else None
    waiting = (prev.get("metrics") or {}).get("observed_wait_seconds", 0)
    if consecutive and now - prev["sampled_at"] <= 2 * INTERVAL and prev.get("status") in WAITING and sample["status"] in WAITING:
        waiting += now - prev["sampled_at"]
    metrics = {
        "wall_seconds": max(0, (end or now) - sample["created_at"]),
        "run_count": len(history),
        "resume_count": sum(r["kind"] == "resume" for r in history),
        "recheck_count": sum(r["kind"] == "recheck" for r in history),
        "extra_step_attempts": sum(max(0, s["attempts"] - 1) for s in sample["stage"].get("steps", [])),
        "kernel_execution_seconds": sample["stage"].get("elapsed_seconds"),
        "observed_wait_seconds": waiting,
        "efficiency_assessment": "insufficient_baseline",
    }
    last_progress = now if prev.get("progress_hash") != sample["progress"] else prev.get("last_progress_at")
    new_state = {
        "revision": sample["revision"],
        "run_id": sample["run_id"],
        "status": sample["status"],
        "sampled_at": now,
        "sample_error": None,
        "diff_error": sample["diff_error"],
        "evidence_complete": sample["evidence_complete"],
        "last_progress_at": last_progress,
        "progress_hash": sample["progress"],
        "metrics": metrics,
        "pending": pending,
    }
    return new_state, alerts


def sync_alert_notifications(store: Store, task_id: str) -> None:
    """Enqueue active watchdog alerts onto SQLite notifications table without blocking business notices."""
    task = store.get_task(task_id)
    state, alerts = store.get_watchdog(task_id)
    if state is None:
        return
    muted = config.is_repo_muted(task["repo_root"])
    fresh = (
        state.get("revision") == task["revision"]
        and state.get("sampled_at") is not None
        and 0 <= time.time() - state["sampled_at"] <= STALE_SECONDS
        and not state.get("sample_error")
    )
    changed = False
    for alert in alerts:
        if alert["notification_state"] in ("baseline_only", "sent", "cancelled", "unsupported", "muted"):
            continue
        if alert["state"] != "active":
            alert["notification_state"] = "cancelled"
            changed = True
            continue
        if muted:
            alert["notification_state"] = "muted"
            changed = True
            continue
        if not fresh:
            # Defer on transient revision bumps, backward NTP steps, or sample errors (P1-4).
            continue
        if task["origin_kind"] == "agy":
            alert["notification_state"] = "unsupported"
            changed = True
            continue
        if task["origin_kind"] != "codex" or not task["origin_session_id"]:
            alert["notification_state"] = "unavailable"
            changed = True
            continue
        notice = store.enqueue_notification(
            dedupe_key=f"watchdog:{alert['id']}",
            task_id=task_id,
            kind="watchdog",
            target_session=task["origin_session_id"],
            body=f"ATO 任务 {task_id} 监控提示 {alert['code']}。请用 ato status {task_id} 核对证据与最新状态。",
            initial_state="pending",
        )
        if alert["notification_state"] != notice["state"]:
            alert["notification_state"] = notice["state"]
            alert["notification_error"] = notice["error"]
            changed = True
    if changed:
        store.save_watchdog(task_id, state, alerts)


class Sampler:
    """One bounded read-only sampler child at a time; skips unchanged settled terminal tasks (P1-3)."""

    def __init__(self, store: Store, *, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.due: dict[str, float] = {}
        self.terminal_scope: dict[str, tuple] = {}
        self.job: dict | None = None
        self.last_error: str | None = None
        self.started_at = time.time()

    def _close_job(self, *, kill: bool = False) -> None:
        job = self.job
        if job is None:
            return
        try:
            proc = job["process"]
            reaped = isinstance(proc, subprocess.Popen) and proc.returncode is not None
            if kill and not reaped:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if not reaped:
                proc.wait()
        finally:
            try:
                job["output"].close()
            finally:
                self.job = None

    def close(self) -> None:
        if self.job is not None:
            self._close_job(kill=True)

    def _is_settled_terminal(self, state: dict | None, alerts: list[dict]) -> bool:
        return (
            state is not None
            and state.get("status") in TERMINAL
            and state.get("sample_error") is None
            and all(a["state"] != "active" and a["notification_state"] != "pending" for a in alerts)
        )

    def _start_next(self, now: float) -> None:
        if self.job is not None:
            return
        tasks = self.store.list_tasks()
        live = {t["id"] for t in tasks}
        self.due = {k: v for k, v in self.due.items() if k in live}
        self.terminal_scope = {k: v for k, v in self.terminal_scope.items() if k in live}

        ready: list[tuple[int, float, str]] = []
        for t in tasks:
            tid = t["id"]
            key = (t["revision"], t["active_run_id"], t["status"])
            if self.terminal_scope.get(tid) == key:
                continue
            self.terminal_scope.pop(tid, None)
            state, alerts = self.store.get_watchdog(tid)
            if self._is_settled_terminal(state, alerts) and (state["revision"], state["run_id"], state["status"]) == key:
                self.terminal_scope[tid] = key
                continue
            due_at = self.due.get(tid, 0.0)
            if due_at <= now:
                ready.append((1 if t["status"] in TERMINAL else 0, due_at, tid))
        ready.sort()

        for _, _, tid in ready:
            self.due[tid] = now + INTERVAL
            output = tempfile.TemporaryFile()
            env = dict(os.environ, ATO_HOME=str(config.ato_home()), CODEX_HOME=str(config.codex_home()))
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "ato.watchdog", "_sample", tid, str(self.store.db_file)],
                    stdout=output, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    env=env, start_new_session=True,
                )
            except OSError:
                output.close()
                self.last_error = "SAMPLE_START_FAILED"
                self._record_failure(tid, self.last_error)
                return
            self.job = {"process": proc, "output": output, "task_id": tid, "started": now}
            return

    def _record_failure(self, task_id: str, code: str) -> None:
        prev, alerts = self.store.get_watchdog(task_id)
        state = prev or {"sampled_at": None, "evidence_complete": False}
        state["sample_error"] = code
        state["pending"] = {}
        self.store.save_watchdog(task_id, state, alerts)

    def poll(self) -> str | None:
        now = self.clock()
        if self.job is not None:
            job = self.job
            proc = job["process"]
            exited = process.probe_exited(proc)
            if not exited and now - job["started"] < SAMPLE_TIMEOUT:
                return None
            tid = job["task_id"]
            retry_delay = INTERVAL
            try:
                if exited:
                    if isinstance(proc, subprocess.Popen) and proc.returncode is None:
                        # Sweep lingering group descendants while leader is still an unreaped zombie (P0-1).
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                        rc = proc.wait()
                    else:
                        rc = proc.poll()
                else:
                    rc = None
                require(rc == 0, "SAMPLE_FAILED")
                job["output"].seek(0)
                encoded = job["output"].read(MAX_RECORD + 1)
                require(len(encoded) <= MAX_RECORD, "STATE_CORRUPT")
                sample = json.loads(encoded)
                require(isinstance(sample, dict) and sample.get("task_id") == tid, "STATE_CORRUPT")
                task = self.store.get_task(tid)
                require(sample["revision"] == task["revision"] and sample["run_id"] == task["active_run_id"], "BUSY")
                sample["baseline"] = task["created_at"] < self.started_at
                prev_state, prev_alerts = self.store.get_watchdog(tid)
                new_state, new_alerts = reduce_sample(prev_state, prev_alerts, sample)
                self.store.save_watchdog(tid, new_state, new_alerts)
                if self._is_settled_terminal(new_state, new_alerts):
                    self.terminal_scope[tid] = (new_state["revision"], new_state["run_id"], new_state["status"])
                self.last_error = None
            except Exception as exc:
                if getattr(exc, "code", None) == "BUSY":
                    self.last_error = "BUSY"
                    retry_delay = 5
                else:
                    self.last_error = "SAMPLE_UNAVAILABLE"
                    self._record_failure(tid, self.last_error)
            finally:
                self._close_job(kill=not exited)
                self.due[tid] = now + retry_delay
            self._start_next(now)
            return tid
        self._start_next(now)
        return None


def _main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "_sample":
        _, _, task_id, db_file = sys.argv
        store = Store(Path(db_file))
        sample = collect(store, task_id)
        encoded = json.dumps(sample, ensure_ascii=False)
        require(len(encoded.encode("utf-8")) <= MAX_RECORD, "STATE_CORRUPT")
        print(encoded)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
