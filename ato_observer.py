"""Authorized observation of the unified namespace through its existing mailbox."""
from functools import partial
import os
import signal
from threading import Event
import time

import ato_config
import ato_process
from ato_host import Host
from ato_private import delivery_candidates, render_notice
from ato_task_store import TaskError, _directory, _locked_file, _publish, _require, _save
from message_bus import EndpointCredential, MailboxError
import transport


def _dispatch(host, bus, route, message_id, guard):
    configured = host.record("observation", {})
    endpoint = bus.authenticate(route.target)
    kind = endpoint["kind"]
    if kind not in configured.get("transport_kinds", ()):
        return
    import agents  # Register fixed local adapters before resolving a configured transport.
    adapter = agents.registry.get(kind, "transport")
    owner = bus.acquire_endpoint(route.target, owner="ato-observer:" + str(os.getpid()), lease_seconds=30)
    try:
        with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
            transport.dispatch_one(bus, route.target, owner, {kind: adapter},
                                   message_id=message_id, guard=guard)
    finally:
        bus.release_endpoint(route.target, owner)


def tick(host):
    service = host.interaction
    for task_id in service._task_ids():
        task, spec, binding = service._task(task_id)
        if task["creation"]["runtime_seal"] != host.store.runtime_seal:
            continue
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


def observe(seal):
    host = Host(seal=seal).compose(render_notice=render_notice)
    host.interaction.dispatch_envelope = partial(_dispatch, host)
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
    failures = 0
    while not stop.is_set():
        if host.record("observation") != config:
            break
        try:
            tick(host)
            failures = 0
        except (TaskError, OSError, ValueError, MailboxError):
            failures += 1
        _save(fd, "process.json", dict(pid=os.getpid(), pid_identity=birth, release=seal,
               heartbeat=time.time(), state="watching", consecutive_failures=failures))
        stop.wait(2)
    _save(fd, "process.json", dict(pid=os.getpid(), pid_identity=birth, release=seal,
           heartbeat=time.time(), state="stopped", consecutive_failures=failures))
