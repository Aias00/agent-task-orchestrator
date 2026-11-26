"""Advisory execution observations, independent of Task control and settlement."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import ato_product
import ato_config
import notification_state
from message_bus import MailboxError
from ato_task_store import _identifier, _read_json, _require, _save, _sha, canonical_json

INTERVAL = 60
SAMPLE_TIMEOUT = 120
STALE_SECONDS = 180
MAX_RECORD = 1024 * 1024
MAX_ALERTS = 64
WAITING = {"waiting", "needs_input", "needs_host"}
TERMINAL = {"completed", "stopped"}


def scope(task, binding):
    return dict(revision=task["revision"], run_id=task["run_id"], stop_epoch=task["stop_epoch"],
                binding_revision=binding["revision"], spec_sha256=task["spec_sha256"],
                runtime_seal=task["creation"]["runtime_seal"])


def read(store, task_id):
    _identifier(task_id, "task")
    try:
        with store._private(store.root / "watchdog") as fd:
            record = _read_json(fd, task_id + ".json")
    except FileNotFoundError:
        return None
    _require(isinstance(record, dict) and record.get("version") == 1 and record.get("task_id") == task_id
             and isinstance(record.get("alerts"), list), "STATE_CORRUPT")
    return record


def save(store, record):
    _identifier(record["task_id"], "task")
    with store._private(store.root / "watchdog", create=True) as fd:
        _save(fd, record["task_id"] + ".json", record)


def public_status(store, task_id, *, now=None):
    now = time.time() if now is None else now
    try:
        task = store.load_task(task_id)
        record = read(store, task_id)
        if record is None:
            return dict(state="not_observed", alerts=[])
        sampled = record.get("sampled_at")
        current = record.get("scope", {})
        matches = all(current.get(key) == value for key, value in (
            ("run_id", task["run_id"]), ("stop_epoch", task["stop_epoch"]),
            ("revision", task["revision"]), ("runtime_seal", task["creation"]["runtime_seal"])))
        state = "observing"
        if sampled is None:
            state = "unavailable"
        elif not matches:
            state = "awaiting_sample"
        elif not 0 <= now - sampled <= STALE_SECONDS or record.get("sample_error"):
            state = "stale"
        alerts = []
        for row in record["alerts"]:
            item = {key: deepcopy(row[key]) for key in
                    ("id", "code", "state", "first_seen", "last_seen", "resolved_at", "evidence")}
            item["run_id"] = row["scope"]["run_id"]
            item["notification"] = {key: value for key, value in row["notification"].items()
                                    if key in {"state", "error"}}
            alerts.append(item)
        return dict(state=state, sampled_at=sampled, sample_error=record.get("sample_error"),
                    alerts=alerts, metrics=record.get("metrics"),
                    last_progress_at=record.get("last_progress_at"),
                    evidence_complete=record.get("evidence_complete", False))
    except (OSError, ValueError, KeyError, TypeError):
        return dict(state="unavailable", alerts=[], sample_error="MONITOR_RECORD_UNAVAILABLE")


def collect(host, task_id, *, now=None):
    """Run inside a disposable read-only child; never send notifications here."""
    now = time.time() if now is None else now
    service = host.interaction
    task, spec, binding = service._task(task_id)
    status, generation, _ = service._view(task, spec)
    diff = None
    diff_error = None
    if task["worktree"] is not None and task["creation"]["phase"] == "ready":
        try:
            if task["worktree_removed"]:
                host.store._repo_matches(task)
            else:
                host.store._verify_worktree(task)
            diff = ato_product.collect_diff(task)
        except (OSError, ValueError, subprocess.SubprocessError):
            diff_error = "DIFF_UNAVAILABLE"
    latest, _, current = service._task(task_id)
    _require(scope(latest, current) == scope(task, binding), "BUSY")
    complete = diff is not None and not diff["summary"]["truncated"]
    violations = ato_product.check_guardrails(task, spec, None, diff={
        row["path"]: row["status"] for row in diff["files"]}) if diff else []
    stage = status.get("stage") or {}
    # Exclude heartbeats and elapsed counters: they do not demonstrate progress.
    progress = dict(run=task["run_id"], status=status["status"], steps=stage.get("steps", []),
                    checks=status["checks"], summary=diff["summary"] if diff else None)
    return dict(task_id=task_id, scope=scope(task, binding), sampled_at=now, generation=generation,
                status=status["status"], reason=status["reason"], execution=status["execution"],
                stage=stage, run_history=status["run_history"], created_at=task["created_at"],
                evidence_complete=complete, diff_error=diff_error, violations=violations[:32],
                progress=_sha(canonical_json(progress)))


def findings(sample):
    found = {}
    execution, stage = sample["execution"], sample["stage"]
    history = sample["run_history"]
    if execution["state"] == "unknown" and history and history[-1]["phase"] != "settled":
        found["EXECUTION_UNVERIFIED"] = dict(run_id=execution["run_id"], state=execution["state"])
    repeated = [dict(stage_id=row["stage_id"], step_id=row["step_id"], attempts=row["attempts"])
                for row in stage.get("steps", []) if row["attempts"] >= 3
                and row["state"] not in {"completed", "passed", "succeeded"}]
    if repeated and sample["status"] not in TERMINAL:
        found["REPEATED_ATTEMPTS"] = dict(steps=repeated[:32])
    if sample["violations"]:
        found["DECLARED_SCOPE_CHANGED"] = dict(violations=sample["violations"],
                                               complete=sample["evidence_complete"])
    remaining = stage.get("remaining_stage_ids", [])
    if sample["reason"] == "STAGES_INCOMPLETE" or (sample["status"] == "completed" and remaining):
        found["STAGES_INCOMPLETE"] = dict(remaining_stage_ids=remaining[:32])
    if sample["status"] in {"blocked", "cleanup_blocked"} and execution["state"] != "active":
        found["UNHANDLED_FAILURE"] = dict(reason=sample["reason"], next_action="inspect_status")
    return found


def reduce_sample(previous, sample):
    """Pure update: deduplicate occurrences and retain explicit recovery evidence."""
    now = sample["sampled_at"]
    record = deepcopy(previous) if previous else dict(version=1, task_id=sample["task_id"], alerts=[])
    same = all(record.get("scope", {}).get(key) == sample["scope"][key]
               for key in ("run_id", "stop_epoch", "binding_revision", "spec_sha256", "runtime_seal"))
    consecutive = (same and record.get("sample_error") not in {"SAMPLE_UNAVAILABLE", "SAMPLE_START_FAILED"}
                   and 0 <= now - (record.get("sampled_at") or 0) <= STALE_SECONDS)
    candidates = findings(sample)
    pending = {}
    for code in list(candidates):
        pending[code] = record.get("pending", {}).get(code, 0) + 1 if consecutive else 1
        if code == "EXECUTION_UNVERIFIED" and pending[code] < 2:
            del candidates[code]
    active = {row["code"]: row for row in record["alerts"] if row["state"] == "active"}
    for code, row in active.items():
        if not same or (code not in candidates and not (
                code == "DECLARED_SCOPE_CHANGED" and not sample["evidence_complete"])):
            # A missing second confirmation must not erase an already confirmed alert.
            if same and code == "EXECUTION_UNVERIFIED" and code in pending:
                continue
            row.update(state="resolved", resolved_at=now)
    for code, evidence in candidates.items():
        old = active.get(code)
        if same and old and old["state"] == "active":
            old.update(last_seen=now, evidence=evidence)
            continue
        identifier = _sha(canonical_json([sample["task_id"], sample["scope"], code, now]))
        historical_failure = ((previous is None or previous.get("sampled_at") is None)
                              and sample.get("baseline", False)
                              and sample["status"] in TERMINAL | {"blocked", "cleanup_blocked"})
        initial_delivery_state = "baseline_only" if historical_failure else "pending"
        record["alerts"].append(dict(id=identifier, code=code, state="active", first_seen=now,
            last_seen=now, resolved_at=None, evidence=evidence, scope=sample["scope"],
            notification=dict(state=initial_delivery_state, message_id=None)))
    # Prefer queued records for cancellation; message expiry also bounds crash recovery.
    retained = [row for row in record["alerts"] if row["state"] == "active"
                or row["notification"]["state"] in {"pending", "queued", "claimed", "dispatching"}]
    history = [row for row in record["alerts"] if row not in retained]
    history_slots = max(0, MAX_ALERTS - len(retained))
    record["alerts"] = ((history[-history_slots:] if history_slots else []) + retained)[-MAX_ALERTS:]
    history = sample["run_history"]
    end = history[-1].get("settled_at") if sample["status"] in TERMINAL and history else None
    waiting = (record.get("metrics") or {}).get("observed_wait_seconds", 0)
    if (consecutive and now - record["sampled_at"] <= 2 * INTERVAL
            and record.get("status") in WAITING and sample["status"] in WAITING):
        waiting += now - record["sampled_at"]
    metrics = dict(wall_seconds=max(0, (end or now) - sample["created_at"]), run_count=len(history),
        resume_count=sum(row["kind"] == "resume" for row in history),
        recheck_count=sum(row["kind"] == "recheck" for row in history),
        extra_step_attempts=sum(max(0, row["attempts"] - 1) for row in sample["stage"].get("steps", [])),
        kernel_execution_seconds=sample["stage"].get("elapsed_seconds"),
        observed_wait_seconds=waiting, human_work_seconds=None, model_tokens=None,
        efficiency_assessment="insufficient_baseline")
    if record.get("progress") != sample["progress"]:
        record["last_progress_at"] = now
    record.update(scope=sample["scope"], sampled_at=now, status=sample["status"],
                  progress=sample["progress"], pending=pending, metrics=metrics,
                  generation=sample["generation"], evidence_complete=sample["evidence_complete"],
                  sample_error=sample["diff_error"])
    return record


def record_failure(store, task_id, code):
    record = read(store, task_id) or dict(version=1, task_id=task_id, alerts=[], sampled_at=None)
    record.update(sample_error=code, sample_failed_at=time.time(), pending={})
    save(store, record)


class Sampler:
    """One bounded child at a time keeps expensive Git reads off the notification loop."""

    def __init__(self, host, *, clock=time.monotonic):
        self.host, self.clock = host, clock
        self.due = {}
        self.job = None
        self.last_error = None
        self.started_at = time.time()

    def _close_job(self, *, kill=False):
        job = self.job
        if kill:
            try:
                os.killpg(job["process"].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        job["process"].wait()
        job["output"].close()
        self.job = None

    def close(self):
        if self.job is not None:
            self._close_job(kill=True)

    def poll(self):
        now = self.clock()
        if self.job is not None:
            job = self.job
            if job["process"].poll() is None and now - job["started"] < SAMPLE_TIMEOUT:
                return None
            task_id = job["task_id"]
            try:
                _require(job["process"].poll() == 0, "BUSY")
                job["output"].seek(0)
                encoded = job["output"].read(MAX_RECORD + 1)
                _require(len(encoded) <= MAX_RECORD, "STATE_CORRUPT")
                sample = json.loads(encoded)
                _require(sample["task_id"] == task_id, "STATE_CORRUPT")
                task, _, binding = self.host.interaction._task(task_id)
                _require(sample["scope"] == scope(task, binding), "BUSY")
                sample["baseline"] = task["created_at"] < self.started_at
                save(self.host.store, reduce_sample(read(self.host.store, task_id), sample))
                self.last_error = None
            except (OSError, ValueError, KeyError, TypeError):
                self.last_error = "SAMPLE_UNAVAILABLE"
                record_failure(self.host.store, task_id, self.last_error)
            finally:
                self._close_job(kill=True)
                self.due[task_id] = now + INTERVAL
            return task_id
        tasks = self.host.interaction._task_ids()
        self.due = {key: value for key, value in self.due.items() if key in tasks}
        ready = sorted((self.due.get(key, 0), key) for key in tasks if self.due.get(key, 0) <= now)
        for _, task_id in ready:
            self.due[task_id] = now + INTERVAL
            output = tempfile.TemporaryFile()
            environment = dict(os.environ, ATO_HOME=str(self.host.store.home),
                               CODEX_HOME=str(self.host.store.native_home))
            environment.pop("ATO_ACTOR_FILE", None)
            try:
                process = subprocess.Popen([sys.executable, "-IB", str(Path(__file__).resolve()),
                    task_id, self.host.store.runtime_seal], stdout=output, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, env=environment, start_new_session=True)
            except OSError:
                output.close()
                self.last_error = "SAMPLE_START_FAILED"
                record_failure(self.host.store, task_id, self.last_error)
                return None
            self.job = dict(process=process, output=output, task_id=task_id, started=now)
            break
        return None


def _muted(host, task):
    with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
        return any(Path(rule["repo"]).resolve() == Path(task["repo"]["root"])
                   for rule in notification_state.project_mutes())


def _cancel_notification(bus, sender, notification):
    if notification.get("message_id") is None:
        notification["state"] = "cancelled"
        return
    message = bus.message(notification["message_id"])
    if message["state"] in {"queued", "claimed"} and not message["external_possible"]:
        message = bus.cancel(sender, message["id"], source="watchdog observation superseded")
    notification["state"] = message["state"]


def _deliver_alert(host, task, binding, record, alert, dispatch):
    notification = alert["notification"]
    service = host.interaction
    task_id = task["task_id"]
    bus = host.mailbox()
    if bus is None:
        notification["state"] = "unavailable"
        return
    # Recover expired leases and messages, including a crash after enqueue.
    bus.recover()
    current = (alert["state"] == "active" and record["scope"] == scope(task, binding)
               and all(alert["scope"][key] == record["scope"][key] for key in
                       ("run_id", "stop_epoch", "binding_revision", "spec_sha256", "runtime_seal"))
               and 0 <= time.time() - record["sampled_at"] <= STALE_SECONDS and not record.get("sample_error"))
    muted = _muted(host, task)
    reservation = notification.get("reservation")
    if reservation is not None and (not current or muted):
        sender = host.delivery_sender(service._scope_record(task), reservation)
        if sender is not None:
            _cancel_notification(bus, sender, notification)
        elif notification["message_id"] is not None:
            notification["state"] = bus.message(notification["message_id"])["state"]
        else:
            notification["state"] = "cancelled"
        return
    if not current or muted:
        notification["state"] = "muted" if muted else "cancelled"
        return
    origin = host.store.submission_origin(task_id)
    if origin is None or origin.kind != "codex" or origin.session_id is None:
        notification["state"] = "unsupported" if origin and origin.kind == "agy" else "unavailable"
        return
    item = dict(created_at=alert["first_seen"], generation=record["generation"])
    route = host.local_completion_route(service._scope_record(task), binding, item)
    if route is None:
        notification["state"] = "unavailable"
        return
    if reservation is None:
        reservation = dict(sender=route.sender.endpoint_id, target=route.target.endpoint_id,
            source_ref=route.source_ref, local_completion=True, generation=record["generation"],
            expires_at=time.time() + 300,
            body=f"ATO 任务 {task_id} 监控提示 {alert['code']}。请用 ato status {task_id} 核对证据与最新状态。")
        notification["reservation"] = reservation
        # Persist the immutable enqueue inputs before publishing any message.
        save(host.store, record)
    _require((route.sender.endpoint_id, route.target.endpoint_id, route.source_ref) ==
             (reservation["sender"], reservation["target"], reservation["source_ref"]), "STALE_REQUEST")
    if notification["message_id"] is None:
        # Share the existing FIFO endpoint without advancing its business route.
        with bus._transaction() as connection:
            existing = connection.execute(
                "select generation,route_generation from bindings where sender=? and target=? "
                "and task_id=? and contract_hash=?",
                (reservation["sender"], reservation["target"], task_id, task["spec_sha256"])).fetchone()
        if existing is not None:
            _require(json.loads(existing["generation"]) == reservation["generation"], "STALE_REQUEST")
        bound = bus.bind_route(sender=reservation["sender"], target=reservation["target"],
            task_id=task_id, contract_hash=task["spec_sha256"], source=reservation["source_ref"],
            generation=reservation["generation"], advance=False,
            route_generation=existing["route_generation"] if existing is not None else 0,
            cross_project=route.sender.project != route.target.project)
        message = bus.enqueue(route.sender, binding_id=bound["id"], target=reservation["target"],
            kind="ato_watchdog", body=reservation["body"], idempotency_key="watchdog:" + alert["id"],
            task_id=task_id, contract_hash=task["spec_sha256"], correlation_id=alert["id"],
            protocol=2, generation=reservation["generation"], canonical_id=alert["id"],
            authority_source=reservation["source_ref"], expires_at=reservation["expires_at"])
        notification.update(message_id=message["id"], state=message["state"])
        save(host.store, record)
    message = bus.message(notification["message_id"])
    if message["state"] == "queued":
        def guard():
            try:
                latest, _, current_binding = service._task(task_id)
                _require(scope(latest, current_binding) == record["scope"]
                         and not _muted(host, latest), "STALE_REQUEST")
                actual = service._removed_generation(latest) if latest["worktree_removed"] else service._kernel(latest)[0]
                _require(actual == reservation["generation"], "STALE_REQUEST")
            except (OSError, ValueError):
                try:
                    claimed = bus.message(message["id"])
                    if claimed["state"] == "dispatching":
                        bus.finish_dispatch(claimed["id"], claimed["attempt_id"], state="failed")
                    else:
                        _cancel_notification(bus, route.sender, notification)
                except MailboxError:
                    pass
                raise
        dispatch(host, bus, route, message["id"], guard)
    notification["state"] = bus.message(message["id"])["state"]


def deliver(host, task_id, dispatch):
    record = read(host.store, task_id)
    if not record:
        return
    task, _, binding = host.interaction._task(task_id)
    for alert in record["alerts"]:
        notification = alert["notification"]
        if notification["state"] in {"baseline_only", "delivered", "accepted", "responded", "turn_completed",
                                    "delivery_unknown", "failed", "cancelled", "expired"}:
            continue
        try:
            _deliver_alert(host, task, binding, record, alert, dispatch)
            notification.pop("error", None)
        except (OSError, ValueError, MailboxError):
            notification["error"] = "NOTIFICATION_UNAVAILABLE"
        save(host.store, record)


def main():
    from ato_host import Host
    host = Host(seal=sys.argv[2], read_only=True).compose()
    try:
        encoded = json.dumps(collect(host, sys.argv[1]))
        _require(len(encoded.encode()) <= MAX_RECORD, "STATE_CORRUPT")
        print(encoded)
    finally:
        host.close_mailbox_snapshot()


if __name__ == "__main__":
    main()
