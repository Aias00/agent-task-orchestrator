"""Scoped human decisions for unified Tasks. Execution and routing stay separate."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import wraps
import os
from pathlib import Path
import re
import stat
import time
from typing import Mapping

import agent_registry
import ato_config
from ato_execution import HostHandoffProof, _active_proof, _binding, _raw_stop
import ato_process
from ato_private import render_local_completion
from ato_task_store import (
    DELIVERY_OUTCOMES, TaskError, _ID, _REF, _SHA, _directory, _existing, _fields, _generation_valid,
    _identifier, _matches, _positive, _read_json, _regular, _require, _save, _sha,
    _state_errors, _strict_json, _time, _valid_id, canonical_json, new_id, read_input_file,
)
import execution_state
from message_bus import EndpointCredential, MailboxError
import notification_state
import recovery
import transport


_PRODUCT = re.compile(r"product-[0-9a-f]{64}")
_MAILBOX_ID = re.compile(r"msg-[0-9a-f]{32}")
_MANUAL = "manual_acceptance"
_LOCAL_ACCEPTANCE = "local-acceptance.json"
_ASK_KINDS = {"decision", "missing_information", "handler_assignment", "permission", "host_handoff"}
_GRANT_KINDS = {"permission", "host_handoff"}
_ROLES = agent_registry.ActorRole
_ACTIONS = {
    "ask": (_ROLES.ORIGIN, "controller_ref", "question.ask"),
    "present": (_ROLES.HUB, "interaction_ref", "interaction.present"),
    "answer": (_ROLES.USER, "interaction_ref", "decision.answer"),
    "accept": (_ROLES.USER, "interaction_ref", "decision.accept"),
    "read_answer": (_ROLES.ORIGIN, "controller_ref", "answer.accept"),
}
_EVIDENCE_FIELDS = ("schema_version", "task_id", "spec_sha256", "product_id", "checks_sha256",
                    "criteria", "decision", "source", "note")


def _text(value):
    try:
        return (isinstance(value, str) and bool(value.strip()) and "\0" not in value
                and len(value.encode("utf-8")) <= 8192)
    except UnicodeError:
        return False


def _acceptance_input(data):
    decision = _strict_json(data, "INVALID_JSON")
    _fields(decision, _EVIDENCE_FIELDS, code="INVALID_EVIDENCE")
    _require(type(decision["schema_version"]) is int and decision["schema_version"] == 1,
             "UNSUPPORTED_VERSION")
    criteria = decision["criteria"]
    _require(_valid_id(decision["task_id"], "task")
             and _matches(_SHA, decision["spec_sha256"])
             and _matches(_PRODUCT, decision["product_id"])
             and _matches(_SHA, decision["checks_sha256"])
             and isinstance(criteria, list)
             and all(_matches(_ID, entry) for entry in criteria) and len(set(criteria)) == len(criteria)
             and decision["decision"] == "accept" and _text(decision["source"]) and _text(decision["note"]),
             "INVALID_EVIDENCE")
    return dict(decision, criteria=sorted(criteria))


@dataclass(frozen=True)
class VerifiedEvent:
    """Host verification of the actual scoped event, with non-secret audit references.

    For permission/host-handoff answers the verifier checks the exact frozen
    purpose, actual authorizer and grant. A denial may be recorded with no
    grant. `performed` means verified evidence of an already completed host
    action, used by the T3 host handoff seam.
    """

    reference: str
    grant_ref: str | None = None
    performed: bool = False


@dataclass(frozen=True)
class MailboxRoute:
    """Exact enrolled endpoints selected by the trusted unified host."""

    sender: EndpointCredential
    target: EndpointCredential
    source_ref: str
    owner_token: str | None = None
    transports: Mapping[str, transport.ManagedTransport] | None = None
    local_completion: bool = False


def _retry_busy(method):
    @wraps(method)
    def call(*args, **kwargs):
        for delay in (.005, .02, .05, .12, None):
            try:
                return method(*args, **kwargs)
            except TaskError as exc:
                if exc.code != "BUSY" or delay is None:
                    raise
                time.sleep(delay)
    return call


class InteractionService:
    """T4 leaf. The parent and sealed child use the same Task/binding readers.

    `authorize(principal, scope, binding, action, item)` checks the current
    unified grant, including binding revision and the exact item scope.
    `verify_event(principal, scope, binding, action, item, value, event)`
    verifies the actual user or presentation event and returns VerifiedEvent.
    `verify_continuation(scope, binding, request, answer)` rechecks the
    recorded answer's applicability and any grant before an execution effect.
    `route(scope, binding, phase, from_ref, to_ref)` resolves and preflights
    both enrolled endpoints and returns MailboxRoute for that exact route.
    These callbacks are trusted code. No proof object or mailbox credential is
    written to the Task. Child projection does not need public actor callbacks.
    """

    def __init__(self, store, execution, *, authorize=None, verify_event=None,
                 verify_continuation=None, route=None, mailbox=None, render_notice=None,
                 local_completion_route=None, authorize_local_acceptance=None, clock=time.time):
        self.store = store
        self.execution = execution
        self.authorize = authorize
        self.verify_event = verify_event
        self.verify_continuation = verify_continuation
        self.route = route
        self.local_completion_route = local_completion_route
        self.authorize_local_acceptance = authorize_local_acceptance
        self.mailbox = mailbox
        self.render_notice = render_notice
        self.dispatch_envelope = None
        self.observe_envelope = None
        self.clock = clock

    def execution_hooks(self):
        """Compose with the existing binding reader in the parent and child."""
        return replace(self.execution.hooks, acceptance=self.acceptance, local_acceptance=self.local_acceptance,
                       pending=self.pending,
                       decision_status=self.decision_status, decision_barrier=self.decision_barrier,
                       claim_continuation=self.claim_continuation, host_handoff=self.host_handoff)

    @contextmanager
    def _records(self, task_id, *, create=False, task_fd=None):
        """Open a private leaf without following links or creating on a read."""
        if task_fd is None:
            with self.store._private(self.store._task_dir(task_id)) as fd:
                with self._records(task_id, create=create, task_fd=fd) as leaf:
                    yield leaf
            return
        if create and _existing(task_fd, "interaction") is None:
            os.mkdir("interaction", 0o700, dir_fd=task_fd)
            os.fsync(task_fd)
        try:
            fd = os.open("interaction", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=task_fd)
        except FileNotFoundError:
            yield None
            return
        try:
            info = os.fstat(fd)
            _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
            yield fd
        finally:
            os.close(fd)

    def _task_ids(self):
        try:
            with self.store._private(self.store.tasks) as fd:
                return sorted(name for name in os.listdir(fd) if _valid_id(name, "task"))
        except FileNotFoundError:
            return []

    def _task(self, task_id):
        task = self.store.load_task(task_id)
        spec = self.store.load_spec(task_id)
        return task, spec, _binding(self.execution.hooks, task, spec)

    def _scope(self, task_id, principal):
        scope = self.store._visible_scope(task_id, principal)
        _require(scope is not None, "NOT_FOUND")
        return scope

    def _allowed(self, principal, scope, task, binding, action, item, generation):
        if not isinstance(principal, agent_registry.ActorContext) or task["workflow"] is None:
            return False
        if action == "read":
            role, route = principal.role, "interaction_ref"
            operation = {_ROLES.HUB: "interaction.inbox", _ROLES.USER: "decision.answer"}.get(role)
        else:
            role, route, operation = _ACTIONS[action]
        if (operation is None or principal.role != role or principal.session.reference != binding[route]
                or principal.project not in (task["repo"]["root"], task["worktree"]["path"])
                or not _generation_valid(generation) or not _matches(_REF, principal.actor_id)):
            return False
        if action == "answer" and item["kind"] in _GRANT_KINDS:
            operation = "decision.approve"
        try:
            # The unified grant additionally binds the Task, its full generation
            # and route revision. The existing actor API authenticates the
            # native endpoint/project and Workflow revision.
            agent_registry.authorize_actor(principal, operation, project=principal.project,
                workflow_id=task["workflow"]["directory"], contract_hash=task["workflow"]["contract_hash"],
                generation=generation[1])
        except ValueError:
            return False
        return self.authorize is not None and self.authorize(
            principal, scope, deepcopy(binding), action, deepcopy(item)) is True

    def _event(self, principal, scope, binding, action, item, value, source_event):
        _require(self.verify_event is not None, "AUTH_REQUIRED")
        try:
            event = self.verify_event(principal, scope, deepcopy(binding), action,
                                      deepcopy(item), deepcopy(value), source_event)
        except (ValueError, PermissionError):
            raise TaskError("FORBIDDEN") from None
        _require(type(event) is VerifiedEvent and _matches(_REF, event.reference)
                 and (event.grant_ref is None or _matches(_REF, event.grant_ref))
                 and type(event.performed) is bool, "FORBIDDEN")
        return event

    def _kernel(self, task):
        """Read the exact current generation and STOP/closure without running Git."""
        link = task["workflow"]
        if link is None or task["worktree_removed"]:
            return None, False
        with self.store._private(self.store._task_dir(task["task_id"])) as fd:
            stop = _raw_stop(fd, task)
        directory = Path(link["directory"])
        with _directory(directory, private_from=Path(task["worktree"]["path"]) / ".codex/tasks") as fd:
            state, _, _ = self.store._workflow_files(fd, task, link)
            for name in ("events.jsonl", "STOP", "user-closure.json"):
                info = _existing(fd, name)
                if info is not None:
                    _regular(info, True, "STATE_CORRUPT")
            generation = execution_state.generation(directory, state)
            open_ = (not (stop and stop["epoch"] > task.get("stop_retired_epoch", 0))
                     and _existing(fd, "STOP") is None and _existing(fd, "user-closure.json") is None)
            _require(_generation_valid(generation))
            _require(_read_json(fd, "state.json", canonical=False) == state, "BUSY")
        return generation, open_

    def _row(self, fd, request_id, task):
        if fd is None or _existing(fd, request_id + ".json") is None:
            return None
        row = _read_json(fd, request_id + ".json")
        _fields(row, ("request", "answer", "consumption", "presentation", "outbox"))
        request = row["request"]
        _fields(request, ("request_id", "task_id", "repo_key", "spec_sha256", "run_id", "generation",
                          "stop_epoch", "created_at", "expires_at", "kind", "question", "reason",
                          "created_by", "controller_ref", "interaction_ref", "binding_revision",
                          "continuation", "purpose_ref"))
        _require(request["request_id"] == request_id and request["task_id"] == task["task_id"]
                 and request["repo_key"] == task["repo"]["key"]
                 and request["spec_sha256"] == task["spec_sha256"]
                 and _valid_id(request["run_id"], "run") and _generation_valid(request["generation"])
                 and type(request["stop_epoch"]) is int and request["stop_epoch"] >= 0
                 and _time(request["created_at"])
                 and (request["expires_at"] is None or _time(request["expires_at"]))
                 and request["kind"] in _ASK_KINDS and _text(request["question"]) and _text(request["reason"])
                 and request["continuation"] in (None, "resume", "recheck")
                 and (request["kind"] == "host_handoff") == (request["continuation"] == "recheck")
                 and (_matches(_REF, request["purpose_ref"]) if request["kind"] in _GRANT_KINDS
                      else request["purpose_ref"] is None)
                 and _positive(request["binding_revision"])
                 and all(_matches(_REF, request[k]) for k in
                         ("created_by", "controller_ref", "interaction_ref")))
        for key in ("answer", "consumption", "presentation"):
            _require(row[key] is None or _matches(_SHA, row[key]))
        _require(isinstance(row["outbox"], dict) and row["outbox"].keys() <= {"question", "answer"})
        return row

    def _rows(self, task):
        with self._records(task["task_id"]) as fd:
            if fd is not None:
                for name in sorted(os.listdir(fd)):
                    if name.endswith(".json") and _valid_id(name[:-5], "request"):
                        row = self._row(fd, name[:-5], task)
                        if row is not None:
                            yield row

    @staticmethod
    def _same_version(request, task, generation, open_, run_id):
        return (open_ and request["run_id"] == (run_id or task["run_id"])
                and request["generation"] == generation and request["stop_epoch"] == task["stop_epoch"])

    def _current(self, request, task, generation, open_, *, run_id=None):
        return (self._same_version(request, task, generation, open_, run_id)
                and (request["expires_at"] is None or request["expires_at"] > self.clock()))

    def _mutation(self, task, binding):
        return self.execution.transaction(task["task_id"], task["revision"], task["stop_epoch"],
                                          binding["revision"])

    def _receipt(self, task_id, ref, kind, request_id=None):
        body = _strict_json(self.store._read_blob(task_id, ref), "STATE_CORRUPT")
        _require(body["schema_version"] == 1 and body["task_id"] == task_id and body["kind"] == kind
                 and (request_id is None or body["request_id"] == request_id))
        return body

    def _retain(self, task_id, kind, **fields):
        return self.store.put_evidence(task_id, canonical_json(dict(
            schema_version=1, task_id=task_id, kind=kind, recorded_at=self.clock(), **fields)))

    @_state_errors
    @_retry_busy
    def ask(self, actor, task_id, *, kind, question, reason, expires_at=None,
            continuation=None, purpose_ref=None):
        """Record a controller's scoped question. Sending is a separate host action."""
        _identifier(task_id, "task")
        principal = self.store._principal(actor)
        scope = self._scope(task_id, principal)
        task, spec, binding = self._task(task_id)
        _require(isinstance(kind, str) and kind in _ASK_KINDS and _text(question) and _text(reason)
                 and (expires_at is None or (_time(expires_at) and expires_at > self.clock()))
                 and continuation in (None, "resume", "recheck")
                 and (kind == "host_handoff") == (continuation == "recheck")
                 and (_matches(_REF, purpose_ref) if kind in _GRANT_KINDS else purpose_ref is None),
                 "INVALID_EVIDENCE")
        generation, open_ = self._kernel(task)
        _require(open_ and task["run_id"] is not None, "STALE_REQUEST")
        request = dict(task_id=task_id, repo_key=scope.repo_key, spec_sha256=scope.spec_sha256,
            run_id=task["run_id"], generation=generation, stop_epoch=task["stop_epoch"],
            kind=kind, question=question, reason=reason, expires_at=expires_at, continuation=continuation,
            purpose_ref=purpose_ref, created_by=principal.actor_id if isinstance(
                principal, agent_registry.ActorContext) else None, binding_revision=binding["revision"],
            controller_ref=binding["controller_ref"], interaction_ref=binding["interaction_ref"])
        _require(self._allowed(principal, scope, task, binding, "ask", request, generation), "FORBIDDEN")
        status, _, _ = self._view(task, spec)
        _require(status["status"] != "completed" or (kind == "decision" and continuation is None),
                 "STALE_REQUEST")
        if kind == "host_handoff":
            _, receipts = self.store._run(task, task["run_id"])
            _require(purpose_ref == self.host_handoff_purpose(spec) and receipts
                     and receipts[-1][1]["phase"] == "settled"
                     and receipts[-1][1]["outcome"] == "needs_host"
                     and receipts[-1][1]["generation"] == generation, "FORBIDDEN")
        # Keep the canonical ID of an unanswered request across a route transfer.
        # A decided request retains its history and can only deduplicate in its own route.
        routing = {"created_by", "binding_revision", "controller_ref", "interaction_ref"}
        for row in self._rows(task):
            prior = row["request"]
            keys = request.keys() - routing if not row["answer"] else request.keys()
            if all(prior[k] == request[k] for k in keys):
                return self._public(task, row)
            if (continuation and prior["continuation"] == continuation
                    and self._current(prior, task, generation, open_)
                    and (not row["answer"] or self._answer_current(task, row, binding))):
                raise TaskError("DECISION_CONFLICT", task_id)
        request.update(request_id=new_id("request"), created_at=self.clock())
        row = dict(request=request, answer=None, consumption=None, presentation=None, outbox={})
        with self._mutation(task, binding) as (task_fd, latest, _):
            now, still_open = self._kernel(latest)
            _require(self._current(request, latest, now, still_open), "STALE_REQUEST")
            with self._records(task_id, create=True, task_fd=task_fd) as fd:
                _save(fd, request["request_id"] + ".json", row, exclusive=True)
        return self._public(task, row)

    def _manual(self, task, spec, status):
        product = status["product"]
        if status["status"] != "completed" or product is None:
            return None
        version = self._version(product)
        request_id = "request-" + _sha(canonical_json([task["task_id"], task["spec_sha256"], version]))[:32]
        criteria = [row for row in spec["worker_contract"]["acceptance"] if row["kind"] == "manual"]
        question = "\n".join(row["description"] for row in criteria)
        return dict(request_id=request_id, task_id=task["task_id"], kind=_MANUAL,
                    question=question.encode("utf-8")[:8192].decode("utf-8", "ignore"),
                    product_id=product["product_id"], checks_sha256=product["checks_sha256"],
                    spec_sha256=task["spec_sha256"], created_at=status["run_history"][-1]["settled_at"])

    def _view(self, task, spec):
        verified, status, generation = self.store._read_projection(task["task_id"])
        _require(task["revision"] == verified["revision"], "BUSY")
        if generation is None and task["worktree_removed"] and status["product"]:
            generation = self._removed_generation(task)
        return status, generation, self._manual(task, spec, status)

    def _public(self, task, row):
        request = row["request"]
        return dict(request_id=request["request_id"], task_id=request["task_id"], kind=request["kind"],
                    question=request["question"], product_id=None, created_at=request["created_at"],
                    delivery=self._delivery(task, row["outbox"].get("question"))[0])

    def _visible_items(self, principal, scope, task, binding):
        generation, open_ = self._kernel(task)
        rows = [row for row in self._rows(task) if not row["answer"]
                and self._current(row["request"], task, generation, open_)
                and self._allowed(principal, scope, task, binding, "read", row["request"], generation)]
        return [self._public(task, row) for row in rows]

    @_state_errors
    @_retry_busy
    def inbox(self, actor):
        principal = self.store._principal(actor)
        items = []
        if not isinstance(principal, agent_registry.ActorContext) or principal.role not in (_ROLES.HUB, _ROLES.USER):
            return dict(items=[])
        for task_id in self._task_ids():
            scope = self.store._visible_scope(task_id, principal)
            if scope is None:
                continue
            task, spec, binding = self._task(task_id)
            if principal.session.reference != binding["interaction_ref"]:
                continue
            current = self._visible_items(principal, scope, task, binding)
            # A handoff committed while reading cannot expose the old route's inbox.
            _require(self.store.load_task(task_id)["revision"] == task["revision"]
                     and _binding(self.execution.hooks, task, spec)["revision"] == binding["revision"], "BUSY")
            items.extend(current)
        return dict(items=sorted(items, key=lambda item: (item["created_at"], item["request_id"])))

    def _locate(self, principal, request_id):
        _identifier(request_id, "request")
        for task_id in self._task_ids():
            scope = self.store._visible_scope(task_id, principal)
            if scope is None:
                continue
            task, _, binding = self._task(task_id)
            with self._records(task_id) as fd:
                row = self._row(fd, request_id, task)
            if row is not None:
                return scope, task, binding, row
        raise TaskError("NOT_FOUND")

    def _addressed(self, actor, request_id, action):
        principal = self.store._principal(actor)
        scope, task, binding, row = self._locate(principal, request_id)
        route = "controller_ref" if action == "read_answer" else "interaction_ref"
        _require(isinstance(principal, agent_registry.ActorContext)
                 and principal.session.reference == binding[route], "NOT_FOUND")
        generation, open_ = self._kernel(task)
        item = row["request"]
        # Check role and scope before exposing whether an old item is stale.
        if item["generation"] != generation:
            _require(self._allowed(principal, scope, task, binding, action, item, item["generation"])
                     or self._allowed(principal, scope, task, binding, action, item, generation), "FORBIDDEN")
        else:
            _require(self._allowed(principal, scope, task, binding, action, item, generation), "FORBIDDEN")
        _require(self._current(item, task, generation, open_), "STALE_REQUEST")
        latest, _, current_binding = self._task(task["task_id"])
        _require(latest["revision"] == task["revision"] and current_binding == binding, "BUSY")
        return principal, scope, task, binding, row, item

    @_state_errors
    @_retry_busy
    def read(self, actor, request_id):
        _, _, task, _, row, _ = self._addressed(actor, request_id, "read")
        _require(row["answer"] is None, "STALE_REQUEST")
        return self._public(task, row)

    @_state_errors
    @_retry_busy
    def read_answer(self, actor, request_id):
        """Addressed private origin read. This does not claim a continuation."""
        _, _, task, binding, row, _ = self._addressed(actor, request_id, "read_answer")
        _require(row["answer"], "STALE_REQUEST")
        answer = self._answer_current(task, row, binding)
        _require(answer is not None, "STALE_REQUEST")
        return dict(self._answer_result(task, row, answer), text=answer["text"],
                    receipt_id=row["answer"])

    @_state_errors
    @_retry_busy
    def record_presentation(self, actor, request_id, source_event):
        principal, scope, task, binding, row, item = self._addressed(actor, request_id, "present")
        _require(row["answer"] is None, "STALE_REQUEST")
        event = self._event(principal, scope, binding, "present", item, None, source_event)
        task_id = task["task_id"]
        if row["presentation"]:
            self._receipt(task_id, row["presentation"], "presentation", request_id)
            return dict(request_id=request_id, receipt_id=row["presentation"])
        receipt = self._retain(task_id, "presentation", request_id=request_id,
            binding_revision=binding["revision"], interaction_ref=binding["interaction_ref"],
            actor_ref=principal.actor_id, event_ref=event.reference)
        with self._mutation(task, binding) as (task_fd, latest, _):
            generation, open_ = self._kernel(latest)
            _require(self._current(item, latest, generation, open_), "STALE_REQUEST")
            with self._records(task_id, create=True, task_fd=task_fd) as fd:
                _require(self._row(fd, request_id, latest) == row, "BUSY")
                _save(fd, request_id + ".json", dict(row, presentation=receipt))
        return dict(request_id=request_id, receipt_id=receipt)

    def _answer_current(self, task, row, binding):
        if row["answer"] is None:
            return None
        answer = self._receipt(task["task_id"], row["answer"], "answer", row["request"]["request_id"])
        _require(answer["request_sha256"] == _sha(canonical_json(row["request"]))
                 and answer["spec_sha256"] == task["spec_sha256"] and _text(answer["text"])
                 and _time(answer["recorded_at"]) and _matches(_REF, answer["actor_ref"])
                 and _matches(_REF, answer["event_ref"]))
        if (answer["binding_revision"] == binding["revision"]
                and answer["interaction_ref"] == binding["interaction_ref"]):
            return answer
        return None

    def _answer_result(self, task, row, answer):
        return dict(request_id=answer["request_id"], task_id=task["task_id"], state="answered",
                    delivery=self._delivery(task, row["outbox"].get("answer"))[0],
                    recorded_at=answer["recorded_at"])

    @_state_errors
    @_retry_busy
    def record_answer(self, actor, request_id, text, source_event):
        _require(_text(text), "INVALID_EVIDENCE")
        principal, scope, task, binding, row, item = self._addressed(actor, request_id, "answer")
        task_id = task["task_id"]
        event = self._event(principal, scope, binding, "answer", item, text, source_event)
        _require(event.grant_ref is None or (item["kind"] in _GRANT_KINDS
                 and event.grant_ref == item["purpose_ref"]), "FORBIDDEN")
        _require(not event.performed or (item["kind"] == "host_handoff"
                                         and event.grant_ref is not None), "FORBIDDEN")
        if row["answer"]:
            prior = self._answer_current(task, row, binding)
            _require(prior is not None, "STALE_REQUEST")
            _require((prior["text"], prior["actor_ref"], prior["grant_ref"], prior["performed"])
                     == (text, principal.actor_id, event.grant_ref, event.performed), "DECISION_CONFLICT")
            return self._answer_result(task, row, prior)
        receipt = self._retain(task_id, "answer", request_id=request_id,
            request_sha256=_sha(canonical_json(item)), spec_sha256=task["spec_sha256"],
            actor_ref=principal.actor_id, event_ref=event.reference, text=text,
            binding_revision=binding["revision"], interaction_ref=binding["interaction_ref"],
            grant_ref=event.grant_ref, performed=event.performed)
        answer = self._receipt(task_id, receipt, "answer", request_id)
        reservation = self._reserve_delivery(task, binding, item, "answer", receipt)
        updated = dict(row, answer=receipt)
        if reservation:
            updated["outbox"] = dict(row["outbox"], answer=reservation)
        with self._mutation(task, binding) as (task_fd, latest, _):
            generation, open_ = self._kernel(latest)
            _require(self._current(item, latest, generation, open_), "STALE_REQUEST")
            with self._records(task_id, task_fd=task_fd) as fd:
                _require(self._row(fd, request_id, latest) == row, "BUSY")
                _save(fd, request_id + ".json", updated)
        return self._answer_result(task, updated, answer)

    @staticmethod
    def _version(product):
        return {key: product[key] for key in ("product_id", "checks_sha256")}

    def _accept_name(self, product):
        return "accept-" + _sha(canonical_json(self._version(product))) + ".json"

    def _accepted(self, task, product):
        with self._records(task["task_id"]) as fd:
            if fd is None or _existing(fd, self._accept_name(product)) is None:
                return None
            pointer = _read_json(fd, self._accept_name(product))
        _fields(pointer, ("receipt_id",))
        ref = pointer["receipt_id"]
        record = self._receipt(task["task_id"], ref, "acceptance")
        _require(record["decision"]["task_id"] == task["task_id"]
                 and record["decision"]["spec_sha256"] == task["spec_sha256"]
                 and self._version(record["decision"]) == self._version(product))
        try:
            source = _acceptance_input(self.store._read_blob(task["task_id"], record["source_sha256"]))
        except TaskError:
            raise TaskError("STATE_CORRUPT") from None
        _require(source == record["decision"] and _matches(_REF, record["actor_ref"])
                 and _matches(_REF, record["event_ref"]) and _time(record["recorded_at"]))
        return ref, record

    def acceptance(self, task, product):
        """Called by T3 only for the current qualified product/check set."""
        accepted = self._accepted(task, product)
        if accepted:
            ref, record = accepted
            _, receipts = self.store._run(task, task["run_id"])
            last = receipts[-1][1]
            _require(last["phase"] == "settled" and last["outcome"] in DELIVERY_OUTCOMES)
            settlement = _strict_json(self.store._read_blob(task["task_id"], last["evidence_sha256"]),
                                      "STATE_CORRUPT")
            review = settlement["review"]
            if (record["review_receipt"] == (review["receipt_id"] if review else None)
                    and record["run_id"] == task["run_id"] and record["generation"] == last["generation"]):
                return dict(disposition="recorded", criteria=record["decision"]["criteria"], receipt_id=ref)
        with self._records(task["task_id"]) as fd:
            if fd is not None and any(re.fullmatch(r"accept-[0-9a-f]{64}\.json", name)
                                      for name in os.listdir(fd)):
                spec = self.store.load_spec(task["task_id"])
                return dict(disposition="invalidated", criteria=self._manual_criteria(spec), receipt_id=None)
        return None

    @staticmethod
    def _manual_criteria(spec):
        return sorted(row["id"] for row in spec["worker_contract"]["acceptance"] if row["kind"] == "manual")

    def local_acceptance(self, task):
        """Project the local owner's explicit decision independently of machine checks."""
        with self._records(task["task_id"]) as fd:
            if fd is None or _existing(fd, _LOCAL_ACCEPTANCE) is None:
                return None
            pointer = _read_json(fd, _LOCAL_ACCEPTANCE)
        _fields(pointer, ("receipt_id",))
        ref = pointer["receipt_id"]
        record = self._receipt(task["task_id"], ref, "local_acceptance")
        _fields(record, ("schema_version", "task_id", "kind", "recorded_at", "actor_ref",
                         "spec_sha256", "criteria", "attachment_sha256"), ("annotation_only",))
        _require(record["spec_sha256"] == task["spec_sha256"]
                 and record["actor_ref"] == task["authorization_ref"]
                 and re.fullmatch(r"local-owner:[0-9]+", record["actor_ref"])
                 and _time(record["recorded_at"]) and isinstance(record["criteria"], list)
                 and all(_matches(_ID, criterion) for criterion in record["criteria"])
                 and (record["attachment_sha256"] is None or _matches(_SHA, record["attachment_sha256"]))
                 and record.get("annotation_only", True) is True)
        summary = dict(disposition="recorded", criteria=record["criteria"], receipt_id=ref, method="local_owner")
        return summary, "annotation_only" not in record

    def _accept_local(self, actor, principal, scope, task, spec, evidence):
        task_id = task["task_id"]
        if self.local_acceptance(task) is None:
            attachment = None
            if evidence is not None:
                _, data = read_input_file(evidence, code="INVALID_EVIDENCE")
                attachment = self.store.put_evidence(task_id, data)
            ref = self._retain(task_id, "local_acceptance", actor_ref=scope.authorization_ref,
                               spec_sha256=scope.spec_sha256, criteria=self._manual_criteria(spec),
                               attachment_sha256=attachment, annotation_only=True)
            with self.store._mutation(task_id, task["revision"]) as (task_fd, latest, frozen):
                binding = _binding(self.execution.hooks, latest, frozen)
                _require(self.authorize_local_acceptance(principal, scope, binding), "FORBIDDEN")
                with self._records(task_id, create=True, task_fd=task_fd) as fd:
                    if _existing(fd, _LOCAL_ACCEPTANCE) is None:
                        _save(fd, _LOCAL_ACCEPTANCE, dict(receipt_id=ref), exclusive=True)
                        self.store._advance(task_fd, latest)
        return self.task_status(actor, task_id)

    @_state_errors
    @_retry_busy
    def accept_product(self, actor, task_id, decision=None, *, source_event=None):
        """Record a local decision or commit exact evidence under a scoped grant."""
        _identifier(task_id, "task")
        principal = self.store._principal(actor)
        scope = self._scope(task_id, principal)
        task, spec, binding = self._task(task_id)
        if (source_event is None and self.authorize_local_acceptance is not None
                and self.authorize_local_acceptance(principal, scope, binding)):
            return self._accept_local(actor, principal, scope, task, spec, decision)
        _require(task["creation"]["runtime_seal"] == self.store.runtime_seal, "RUNTIME_MISMATCH")
        _require(decision is not None, "USAGE")
        generation, _ = self._kernel(task)
        if task["worktree_removed"]:
            generation = self._removed_generation(task)
        version = dict(task_id=task_id, spec_sha256=scope.spec_sha256, run_id=task["run_id"],
                       generation=generation, stop_epoch=task["stop_epoch"])
        _require(self._allowed(principal, scope, task, binding, "accept", version, generation), "FORBIDDEN")
        _, data = read_input_file(decision, code="INVALID_EVIDENCE")
        decision = _acceptance_input(data)
        _require(decision["task_id"] == task_id and decision["spec_sha256"] == scope.spec_sha256
                 and decision["criteria"] == self._manual_criteria(spec), "INVALID_EVIDENCE")
        event = self._event(principal, scope, binding, "accept", version, decision, source_event)
        # A sealed, intentionally removed worktree can only repeat an already
        # verified decision. There is no worktree lock after that cleanup.
        product_context = (nullcontext(self.execution.task_snapshot(task_id)) if task["worktree_removed"]
                           else self.execution.product_transaction(task_id))
        with product_context as snapshot:
            _require(snapshot["revision"] == task["revision"], "BUSY")
            product = snapshot["product"]
            _require(product is not None, "CHECKS_REQUIRED")
            _require(decision["product_id"] == product["product_id"], "PRODUCT_CHANGED")
            _require(decision["checks_sha256"] == product["checks_sha256"], "CHECKS_REQUIRED")
            status, current_generation, manual = self._view(task, spec)
            _require(status["status"] == "completed" and manual, "CHECKS_REQUIRED")
            _require(current_generation == generation, "STALE_REQUEST")
            accepted = self._accepted(task, product)
            if accepted is not None:
                _, record = accepted
                _require(record["decision"] == decision and record["actor_ref"] == principal.actor_id,
                         "DECISION_CONFLICT")
            else:
                _require(not task["worktree_removed"], "CHECKS_REQUIRED")
                review = status["review"]
                ref = self._retain(task_id, "acceptance", request_id=manual["request_id"],
                    decision=decision, source_sha256=self.store.put_evidence(task_id, data),
                    actor_ref=principal.actor_id, event_ref=event.reference, binding_revision=binding["revision"],
                    interaction_ref=binding["interaction_ref"], controller_ref=binding["controller_ref"],
                    stop_epoch=snapshot["stop_epoch"], run_id=snapshot["run_id"], generation=generation,
                    review_receipt=review["receipt_id"] if review else None)
                with self.execution.transaction(task_id, snapshot["revision"], snapshot["stop_epoch"],
                                                binding["revision"]) as (task_fd, latest, _):
                    actual, still_open = self._kernel(latest)
                    _require(actual == generation and still_open, "STALE_REQUEST")
                    with self._records(task_id, create=True, task_fd=task_fd) as fd:
                        _save(fd, self._accept_name(product), dict(receipt_id=ref), exclusive=True)
        return self.task_status(actor, task_id)

    def _removed_generation(self, task):
        _, receipts = self.store._run(task, task["run_id"])
        _require(task["worktree_removed"] and receipts and receipts[-1][1]["phase"] == "settled")
        return receipts[-1][1]["generation"]

    def pending(self, task, binding):
        """Internal T3/T5 summaries. Public queries apply the route grant."""
        rows = list(self._rows(task))
        if not rows:
            return ()
        generation, open_ = self._kernel(task)
        return [dict(request_id=row["request"]["request_id"], kind=row["request"]["kind"])
                for row in rows if not row["answer"]
                and self._current(row["request"], task, generation, open_)]

    @_state_errors
    @_retry_busy
    def task_status(self, actor, task_id):
        """Actor-scoped status for the CLI, including private leaf projections."""
        _identifier(task_id, "task")
        principal = self.store._principal(actor)
        scope = self._scope(task_id, principal)
        task, spec, binding = self._task(task_id)
        status, generation, manual = self._view(task, spec)
        status["pending"] = []
        status["delivery"] = []
        addressed = (isinstance(principal, agent_registry.ActorContext)
                     and principal.session.reference == binding["interaction_ref"])
        if addressed:
            status["pending"] = self._visible_items(principal, scope, task, binding)
            for row in self._rows(task):
                request = row["request"]
                if self._allowed(principal, scope, task, binding, "read", request, generation):
                    for phase, notice in row["outbox"].items():
                        status["delivery"].append(dict(request_id=request["request_id"], phase=phase,
                                                      state=self._delivery(task, notice)[0]))
        if manual:
            request_id = manual["request_id"]
            notice = self._notice(task, request_id, "question")
            if notice and (notice.get("local_acceptance") is True or (
                    addressed and self._allowed(principal, scope, task, binding, "read", manual, generation))):
                status["delivery"].append(dict(request_id=request_id, phase="question",
                                              state=self._delivery(task, notice)[0]))
            legacy_ref = status["acceptance"]["receipt_id"]
            if legacy_ref and addressed and self._allowed(principal, scope, task, binding, "read", manual, generation):
                legacy = self._notice(task, legacy_ref, "completion")
                if legacy:
                    status["delivery"].append(dict(request_id=request_id, phase="completion",
                                                  state=self._delivery(task, legacy)[0]))
        first = self._first_successful_run(status)
        if first:
            correlation = task["run_heads"][first["run_id"]][-1]
            notice = self._notice(task, correlation, "completion")
            item = dict(request_id=None, generation=generation)
            if notice and (notice.get("local_completion") is True or (
                    addressed and self._allowed(principal, scope, task, binding, "read", item, generation))):
                status["delivery"].append(dict(request_id=None, phase="completion",
                                              state=self._delivery(task, notice)[0]))
        _require(self.store.load_task(task_id)["revision"] == task["revision"]
                 and _binding(self.execution.hooks, task, spec)["revision"] == binding["revision"], "BUSY")
        _require(self._scope(task_id, principal) == scope, "NOT_FOUND")
        return status

    def _decision_rows(self, task, rows, generation, open_, operation=None):
        """Select only the relevant source Run across admission of its successor."""
        run_id = task["run_id"]
        if operation and run_id is not None:
            run, receipts = self.store._run(task, run_id)
            phase = receipts[-1][1]["phase"] if receipts else "reserved"
            if (run["kind"] == operation and run["predecessor_run_id"]
                    and phase in {"reserved", "launching", "active"}
                    and run["generation"] == generation):
                run_id = run["predecessor_run_id"]
        return [row for row in rows if self._same_version(row["request"], task, generation, open_, run_id)
                and self._consumption(task, row) is None]

    def _unexpired(self, row):
        expires = row["request"]["expires_at"]
        return expires is None or expires > self.clock()

    def _material_delivery(self, task, rows):
        states = [self._delivery(task, row["outbox"]["answer"])[1] for row in rows
                  if "answer" in row["outbox"]]
        if "delivery_unknown" in states:
            return "delivery_unknown"
        return "waiting" if "waiting" in states else None

    def decision_status(self, task, binding):
        rows = list(self._rows(task))
        if not rows:
            return None
        delivery = self._material_delivery(task, rows)
        if delivery:
            return delivery
        generation, open_ = self._kernel(task)
        current = [row for row in self._decision_rows(task, rows, generation, open_) if self._unexpired(row)]
        if any(not row["answer"] for row in current):
            return "needs_input"
        return "waiting" if any(row["request"]["continuation"] for row in current) else None

    def _usable_answer(self, task, binding, row, answer):
        request = row["request"]
        if answer is None:
            return False
        wrong_grant = request["kind"] in _GRANT_KINDS and answer["grant_ref"] != request["purpose_ref"]
        unperformed = request["kind"] == "host_handoff" and not answer["performed"]
        if wrong_grant or unperformed:
            return False
        scope = self._scope_record(task)
        return self.verify_continuation is not None and self.verify_continuation(
            scope, deepcopy(binding), deepcopy(request), deepcopy(answer)) is True

    def _scope_record(self, task):
        with self.store._private(self.store._task_dir(task["task_id"])) as fd:
            return self.store._scope(fd, task["task_id"])

    def decision_barrier(self, task, binding, operation):
        rows = list(self._rows(task))
        if not rows:
            return None
        delivery = self._material_delivery(task, rows)
        if delivery:
            return "DELIVERY_UNKNOWN" if delivery == "delivery_unknown" else "RUN_UNSETTLED"
        generation, open_ = self._kernel(task)
        rows = self._decision_rows(task, rows, generation, open_, operation)
        unexpired = [row for row in rows if self._unexpired(row)]
        if any(not row["answer"] for row in unexpired):
            return "NOT_RESUMABLE"
        continuations = [row for row in rows if row["request"]["continuation"]]
        if continuations and not any(row["request"]["continuation"] == operation and
                self._usable_answer(task, binding, row, self._answer_current(task, row, binding))
                for row in unexpired if row in continuations):
            return "STALE_REQUEST"
        return None

    def _previous_answers(self, task, binding, run_id, generation, operation):
        return [(row, self._answer_current(task, row, binding)) for row in self._rows(task)
                if row["request"]["continuation"] == operation
                and self._same_version(row["request"], task, generation, True, run_id)]

    def _consumption(self, task, row):
        if row["consumption"] is None:
            return None
        request = row["request"]
        record = self._receipt(task["task_id"], row["consumption"], "consumption", request["request_id"])
        _require(row["answer"] and record["answer_receipt"] == row["answer"]
                 and record["generation"] == request["generation"] and _positive(record["lease_fence"])
                 and record["run_id"] in task["run_hashes"])
        return record

    @_state_errors
    @_retry_busy
    def claim_continuation(self, task, binding, run, generation):
        """Consumption by the admitted T3 child under its existing kernel lease."""
        prior_id = run["predecessor_run_id"]
        if prior_id is None:
            return None
        task_id = task["task_id"]
        task, _, current_binding = self._task(task_id)
        _require(current_binding == binding, "BINDING_CHANGED")
        _require(task["run_id"] == run["run_id"] and run["generation"] == generation, "RUN_UNSETTLED")
        candidates = self._previous_answers(task, binding, prior_id, generation, run["kind"])
        if not candidates:
            return None
        eligible = [(row, answer) for row, answer in candidates
                    if self._unexpired(row) and self._usable_answer(task, binding, row, answer)]
        _require(len(eligible) == 1, "STALE_REQUEST")
        row, answer = eligible[0]
        active_run, receipts = self.store._run(task, run["run_id"])
        owner = _active_proof(self.store, active_run, receipts)
        _require(run == active_run and owner and receipts[-1][1]["phase"] == "active"
                 and owner["pid"] == os.getpid() and owner["birth"] == ato_process.identity(os.getpid())
                 and owner["generation"] == generation, "RUN_UNSETTLED")
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            lease = transport.managed_bus().execution_lease(recovery.execution_resource(run["worktree"]))
        _require(lease and lease["state"] == "active" and lease["lease_until"] > time.time()
                 and lease["fence"] == owner["lease_fence"] and lease["task_id"] == run["workflow_dir"]
                 and lease["contract_hash"] == run["contract_hash"]
                 and lease["generation"] == canonical_json(generation).decode(), "RUN_UNSETTLED")
        _require(not self._material_delivery(task, [row]), "DELIVERY_UNKNOWN")
        request = row["request"]
        request_id = request["request_id"]
        if (record := self._consumption(task, row)) is not None:
            _require(record["run_id"] == run["run_id"] and record["lease_fence"] == lease["fence"],
                     "STALE_REQUEST")
            return row["consumption"]
        ref = self._retain(task_id, "consumption", request_id=request_id, answer_receipt=row["answer"],
                           run_id=run["run_id"], generation=generation, lease_fence=lease["fence"])
        with self._mutation(task, binding) as (task_fd, latest, _):
            now, open_ = self._kernel(latest)
            _require(self._current(request, latest, now, open_, run_id=prior_id), "STALE_REQUEST")
            _require(lease["lease_until"] > time.time(), "RUN_UNSETTLED")
            with self._records(task_id, task_fd=task_fd) as fd:
                _require(self._row(fd, request_id, latest) == row, "BUSY")
                _save(fd, request_id + ".json", dict(row, consumption=ref))
        return ref

    def host_handoff(self, task, binding, run, generation):
        """Current evidence of a performed frozen host action, never a waiver."""
        purpose = self.host_handoff_purpose(self.store.load_spec(task["task_id"]))
        candidates = self._previous_answers(task, binding, run["run_id"], generation, "recheck")
        for row, answer in candidates:
            if (purpose and row["request"]["purpose_ref"] == purpose and self._consumption(task, row) is None
                    and self._unexpired(row) and self._usable_answer(task, binding, row, answer)
                    and not self._material_delivery(task, [row])):
                return HostHandoffProof(task["task_id"], run["run_id"], task["spec_sha256"],
                                        tuple(generation), answer["event_ref"])
        return None

    @staticmethod
    def host_handoff_purpose(spec):
        handoff = spec["worker_contract"].get("host_handoff")
        return "host-handoff:" + _sha(canonical_json(handoff)) if handoff else None

    def _bus(self):
        if self.mailbox is None:
            return None
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            expected = ato_config.interaction_directory() / "mailbox" / "state.sqlite"
        mailbox = self.mailbox() if callable(self.mailbox) else self.mailbox
        if mailbox is None:
            return None
        _require(mailbox.path == expected)
        try:
            with self.store._private(expected.parent) as fd:
                info = _existing(fd, expected.name)
                if info is None:
                    return None
                _regular(info, True, "STATE_CORRUPT")
        except FileNotFoundError:
            return None
        return mailbox

    @staticmethod
    def _notice_name(correlation, phase):
        return phase + "-" + correlation + ".json"

    def _notice(self, task, correlation, phase):
        name = self._notice_name(correlation, phase)
        with self._records(task["task_id"]) as fd:
            return _read_json(fd, name) if fd is not None and _existing(fd, name) is not None else None

    def _reservation(self, task, correlation, phase):
        if phase != "completion":
            with self._records(task["task_id"]) as fd:
                row = self._row(fd, correlation, task)
            if row is not None:
                return row, row["outbox"].get(phase)
        return None, self._notice(task, correlation, phase)

    @staticmethod
    def _notice_key(notice):
        key = "ato:" + notice["phase"] + ":" + notice["correlation_id"]
        if "supersedes" in notice:
            key += ":" + str(notice["binding_revision"])
        return key

    def _message(self, task, notice):
        _fields(notice, ("correlation_id", "phase", "binding_revision", "generation", "stop_epoch",
                         "run_id", "sender", "target", "source_ref", "mailbox_id", "expires_at",
                         "body", "from_ref", "to_ref", "route_generation", "superseded"),
                ("supersedes", "local_acceptance", "local_completion"))
        _require(type(notice["superseded"]) is bool
                 and ("local_acceptance" not in notice or
                      (notice["local_acceptance"] is True and notice["phase"] == "question"))
                 and ("local_completion" not in notice or
                      (notice["local_completion"] is True and notice["phase"] == "completion"))
                 and ("supersedes" not in notice or _matches(_SHA, notice["supersedes"])))
        bus = self._bus()
        if bus is None:
            return None
        identifier = notice["mailbox_id"]
        if identifier is None:
            # Recover an enqueue interrupted before the leaf published its
            # pointer. Read the mailbox's own durable idempotency index.
            key = self._notice_key(notice)
            with bus._transaction() as con:
                entry = con.execute("select id from messages where sender=? and idempotency_key=?",
                                    (notice["sender"], key)).fetchone()
                if entry is None:
                    return None
                row = bus._message(con, entry["id"])
        else:
            _require(_matches(_MAILBOX_ID, identifier))
            try:
                row = bus.message(identifier)
            except MailboxError:
                return None
            _require(row["route_generation"] == notice["route_generation"])
        _require((row["task_id"], row["contract_hash"], row["canonical_id"], row["correlation_id"],
                  row["kind"], row["protocol"], row["generation"], row["expires_at"],
                  row["sender"], row["target"], row["authority_source"], row["body"])
                 == (task["task_id"], task["spec_sha256"], notice["correlation_id"], notice["correlation_id"],
                     "ato_" + notice["phase"], 2, canonical_json(notice["generation"]).decode(),
                     notice["expires_at"], notice["sender"], notice["target"], notice["source_ref"],
                     notice["body"]))
        return row

    def _delivery(self, task, notice):
        if notice is None:
            return "recorded_only", None
        row = self._message(task, notice)
        if row is None:
            if notice["superseded"]:
                return "cancelled", None
            return "delivery_unknown", "delivery_unknown"
        state = row["state"]
        if state in {"dispatching", "delivery_unknown"}:
            return "delivery_unknown", "delivery_unknown"
        if state in {"cancelled", "expired"} and row["external_possible"]:
            reconciled = any(event["kind"] == "native_reconciled"
                             for event in self._bus().events(message_id=row["id"]))
            if not reconciled:
                return "delivery_unknown", "delivery_unknown"
        if state in {"queued", "claimed", "delivered", "accepted", "responded"}:
            return state, "waiting"
        return state, None

    def _local_completion_route(self, task, binding, phase, item):
        if self.local_completion_route is not None and phase == "completion":
            return self.local_completion_route(self._scope_record(task), deepcopy(binding), item)
        return None

    def _delivery_route(self, task, binding, phase, from_ref, to_ref, item):
        if self._bus() is None:
            return None
        route = self._local_completion_route(task, binding, phase, item)
        if route is None and self.route is not None:
            route = self.route(self._scope_record(task), deepcopy(binding), phase, from_ref, to_ref)
        if route is not None:
            _require(type(route) is MailboxRoute and _matches(_REF, route.source_ref)
                     and type(route.sender) is EndpointCredential and type(route.target) is EndpointCredential
                     and route.sender.endpoint_id != route.target.endpoint_id, "FORBIDDEN")
            self._bus().authenticate(route.sender)
            self._bus().authenticate(route.target)
        return route

    def _reserve_delivery(self, task, binding, item, phase, receipt=None):
        outward = phase != "answer"
        source = binding["controller_ref"] if outward else binding["interaction_ref"]
        target = binding["interaction_ref"] if outward else binding["controller_ref"]
        route = self._delivery_route(task, binding, phase, source, target, item)
        if route is None:
            return None
        correlation = receipt if phase in {"completion", "failure"} else item["request_id"]
        # All native messages refer to the canonical record. The guarded
        # reader, rather than a native message, supplies any decision text.
        body = canonical_json(dict(task_id=task["task_id"], request_id=item["request_id"],
                                   phase=phase, receipt_id=receipt)).decode()
        if route.local_completion:
            body = render_local_completion(task, binding, phase, correlation, item, target)
        elif self.render_notice is not None:
            body = self.render_notice(task, binding, phase, correlation, item, target)
        notice = dict(correlation_id=correlation, phase=phase, binding_revision=binding["revision"],
                    generation=item["generation"], stop_epoch=task["stop_epoch"], run_id=task["run_id"],
                    sender=route.sender.endpoint_id, target=route.target.endpoint_id,
                    from_ref=source, to_ref=target, source_ref=route.source_ref, mailbox_id=None,
                    route_generation=None, superseded=False, expires_at=item.get("expires_at"), body=body)
        if route.local_completion:
            notice["local_completion"] = True
        return notice

    def _notification_eligible(self, task, binding, phase, item):
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            policy = notification_state.status()
            if any(Path(rule["repo"]).resolve() == Path(task["repo"]["root"])
                   for rule in notification_state.project_mutes()):
                return False
        if self._local_completion_route(task, binding, phase, item) is not None:
            return True
        if policy.get("hub_thread") != binding["interaction_ref"]:
            return False
        if phase == "failure":
            import failure_attention
            with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
                failure = failure_attention.policy()
            since = failure.get("configured_at")
            return (failure.get("enabled") is True and failure.get("hub_thread") == binding["interaction_ref"]
                    and _time(since) and task["created_at"] >= since)
        if phase == "completion":
            since = policy.get("completion_since")
            return policy.get("completion") == "once" and _time(since) and item["created_at"] >= since
        return policy.get("mode") in notification_state.MODES

    def _delivery_item(self, task, spec, correlation, phase):
        """Resolve only current material for a trusted delivery pump."""
        generation, open_ = self._kernel(task)
        if _valid_id(correlation, "request") and phase != "completion":
            with self._records(task["task_id"]) as fd:
                row = self._row(fd, correlation, task)
            if row:
                item = row["request"]
                _require(self._current(item, task, generation, open_), "STALE_REQUEST")
                _require((phase == "answer" and row["answer"] and not row["consumption"])
                         or (phase == "question" and not row["answer"]), "STALE_REQUEST")
                return row, item, row["outbox"].get(phase)
        status, product_generation, _ = self._view(task, spec)
        if phase == "failure":
            failed = self.failure_item(task, status, generation)
            _require(open_ and failed is not None and failed[0] == correlation, "STALE_REQUEST")
            return None, failed[1], self._notice(task, correlation, phase)
        completion = self.completion_item(task, status, product_generation)
        _require(phase == "completion" and completion and completion[0] == correlation, "STALE_REQUEST")
        return None, completion[1], self._notice(task, correlation, phase)

    @staticmethod
    def _first_successful_run(status):
        return next((run for run in status["run_history"] if run["phase"] == "settled"
                     and run["outcome"] in DELIVERY_OUTCOMES), None)

    def completion_item(self, task, status, generation):
        first = self._first_successful_run(status)
        execution = status["execution"]
        if (generation is None or first is None or first["run_id"] != task["run_id"]
                or status["status"] != "completed" or status["product"] is None
                or execution["state"] != "idle" or not execution["verified"] or execution["stop_requested"]):
            return None
        receipt = task["run_heads"][task["run_id"]][-1]
        return receipt, dict(task_id=task["task_id"], request_id=None, generation=generation,
                             kind="completion", created_at=first["settled_at"])

    @staticmethod
    def failure_item(task, status, generation):
        history = status["run_history"]
        execution = status["execution"]
        if (generation is None or not history or status["status"] != "blocked"
                or status["pending"] or execution["state"] != "idle" or not execution["verified"]
                or execution["stop_requested"] or history[-1]["phase"] != "settled"
                or history[-1]["outcome"] not in {"blocked", "failed"}):
            return None
        receipt = task["run_heads"][task["run_id"]][-1]
        return receipt, dict(task_id=task["task_id"], request_id=None, generation=generation,
                             kind="failure", created_at=history[-1]["settled_at"])

    def _save_delivery(self, task, binding, row, correlation, notice, *, superseding=False):
        task_id, phase = task["task_id"], notice["phase"]
        with self._mutation(task, binding) as (task_fd, latest, _):
            if not superseding and not latest["worktree_removed"]:
                generation, open_ = self._kernel(latest)
                _require(generation == notice["generation"] and open_, "STALE_REQUEST")
            with self._records(task_id, create=True, task_fd=task_fd) as fd:
                if row is None:
                    _save(fd, self._notice_name(correlation, phase), notice)
                else:
                    _require(self._row(fd, correlation, latest) == row, "BUSY")
                    _save(fd, correlation + ".json", dict(row, outbox=dict(row["outbox"], **{phase: notice})))

    @staticmethod
    def _delivery_id(task_id, correlation, phase):
        _identifier(task_id, "task")
        _require((phase in ("completion", "failure") and _matches(_SHA, correlation))
                 or (phase in ("question", "answer") and _valid_id(correlation, "request")), "INVALID_ID")

    @_state_errors
    def delivery_reference(self, task_id, correlation, phase):
        """Private, read-only reference for correlated mailbox reconciliation."""
        self._delivery_id(task_id, correlation, phase)
        task = self.store.load_task(task_id)
        _, notice = self._reservation(task, correlation, phase)
        message = self._message(task, notice) if notice else None
        return dict(state=self._delivery(task, notice)[0],
                    mailbox_id=message["id"] if message else None)

    @_state_errors
    @_retry_busy
    def supersede_unsubmitted(self, task_id, correlation, phase, sender):
        """Private handoff callback. Keep any possible native effect fenced."""
        self._delivery_id(task_id, correlation, phase)
        task, _, binding = self._task(task_id)
        row, notice = self._reservation(task, correlation, phase)
        if notice is None:
            return "recorded_only"
        _require(notice["binding_revision"] < binding["revision"], "STALE_REQUEST")
        bus = self._bus()
        _require(bus is not None and isinstance(sender, EndpointCredential)
                 and sender.endpoint_id == notice["sender"], "FORBIDDEN")
        bus.authenticate(sender)
        message = self._message(task, notice)
        if message is not None:
            if message["external_possible"]:
                return self._delivery(task, notice)[0]
            message = bus.cancel(sender, message["id"], source="verified unified binding supersession")
            if message["external_possible"]:
                return self._delivery(task, notice)[0]
        if not notice["superseded"]:
            self._save_delivery(task, binding, row, correlation, dict(notice, superseded=True), superseding=True)
        return self._delivery(task, notice | {"superseded": True})[0]

    @_state_errors
    def dispatch_delivery(self, task_id, correlation, phase):
        """Trusted foreground host pump for one canonical envelope.

        Only this guarded entry may dispatch these envelopes. Reconciliation
        and endpoint lease ownership stay with the existing managed mailbox.
        """
        self._delivery_id(task_id, correlation, phase)
        task, spec, binding = self._task(task_id)
        row, item, notice = self._delivery_item(task, spec, correlation, phase)
        prior = notice if (notice and notice["superseded"] and self.render_notice is not None
                           and phase == "question") else None
        if notice is None or prior is not None:
            if phase == "answer" or (row and row["presentation"]):
                return "recorded_only"
            if not self._notification_eligible(task, binding, phase, item):
                return "recorded_only"
            notice = self._reserve_delivery(task, binding, item, phase,
                                            correlation if phase in {"completion", "failure"} else None)
            if notice is None:
                return "recorded_only"
            if prior:
                _require(prior["binding_revision"] < binding["revision"]
                         and self._delivery(task, prior)[0] == "cancelled", "STALE_REQUEST")
                notice["supersedes"] = self.store.put_evidence(task_id, canonical_json(prior))
            self._save_delivery(task, binding, row, correlation, notice)
            task, spec, binding = self._task(task_id)
            row, item, notice = self._delivery_item(task, spec, correlation, phase)
        bus = self._bus()
        _require(bus is not None, "DELIVERY_UNKNOWN")
        bus.recover()
        message = self._message(task, notice)
        if message is not None and message["state"] != "queued" and (
                self.observe_envelope is None or message["state"] not in {
                    "dispatching", "delivered", "delivery_unknown", "accepted", "responded"}):
            return self._delivery(task, notice)[0]

        route = self._delivery_route(task, binding, phase, notice["from_ref"], notice["to_ref"], item)
        _require(route and (route.sender.endpoint_id, route.target.endpoint_id, route.source_ref,
                            route.local_completion) == (notice["sender"], notice["target"], notice["source_ref"],
                                                        notice.get("local_completion", False)), "STALE_REQUEST")

        def guard():
            latest, frozen, current = self._task(task_id)
            current_row, current_item, reserved = self._delivery_item(latest, frozen, correlation, phase)
            _require(current["revision"] == notice["binding_revision"]
                     and latest["run_id"] == notice["run_id"] and latest["stop_epoch"] == notice["stop_epoch"]
                     and current_item["generation"] == notice["generation"] and reserved == notice
                     and not notice["superseded"],
                     "STALE_REQUEST")
            if phase != "answer":
                _require(not (current_row and current_row["presentation"])
                         and self._notification_eligible(latest, current, phase, current_item), "STALE_REQUEST")
            if route.local_completion:
                _require(self._local_completion_route(latest, current, phase, current_item) == route,
                         "STALE_REQUEST")

        guard()
        if message is None and notice["mailbox_id"] is None:
            # Scope the route and key to the original reservation. A restart
            # can recover its queued row, while a changed route cannot create one.
            bound = bus.bind_route(sender=notice["sender"], target=notice["target"],
                task_id=task_id, contract_hash=task["spec_sha256"], source=notice["source_ref"],
                generation=notice["generation"], advance=True,
                cross_project=route.sender.project != route.target.project)
            message = bus.enqueue(route.sender, binding_id=bound["id"], target=notice["target"],
                kind="ato_" + phase, body=notice["body"], idempotency_key=self._notice_key(notice),
                task_id=task_id, contract_hash=task["spec_sha256"], correlation_id=correlation,
                protocol=2, generation=notice["generation"], canonical_id=correlation,
                authority_source=notice["source_ref"], expires_at=notice["expires_at"])
        _require(message is not None, "DELIVERY_UNKNOWN")
        if notice["mailbox_id"] is None:
            updated = dict(notice, mailbox_id=message["id"], route_generation=message["route_generation"])
            try:
                self._save_delivery(task, binding, row, correlation, updated)
            except TaskError as exc:
                if exc.code in {"BINDING_CHANGED", "STALE_REQUEST"}:
                    bus.cancel(route.sender, message["id"], source="unified reservation became stale")
                raise
            notice = updated
        if message["state"] != "queued":
            self.observe_envelope(bus, route, notice["mailbox_id"], guard)
        elif self.dispatch_envelope is not None:
            self.dispatch_envelope(bus, route, notice["mailbox_id"], guard)
        elif route.owner_token and route.transports:
            transport.dispatch_one(bus, route.target, route.owner_token, route.transports,
                                   message_id=notice["mailbox_id"], guard=guard)
        return self._delivery(task, notice)[0]
