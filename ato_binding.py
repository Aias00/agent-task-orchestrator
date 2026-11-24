"""Task-scoped controller, interaction and Reviewer bindings."""
from dataclasses import asdict, dataclass
import os
import time

from ato_execution import _initial_binding, _raw_stop
from ato_task_store import (TaskError, TaskScope, _REF, _SHA, _existing, _fields, _identifier,
                            _matches, _positive, _read_json, _require, _retry_busy, _save,
                            _sha, _state_errors, _strict_json, _valid_id, canonical_json,
                            read_input_file)
from hub_contract import HubAgentSpec, HubError


_HEAD = "current.json"
_OPERATIONS = {
    "handoff": ("from", "to", "interaction"),
    "reviewer": ("reviewer",),
}


@dataclass(frozen=True)
class BindingGrant:
    """Verified by the host registry for one role and one Task binding."""

    scope: TaskScope
    ref: str
    role: str
    revision: int
    operations: frozenset[str] = frozenset()


@dataclass(frozen=True)
class BindingDecision:
    """Host-verified actual user event authorizing the exact evidence file."""

    user: BindingGrant
    source_ref: str
    evidence_sha256: str


def _agent(value, code):
    if type(value) is HubAgentSpec:
        return asdict(value)
    _fields(value, ("kind",), ("model", "effort"), code=code)
    try:
        return asdict(HubAgentSpec(**value))
    except HubError:
        raise TaskError(code) from None


def _scope_record(task):
    return dict(task_id=task["task_id"], repo_key=task["repo"]["key"],
                spec_sha256=task["spec_sha256"], authorization_ref=task["authorization_ref"])


def _validate_transition(row, spec, previous, revision):
    before, after = row["before"], row["after"]
    _fields(after, ("task_id", "revision", "controller_ref", "interaction_ref",
                    "reviewer", "review_revision"))
    _require(before == previous and after["task_id"] == previous["task_id"]
             and _positive(after["revision"]) and after["revision"] == revision
             and _positive(after["review_revision"])
             and _matches(_REF, after["controller_ref"]) and _matches(_REF, after["interaction_ref"])
             and after["controller_ref"] != after["interaction_ref"], "BINDING_CHANGED")
    if row["operation"] == "handoff":
        _require((after["controller_ref"] != before["controller_ref"]
                  or after["interaction_ref"] != before["interaction_ref"])
                 and after["reviewer"] == before["reviewer"]
                 and after["review_revision"] == before["review_revision"], "BINDING_CHANGED")
    else:
        _require(row["operation"] == "reviewer" and spec["reviewer"] is not None
                 and after["controller_ref"] == before["controller_ref"]
                 and after["interaction_ref"] == before["interaction_ref"]
                 and after["review_revision"] == before["review_revision"] + 1
                 and after["reviewer"] != before["reviewer"]
                 and after["reviewer"] == _agent(after["reviewer"], "BINDING_CHANGED")
                 and after["reviewer"]["kind"] in spec["reviewer"]["allowed_kinds"], "BINDING_CHANGED")


def _journal(store, task, spec):
    """Verify the committed chain against the frozen initial binding."""
    initial = _initial_binding(task, spec)
    if initial["reviewer"] is not None:
        initial["reviewer"] = dict(initial["reviewer"])
    scope = _scope_record(task)
    with store._private(store._task_dir(task["task_id"]) / "bindings") as fd:
        if _existing(fd, _HEAD) is None and not any(name.startswith("change-") for name in os.listdir(fd)):
            return initial, []
        try:
            head = _read_json(fd, _HEAD, code="BINDING_CHANGED")
        except FileNotFoundError:
            raise TaskError("BINDING_CHANGED") from None
        _fields(head, ("scope", "revision", "receipt_id"), code="BINDING_CHANGED")
        revision, receipt = head["revision"], head["receipt_id"]
        _require(head["scope"] == scope and _positive(revision)
                 and revision >= initial["revision"], "BINDING_CHANGED")
        chain = []
        while revision > initial["revision"]:
            _require(_matches(_SHA, receipt), "BINDING_CHANGED")
            try:
                row = _read_json(fd, f"change-{revision}-{receipt}.json", code="BINDING_CHANGED")
            except FileNotFoundError:
                raise TaskError("BINDING_CHANGED") from None
            _fields(row, ("schema_version", "operation", "scope", "before", "after", "previous_receipt",
                          "authorization", "observation", "recorded_at"), code="BINDING_CHANGED")
            _require(_sha(canonical_json(row)) == receipt
                     and _positive(row["schema_version"]) and row["schema_version"] == 1
                     and row["scope"] == scope, "BINDING_CHANGED")
            chain.append((receipt, row))
            revision, receipt = revision - 1, row["previous_receipt"]
        _require(receipt is None, "BINDING_CHANGED")
        previous = initial
        for _, row in reversed(chain):
            _validate_transition(row, spec, previous, previous["revision"] + 1)
            previous = row["after"]
    return initial, chain


def _head(store, task, spec):
    initial, chain = _journal(store, task, spec)
    if chain:
        receipt, row = chain[0]
        return row["after"], receipt
    return initial, None


def _publish_change(fd, row):
    revision = row["after"]["revision"]
    receipt = _sha(canonical_json(row))
    # Seed the initial pointer before staging immutable receipts. Readers
    # retain that binding if staging is interrupted before pointer publication.
    if _existing(fd, _HEAD) is None:
        _require(row["previous_receipt"] is None, "BINDING_CHANGED")
        _save(fd, _HEAD, dict(scope=row["scope"], revision=revision - 1, receipt_id=None), exclusive=True)
    _save(fd, f"change-{revision}-{receipt}.json", row, exclusive=True)
    _save(fd, _HEAD, dict(scope=row["scope"], revision=revision, receipt_id=receipt))
    return receipt


@_state_errors
def read_binding(store, task, spec):
    """Trusted, read-only ExecutionHooks.binding adapter. No actor or vendor calls."""
    return _head(store, task, spec)[0]


@_state_errors
def committed_binding_changes(store, task, spec):
    """Private history for reconciliation and audit, oldest change first."""
    _, chain = _journal(store, task, spec)
    return [dict(row, receipt_id=receipt) for receipt, row in reversed(chain)]


def _granted(grant, scope, revision, role, ref=None, operation=None):
    return (type(grant) is BindingGrant and grant.scope == scope
            and _positive(grant.revision) and grant.revision == revision and grant.role == role
            and _matches(_REF, grant.ref) and (ref is None or grant.ref == ref)
            and type(grant.operations) is frozenset
            and (operation is None or operation in grant.operations))


def _read_decision(path, operation):
    _, data = read_input_file(path, code="INVALID_EVIDENCE")
    decision = _strict_json(data, "INVALID_EVIDENCE")
    _fields(decision, ("schema_version", "task_id", "spec_sha256", "binding_revision", "source",
                       *_OPERATIONS[operation]), code="INVALID_EVIDENCE")
    _require(_positive(decision["schema_version"]) and decision["schema_version"] == 1
             and _valid_id(decision["task_id"], "task") and _matches(_SHA, decision["spec_sha256"])
             and _positive(decision["binding_revision"])
             and _matches(_REF, decision["source"]), "INVALID_EVIDENCE")
    return decision, _sha(data)


def _owner(execution):
    return {key: execution.get(key) for key in ("run_id", "state", "pid", "owner_role", "verified",
                                               "stop_requested", "lease", "activity")}


def _observation(task, status, generation, epoch):
    # Project only code-owned facts and opaque evidence IDs. In particular,
    # request text, Git paths, argv, reviewer verdict text and native IDs stay out.
    stage, product, review = (status[key] for key in ("stage", "product", "review"))
    requests = [item["request_id"] for item in status["pending"]]
    _require(all(_valid_id(ref, "request") for ref in requests))
    receipts = task["run_heads"].get(task["run_id"], [])
    return dict(task_revision=task["revision"], stop_epoch=epoch, generation=generation, observed_at=time.time(),
                run_receipt_id=receipts[-1] if receipts else None,
                run_owner=_owner(status["execution"]),
                stage={key: stage[key] for key in ("state", "stage_id", "attempts", "elapsed_seconds",
                                                  "planning_attempts")} if stage else None,
                unresolved_request_ids=sorted(set(requests)),
                product={key: product[key] for key in ("product_id", "checks_sha256")} if product else None,
                check_receipt_ids=[row["receipt_id"] for row in status["checks"] if row["receipt_id"]],
                review_receipt_id=review["receipt_id"] if review else None)


class BindingService:
    """Compare-and-swap bindings through the composed T3 transaction.

    The three authority callbacks are trusted, local registry reads. They
    must return fresh verified records, including revocation and eligibility
    checks, without invoking Git, the mailbox or native processes under the
    Task lock. `actor_grant(principal, scope, binding, operation)` returns a
    BindingGrant. `endpoint_grant(ref, scope, role, revision)` verifies an
    enrolled endpoint for that revision (including a prospective revision).
    `verify_decision(scope, operation, decision, evidence_sha256)` verifies
    exact user decision provenance and returns a BindingDecision.

    `reviewer_supported(scope, agent, session_mode)` checks the trusted
    adapter profile before publication, outside the Task lock. T3 repeats
    capability and execution admission checks when running a review.
    """

    def __init__(self, execution, *, actor_grant, endpoint_grant, verify_decision, reviewer_supported=None,
                 handoff_observe=None, historical_endpoint=None):
        self.execution = execution
        self.store = execution.store
        self.actor_grant = actor_grant
        self.endpoint_grant = endpoint_grant
        self.verify_decision = verify_decision
        self.reviewer_supported = reviewer_supported
        self.handoff_observe = handoff_observe
        self.historical_endpoint = historical_endpoint or endpoint_grant

    def _authorize_actor(self, principal, scope, binding, operation, *, successor=None):
        grant = self.actor_grant(principal, scope, binding, operation)
        revision = binding["revision"]
        if _granted(grant, scope, revision, "controller", binding["controller_ref"], operation):
            return
        if (_granted(grant, scope, revision, "user", operation=operation)
                and grant.ref not in (binding["controller_ref"], binding["interaction_ref"])):
            return
        if successor is not None:
            # The enrolled recipient can record its own explicitly authorized
            # transfer with an exact prospective operation grant.
            prospective = self.actor_grant(principal, scope, successor, operation)
            _require(_granted(prospective, scope, successor["revision"], "controller",
                              successor["controller_ref"], operation), "FORBIDDEN")
            return
        _require(operation == "binding" and _granted(grant, scope, revision, "interaction",
                 binding["interaction_ref"], operation), "FORBIDDEN")

    def _authorize_endpoints(self, scope, binding, *, historical=False):
        revision = binding["revision"]
        for role in ("controller", "interaction"):
            ref = binding[role + "_ref"]
            lookup = self.historical_endpoint if historical else self.endpoint_grant
            grant = lookup(ref, scope, role, revision)
            _require(_granted(grant, scope, revision, role, ref), "FORBIDDEN")

    def _current(self, actor, task_id, *, observe=None):
        _identifier(task_id, "task")
        principal = self.store._principal(actor)
        scope = self.store._visible_scope(task_id, principal, observe=observe)
        _require(scope is not None, "NOT_FOUND")
        with self.store._private(self.store._task_dir(task_id)) as fd:
            task, spec = self.store._task(fd, task_id)
            _require(self.store._scope(fd, task_id) == scope, "NOT_FOUND")
            binding, receipt = _head(self.store, task, spec)
        return principal, scope, task, spec, binding, receipt

    def _observe(self, scope, task, spec, binding, receipt):
        task_id = task["task_id"]
        with self.store._private(self.store._task_dir(task_id)) as fd:
            _require(_read_json(fd, "task.json") == task, "BUSY")
            journal = _raw_stop(fd, task)
            status, generation = self.store._project(task, spec)
            latest = _read_json(fd, "task.json")
            _require(latest == task and _raw_stop(fd, latest) == journal, "BUSY")
            _require(self.store._scope(fd, task_id) == scope, "NOT_FOUND")
            _require(_head(self.store, task, spec) == (binding, receipt), "BINDING_CHANGED")
        return status, generation, journal["epoch"] if journal else task["stop_epoch"]

    @_state_errors
    @_retry_busy
    def binding(self, actor, task_id):
        """Scoped current binding and the actual Run owner from T3's projection."""
        principal, scope, task, spec, binding, receipt = self._current(actor, task_id)
        self._authorize_actor(principal, scope, binding, "binding")
        status, _, _ = self._observe(scope, task, spec, binding, receipt)
        _require(self.store.can_observe(principal, scope) is True, "NOT_FOUND")
        self._authorize_actor(principal, scope, binding, "binding")
        return dict(binding, run_owner=_owner(status["execution"]), receipt_id=receipt)

    @_state_errors
    def handoff(self, actor, task_id, target, interaction, decision):
        return self._change(actor, task_id, "handoff", decision, target=target, interaction=interaction)

    @_state_errors
    def select_reviewer(self, actor, task_id, reviewer, decision):
        return self._change(actor, task_id, "reviewer", decision, reviewer=reviewer)

    def _change(self, actor, task_id, operation, path, *, target=None, interaction=None, reviewer=None):
        observe = self.handoff_observe if operation == "handoff" else None
        principal, scope, task, spec, before, previous = self._current(actor, task_id, observe=observe)
        after = dict(before, revision=before["revision"] + 1)
        successor = None
        if operation == "handoff":
            route = before["interaction_ref"] if interaction is None else interaction
            _require(all(_matches(_REF, ref) for ref in (target, route))
                     and target != route, "INVALID_EVIDENCE")
            after.update(controller_ref=target, interaction_ref=route)
            if target != before["controller_ref"]:
                successor = after
        self._authorize_actor(principal, scope, before, operation, successor=successor)
        evidence, digest = _read_decision(path, operation)
        _require(evidence["task_id"] == task_id and evidence["spec_sha256"] == scope.spec_sha256,
                 "INVALID_EVIDENCE")
        _require(evidence["binding_revision"] == before["revision"], "BINDING_CHANGED")
        self.store._repo_matches(task)
        _require(task["creation"]["phase"] == "ready", "NOT_RESUMABLE")
        if operation == "handoff":
            _require((successor is not None or route != before["interaction_ref"])
                     and evidence["from"] == before["controller_ref"] and evidence["to"] == target
                     and evidence["interaction"] == route, "INVALID_EVIDENCE")
        else:
            selected = _agent(reviewer, "INVALID_EVIDENCE")
            _require(selected == _agent(evidence["reviewer"], "INVALID_EVIDENCE")
                     and (before["reviewer"] is None or selected != _agent(before["reviewer"], "BINDING_CHANGED")),
                     "INVALID_EVIDENCE")
            policy = spec["reviewer"]
            _require(policy is not None and selected["kind"] in policy["allowed_kinds"], "FORBIDDEN")
            _require(self.reviewer_supported is not None
                     and self.reviewer_supported(scope, HubAgentSpec(**selected), policy["session_mode"]) is True,
                     "UNSUPPORTED_CAPABILITY")
            after.update(reviewer=selected, review_revision=before["review_revision"] + 1)
        status, generation, epoch = self._observe(scope, task, spec, before, previous)
        if operation == "handoff" and status["run_history"]:
            # A child without a startup receipt still has to validate its
            # launch binding. Allow changes once it has established ownership.
            _require(status["run_history"][-1]["phase"] != "launching", "RUN_UNSETTLED")
        observed = _observation(task, status, generation, epoch)
        with self.execution.transaction(task_id, task["revision"], epoch, before["revision"],
                                        reviewer_change=operation == "reviewer") as (task_fd, latest, current):
            _require(current == before and _scope_record(latest) == asdict(scope), "BINDING_CHANGED")
            _require(latest["worktree_removed"] or _existing(task_fd, "cleanup.json") is None, "CLEANUP_BLOCKED")
            _require((observe or self.store.can_observe)(principal, scope) is True, "NOT_FOUND")
            self._authorize_actor(principal, scope, current, operation, successor=successor)
            self._authorize_endpoints(scope, current, historical=operation == "handoff")
            self._authorize_endpoints(scope, after)
            proof = self.verify_decision(scope, operation, evidence.copy(), digest)
            _require(type(proof) is BindingDecision and proof.evidence_sha256 == digest
                     and proof.source_ref == evidence["source"]
                     and _granted(proof.user, scope, before["revision"], "user", operation=operation)
                     and proof.user.ref not in (before["controller_ref"], before["interaction_ref"],
                                                after["controller_ref"], after["interaction_ref"]), "FORBIDDEN")
            row = dict(schema_version=1, scope=asdict(scope), operation=operation, before=before, after=after,
                       authorization=dict(evidence_sha256=digest, source_sha256=_sha(proof.source_ref.encode()),
                                          user_sha256=_sha(proof.user.ref.encode())),
                       previous_receipt=previous, observation=observed, recorded_at=time.time())
            with self.store._private(self.store._task_dir(task_id) / "bindings") as fd:
                receipt = _publish_change(fd, row)
        return dict(after, run_owner=observed["run_owner"], receipt_id=receipt)
