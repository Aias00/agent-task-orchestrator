"""Authorized observation of the unified namespace through its existing mailbox."""
from functools import partial
import json
import os
import re
import signal
import sqlite3
from threading import Event
import time

import ato_config
import ato_watchdog
from agent_registry import SessionEvent, SessionIdentity
import ato_process
import failure_attention
import notification_state
from ato_host import Host
from ato_private import delivery_candidates, render_notice
from ato_task_store import TaskError, _directory, _locked_file, _publish, _read_json, _require, _save, _sha, _state_errors
from message_bus import MailboxError, TERMINAL
from codex_session_adapter import CodexSessionAdapter, queue_executable
import transport


_LIFECYCLE = {"message_received": "accepted", "response_completed": "responded",
              "turn_completed": "turn_completed"}
_NOTICE = re.compile(r"\[ato handoff (handoff-[0-9a-f]{32})\]\n")
_LEASE_SECONDS = 60


def _ledger(host, key, row=None):
    with host.store._private(host.folder / "observer-ledger", create=True) as fd:
        if row is not None:
            _save(fd, key + ".json", row)
            return row
        try:
            return _read_json(fd, key + ".json")
        except FileNotFoundError:
            return None


def _owner(host, bus, credential, message, *, queued):
    key = "owner-" + _sha(credential.endpoint_id.encode())
    receipt = _ledger(host, key)
    if receipt:
        token = receipt["token"]
        if queued or token == message["claim_owner"]:
            try:
                bus.renew_endpoint(credential, token, lease_seconds=_LEASE_SECONDS)
                return token
            except MailboxError:
                pass
    if not queued:
        return None  # Never adopt a previous attempt under a new endpoint fence.
    token = bus.acquire_endpoint(credential, owner="ato-observer", lease_seconds=_LEASE_SECONDS)
    _ledger(host, key, {"token": token})
    return token


def _observe(host, bus, route, endpoint, adapter, message, guard):
    marker = _NOTICE.match(message["body"])
    cursor_reader = getattr(adapter, "session_cursor", None)
    reader = getattr(adapter, "observe_session_v2", None)
    if not marker or not callable(cursor_reader) or not callable(reader):
        return False
    correlation = marker[1]
    identity = SessionIdentity(endpoint["kind"], endpoint["session_id"], endpoint["project"], endpoint["source"])
    generation = json.loads(message["generation"])
    key = "message-" + _sha(message["id"].encode())
    scope = dict(identity=identity.to_mapping(), generation=generation, correlation=correlation,
                 binding_id=message["binding_id"], route_generation=message["route_generation"])
    saved = _ledger(host, key)
    if saved is not None:
        _require(saved["scope"] == scope, "STALE_REQUEST")
    if message["state"] == "queued":
        owner = _owner(host, bus, route.target, message, queued=True)
        if saved is None:
            cursor = cursor_reader(identity.native_id, identity.project)
            _ledger(host, key, dict(scope=scope, cursor=cursor))
        transport.dispatch_one(bus, route.target, owner, {endpoint["kind"]: adapter},
                               message_id=message["id"], guard=guard, lease_seconds=_LEASE_SECONDS)
        return True
    if saved is None or not message["attempt_id"] or not _owner(host, bus, route.target, message, queued=False):
        return True
    events, cursor = reader(identity, correlation, generation[-1], saved["cursor"])
    selected = [event for event in events if event.kind in _LIFECYCLE]
    _require(all(type(event) is SessionEvent and event.session == identity
                 and event.correlation_id == correlation and event.message_id == correlation
                 and event.generation == generation[-1] and bool(event.turn_id)
                 for event in selected), "STALE_REQUEST")
    for event in selected:
        guard()
        current = bus.message(message["id"])
        _require(current["attempt_id"] == message["attempt_id"]
                 and current["endpoint_generation"] == message["endpoint_generation"], "STALE_REQUEST")
        transport.receive_event(bus, route.target, transport.TransportEvent(
            _LIFECYCLE[event.kind], message["id"], route.target.endpoint_id, identity.native_id,
            event.event_id, event.source, version=2, project=identity.project,
            correlation_id=message["correlation_id"], generation=generation,
            route_generation=message["route_generation"], endpoint_generation=message["endpoint_generation"],
            attempt_id=message["attempt_id"], turn_id=event.turn_id))
    _ledger(host, key, dict(scope=scope, cursor=cursor))
    return True


def _dispatch(host, bus, route, message_id, guard, *, observe_only=False):
    endpoint = bus.authenticate(route.target)
    kind = endpoint["kind"]
    override = None
    if route.local_completion:
        binary = host.acceptance_queue_binary()
        if kind != "codex" or binary is None:
            return
        adapter = CodexSessionAdapter(queue_binary=binary)
    else:
        configured = host.record("observation", {})
        if kind not in configured.get("transport_kinds", ("codex", "pi_acp")):
            return
        import agents  # Register fixed local adapters before resolving a configured transport.
        adapter = agents.registry.get(kind, "transport")
    with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
        message = bus.message(message_id)
        if (message["state"] == "queued") == observe_only:
            return
        if not route.local_completion and kind == "codex" and message["state"] == "queued":
            override = host.queue_executable(configured)
            _require(override is not None, "UNSUPPORTED_CAPABILITY")
        token = queue_executable.set(override)
        try:
            _dispatch_selected(host, bus, route, endpoint, adapter, message, guard)
        finally:
            queue_executable.reset(token)


def _dispatch_selected(host, bus, route, endpoint, adapter, message, guard):
    if _observe(host, bus, route, endpoint, adapter, message, guard) or message["state"] != "queued":
        return
    owner = bus.acquire_endpoint(route.target, owner="ato-observer:" + str(os.getpid()), lease_seconds=_LEASE_SECONDS)
    try:
        transport.dispatch_one(bus, route.target, owner, {endpoint["kind"]: adapter},
                               message_id=message["id"], guard=guard, lease_seconds=_LEASE_SECONDS)
    finally:
        bus.release_endpoint(route.target, owner)


def _remove_terminal_cursor(host, message):
    if message is None or message["state"] not in TERMINAL or message["busy"]:
        return
    key = "message-" + _sha(message["id"].encode()) + ".json"
    try:
        with host.store._private(host.folder / "observer-ledger") as fd:
            os.unlink(key, dir_fd=fd)
    except FileNotFoundError:
        pass


def _cancel_stale(host, service, task, phase, correlation):
    _, notice = service._reservation(task, correlation, phase)
    if notice is None:
        return True
    sender = host.delivery_sender(service._scope_record(task), notice)
    if sender:
        service.cancel_unsent(task, notice, sender)
    message = service._message(task, notice)
    _remove_terminal_cursor(host, message)
    if message is None:
        return notice["mailbox_id"] is None
    return not message["busy"] and message["state"] not in {"queued", "claimed", "dispatching"}


@_state_errors
def _tick_task(host, service, task_id, cache=None):
    task, _, binding = service._task(task_id)
    if task["creation"]["runtime_seal"] != host.store.runtime_seal:
        return
    if cache is not None and cache.get(task_id) == task["revision"]:
        return
    failure, settled = None, True
    for phase, correlation in delivery_candidates(service, task):
        try:
            _, notice = service._reservation(task, correlation, phase)
            if notice and notice["binding_revision"] != binding["revision"]:
                # Only the original enrolled sender can cancel a displaced reservation.
                if not notice["superseded"] and (sender := host.delivery_sender(service._scope_record(task), notice)):
                    service.supersede_unsubmitted(task_id, correlation, phase, sender)
                if phase != "question":
                    settled &= _cancel_stale(host, service, task, phase, correlation)
                    continue
            result = service.dispatch_delivery(task_id, correlation, phase)
            if result in TERMINAL:
                _, notice = service._reservation(task, correlation, phase)
                if notice:
                    message = service._message(task, notice)
                    _remove_terminal_cursor(host, message)
                    settled &= bool(message and not message["busy"])
            else:
                settled &= result == "recorded_only"
        except TaskError as exc:
            if exc.code in {"STALE_REQUEST", "BINDING_CHANGED"}:
                try:
                    settled &= _cancel_stale(host, service, task, phase, correlation)
                except (TaskError, MailboxError) as cleanup_error:
                    settled = False
                    failure = failure or TaskError(getattr(cleanup_error, "code", "DELIVERY_UNKNOWN"))
            else:
                settled = False
                if exc.code not in {"BUSY", "PRODUCT_CHANGED", "CHECKS_REQUIRED"}:
                    failure = failure or exc
    if failure:
        raise failure
    if settled and cache is not None:
        cache[task_id] = task["revision"]


def tick(host, cache=None):
    service = host.interaction
    failures = {}
    for task_id in service._task_ids():
        try:
            if cache is None:
                _tick_task(host, service, task_id)
            else:
                _tick_task(host, service, task_id, cache)
        except TaskError as exc:
            failures[task_id] = exc.code
    return failures


def observe(seal):
    host = Host(seal=seal).compose(render_notice=render_notice)
    host.interaction.dispatch_envelope = partial(_dispatch, host)
    host.interaction.observe_envelope = partial(_dispatch, host, observe_only=True)
    state = host.store.kernel_root / "ctask-interaction" / "control"
    _require(host.mailbox() is not None, "FORBIDDEN")
    config = host.record("observation", {})
    _require(config.get("enabled") is True, "FORBIDDEN")
    birth = ato_process.identity(os.getpid())
    _require(birth, "FORBIDDEN")
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with _directory(state, create=True, private_from=host.store.home) as fd:
        try:
            _publish(fd, "worker.lock", b"", exclusive=True)
        except FileExistsError:
            pass
        with _locked_file(fd, "worker.lock"):
            _watch(host, config, fd, seal, birth, stop)


def _watch(host, config, fd, seal, birth, stop):
    row = dict(pid=os.getpid(), pid_identity=birth, release=seal, consecutive_failures=0,
               failed_task_count=0, first_task_failure=None)
    cache = {}
    policy = None
    sampler = ato_watchdog.Sampler(host)
    watchdog_error = None
    try:
        while not stop.is_set():
            try:
                if host.record("observation") != config:
                    break
                with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
                    configured_policy = (notification_state.status(), notification_state.project_mutes(),
                                         failure_attention.policy(), host.acceptance_notifications())
                if configured_policy != policy:
                    cache.clear()
                    policy = configured_policy
                task_failures = tick(host, cache)
            except (OSError, ValueError, sqlite3.Error):
                task_failures = None
            try:
                if sampled_task := sampler.poll():
                    ato_watchdog.deliver(host, sampled_task, _dispatch)
                watchdog_error = sampler.last_error
            except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, MailboxError):
                watchdog_error = "MONITOR_UNAVAILABLE"
            failed = task_failures is None or bool(task_failures)
            first = next(iter(task_failures.items())) if task_failures else None
            row.update(heartbeat=time.time(), state="watching", watchdog_error=watchdog_error,
                       consecutive_failures=row["consecutive_failures"] + 1 if failed else 0,
                       failed_task_count=len(task_failures) if task_failures is not None else None,
                       first_task_failure=dict(task_id=first[0], code=first[1]) if first else None)
            _save(fd, "process.json", row)
            stop.wait(2)
    finally:
        sampler.close()
        _save(fd, "process.json", dict(row, heartbeat=time.time(), state="stopped"))
