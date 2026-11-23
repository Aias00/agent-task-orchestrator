"""Authorized observation of the unified namespace through its existing mailbox."""
from functools import partial
import json
import os
import re
import signal
from threading import Event
import time

import ato_config
from agent_registry import SessionEvent, SessionIdentity
import ato_process
from ato_host import Host
from ato_private import delivery_candidates, render_notice
from ato_task_store import TaskError, _directory, _locked_file, _publish, _read_json, _require, _save, _sha, _state_errors
from message_bus import EndpointCredential, MailboxError
import transport


_LIFECYCLE = {"message_received": "accepted", "response_completed": "responded",
              "turn_completed": "turn_completed"}
_NOTICE = re.compile(r"\[ato handoff (handoff-[0-9a-f]{32})\]\n")


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
                bus.renew_endpoint(credential, token, lease_seconds=30)
                return token
            except MailboxError:
                pass
    if not queued:
        return None  # Never adopt a previous attempt under a new endpoint fence.
    token = bus.acquire_endpoint(credential, owner="ato-observer", lease_seconds=30)
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
                               message_id=message["id"], guard=guard)
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


def _dispatch(host, bus, route, message_id, guard):
    configured = host.record("observation", {})
    endpoint = bus.authenticate(route.target)
    kind = endpoint["kind"]
    if kind not in configured.get("transport_kinds", ()):
        return
    import agents  # Register fixed local adapters before resolving a configured transport.
    adapter = agents.registry.get(kind, "transport")
    with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
        message = bus.message(message_id)
        if _observe(host, bus, route, endpoint, adapter, message, guard) or message["state"] != "queued":
            return
        owner = bus.acquire_endpoint(route.target, owner="ato-observer:" + str(os.getpid()), lease_seconds=30)
        try:
            transport.dispatch_one(bus, route.target, owner, {kind: adapter},
                                   message_id=message_id, guard=guard)
        finally:
            bus.release_endpoint(route.target, owner)


@_state_errors
def _tick_task(host, service, task_id):
    task, spec, binding = service._task(task_id)
    if task["creation"]["runtime_seal"] != host.store.runtime_seal:
        return
    for phase, correlation in delivery_candidates(service, task):
        try:
            _, _, notice = service._delivery_item(task, spec, correlation, phase)
            if notice and notice["binding_revision"] != binding["revision"]:
                # A displaced reservation can be cancelled only by its
                # original enrolled sender and before a possible native effect.
                old = host.record(notice["from_ref"], {})
                if not notice["superseded"] and old.get("mailbox"):
                    service.supersede_unsubmitted(task_id, correlation, phase,
                                                   EndpointCredential(**old["mailbox"]))
                if phase != "question":
                    continue
            service.dispatch_delivery(task_id, correlation, phase)
        except TaskError as exc:
            if exc.code not in {"BUSY", "STALE_REQUEST", "BINDING_CHANGED",
                                "PRODUCT_CHANGED", "CHECKS_REQUIRED"}:
                raise


def tick(host):
    service = host.interaction
    failures = {}
    for task_id in service._task_ids():
        try:
            _tick_task(host, service, task_id)
        except TaskError as exc:
            failures[task_id] = exc.code
    return failures


def observe(seal):
    host = Host(seal=seal).compose(render_notice=render_notice)
    host.interaction.dispatch_envelope = partial(_dispatch, host)
    host.interaction.observe_envelope = partial(_dispatch, host)
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
    while not stop.is_set():
        if host.record("observation") != config:
            break
        try:
            task_failures = tick(host)
        except (OSError, ValueError):
            task_failures = None
        failed = task_failures is None or bool(task_failures)
        first = next(iter(task_failures.items())) if task_failures else None
        row.update(heartbeat=time.time(), state="watching",
                   consecutive_failures=row["consecutive_failures"] + 1 if failed else 0,
                   failed_task_count=len(task_failures) if task_failures is not None else None,
                   first_task_failure=dict(task_id=first[0], code=first[1]) if first else None)
        _save(fd, "process.json", row)
        stop.wait(2)
    _save(fd, "process.json", dict(row, heartbeat=time.time(), state="stopped"))
