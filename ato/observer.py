"""Lightweight single-user Observer daemon: dispatches Codex notifications and drives Watchdog."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
import time

from ato import codex_notify, config, process, watchdog
from ato.store import Store


def dispatch_pending(store: Store, *, sender=None) -> dict[str, int]:
    """Deliver all pending notifications independently (zero FIFO head-of-line blocking)."""
    settings = config.load_settings()
    enabled = bool(settings.get("notifications_enabled", True))
    queue_bin = settings.get("codex_queue_bin")
    send_fn = sender or codex_notify.send_queue_notice
    stats = {"sent": 0, "muted": 0, "unsupported": 0, "failed": 0}

    for notice in store.pending_notifications():
        nid = notice["id"]
        try:
            task = store.get_task(notice["task_id"])
        except Exception:
            store.update_notification(nid, state="cancelled", error="TASK_NOT_FOUND")
            continue

        if not enabled or config.is_repo_muted(task["repo_root"], settings):
            store.update_notification(nid, state="muted")
            stats["muted"] += 1
            continue

        target = notice.get("target_session")
        if task["origin_kind"] != "codex" or not target:
            store.update_notification(nid, state="unsupported")
            stats["unsupported"] += 1
            continue

        try:
            ok, receipt_id, err = send_fn(config.codex_home(), target, notice["body"], queue_bin=queue_bin)
        except Exception as exc:
            ok, receipt_id, err = False, None, str(exc)[:240]

        if ok:
            store.update_notification(nid, state="sent", receipt_id=receipt_id, error=None)
            stats["sent"] += 1
            # Keep watchdog alert notification_state mirrored when applicable.
            if notice["kind"] == "watchdog" and notice["dedupe_key"].startswith("watchdog:"):
                _mirror_alert_state(store, task["id"], notice["dedupe_key"].removeprefix("watchdog:"), "sent", None)
        else:
            store.update_notification(nid, state="failed", error=err or "DELIVERY_FAILED")
            stats["failed"] += 1
            if notice["kind"] == "watchdog" and notice["dedupe_key"].startswith("watchdog:"):
                _mirror_alert_state(store, task["id"], notice["dedupe_key"].removeprefix("watchdog:"), "failed", err)
    return stats


def _mirror_alert_state(store: Store, task_id: str, alert_id: str, state: str, error: str | None) -> None:
    wstate, alerts = store.get_watchdog(task_id)
    if wstate is None:
        return
    for a in alerts:
        if a["id"] == alert_id:
            a["notification_state"] = state
            a["notification_error"] = error
            store.save_watchdog(task_id, wstate, alerts)
            return


def _write_state(path: Path, payload: dict) -> None:
    config.ensure_private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def observer_status() -> dict:
    path = config.observer_state_path()
    if not path.is_file():
        return {"pid": None, "state": "stopped", "watchdog_error": None, "heartbeat": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"pid": None, "state": "unknown", "watchdog_error": None, "heartbeat": None}
    pid, birth = data.get("pid"), data.get("pid_identity")
    live = bool(pid and birth and process.disposition(pid, birth) == "live")
    return {
        "pid": pid if live else None,
        "state": data.get("state", "watching") if live else "stopped",
        "watchdog_error": data.get("watchdog_error"),
        "heartbeat": data.get("heartbeat"),
    }


def start_daemon(db_file: Path | None = None) -> dict:
    current = observer_status()
    if current["state"] == "watching" and current["pid"]:
        return current
    config.ensure_private_dir(config.state_dir())
    log_path = config.state_dir() / "observer.log"
    target_db = str(db_file or config.db_path())
    env = dict(os.environ, ATO_HOME=str(config.ato_home()), CODEX_HOME=str(config.codex_home()))
    with log_path.open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "ato.observer", "_serve", target_db],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=env, start_new_session=True, close_fds=True,
        )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        st = observer_status()
        if st["state"] == "watching" and st["pid"]:
            return st
        time.sleep(0.1)
    return observer_status()


def stop_daemon() -> dict:
    path = config.observer_state_path()
    if not path.is_file():
        return {"pid": None, "state": "stopped"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"pid": None, "state": "stopped"}
    pid, birth = data.get("pid"), data.get("pid_identity")
    if pid and birth:
        process.stop_pid_group(pid, birth)
    data["state"] = "stopped"
    data["heartbeat"] = time.time()
    _write_state(path, data)
    return {"pid": None, "state": "stopped"}


def serve(store: Store, *, poll_seconds: float = 2.0, stop_event: Event | None = None) -> None:
    config.ensure_private_dir(config.state_dir())
    lock_file = config.observer_lock_path().open("a+")
    os.chmod(config.observer_lock_path(), 0o600)
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return

    stop = stop_event or Event()
    if stop_event is None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())

    pid = os.getpid()
    birth = process.identity(pid)
    state_path = config.observer_state_path()
    sampler = watchdog.Sampler(store)
    row = {"pid": pid, "pid_identity": birth, "state": "watching", "watchdog_error": None, "heartbeat": time.time()}
    _write_state(state_path, row)
    try:
        while not stop.is_set():
            try:
                dispatch_pending(store)
            except Exception:
                pass
            try:
                sampled = sampler.poll()
                if sampled and sampler.last_error is None:
                    watchdog.sync_alert_notifications(store, sampled)
                    dispatch_pending(store)
                row["watchdog_error"] = sampler.last_error
            except Exception:
                row["watchdog_error"] = "MONITOR_UNAVAILABLE"
            row["heartbeat"] = time.time()
            _write_state(state_path, row)
            stop.wait(poll_seconds)
    finally:
        sampler.close()
        row.update(state="stopped", heartbeat=time.time())
        _write_state(state_path, row)
        lock_file.close()


def _main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "_serve":
        serve(Store(Path(sys.argv[2])))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
