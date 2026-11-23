"""Unified Task launch, execution ownership, STOP and recovery service."""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

import ato_config
import ato_core
import ato_process
import ato_product
from ato_execution_host import CommandHost, HostProfile
from ato_task_store import (RunIntent, RunReceipt, TaskError, _directory, _existing,
    _identity, _identifier, _locked_file, _publish, _read_json, _regular, _require, _save,
    _sha, _state_errors, _strict_json, _valid_id, canonical_json, new_id)
import budget
from hub_contract import HubAgentSpec, HubTurnRequest, blocker_fingerprint, validate_review_verdict
from hub_loop import GuardedWorkflow
import hub_sandbox
import hub_worktree
import recovery
import runner
import transport


_ENTRY = Path(__file__).resolve().with_name("ato_runtime.py")
_TERMINAL = {"settled", "launch_failed"}
_RETRYABLE = {"stopped", "blocked", "failed", "launch_failed"}
_STARTUP_SECONDS = 12


def _initial_binding(task, spec):
    return dict(task_id=task["task_id"], **task["binding"],
                reviewer=spec["reviewer"]["agent"] if spec["reviewer"] else None, review_revision=1)


@dataclass(frozen=True)
class ExecutionHooks:
    """Read-only trusted adapters. Child hooks come from the selected source."""
    binding: object = _initial_binding
    decision_barrier: object = lambda task, binding, operation: None
    acceptance: object = lambda task, product: None
    pending: object = lambda task, binding: ()
    decision_status: object = lambda task, binding: None
    host_handoff: object = lambda task, binding, run, generation: None
    claim_continuation: object = lambda task, binding, run, generation: None


@dataclass(frozen=True)
class HostHandoffProof:
    """T4's authenticated evidence of an already performed frozen host action."""
    task_id: str
    run_id: str
    spec_sha256: str
    generation: tuple[float | None, int]
    source_ref: str


def _binding(hooks, task, spec):
    current = hooks.binding(task, spec)
    _require(current["task_id"] == task["task_id"] and type(current["revision"]) is int
             and current["revision"] >= task["binding"]["revision"]
             and type(current["review_revision"]) is int and current["review_revision"] > 0,
             "BINDING_CHANGED")
    if spec["reviewer"] is None:
        _require(current["reviewer"] is None, "BINDING_CHANGED")
    elif current["reviewer"] is not None:
        agent = HubAgentSpec(**current["reviewer"])
        _require(agent.kind in spec["reviewer"]["allowed_kinds"], "BINDING_CHANGED")
    else:
        raise TaskError("BINDING_CHANGED")
    return current


def _barrier(hooks, task, binding, operation):
    code = hooks.decision_barrier(task, binding, operation)
    if code is not None:
        _require(code in {"RUN_UNSETTLED", "DELIVERY_UNKNOWN", "NOT_RESUMABLE", "STALE_REQUEST",
                          "FORBIDDEN", "BINDING_CHANGED", "AUTH_REQUIRED"})
        raise TaskError(code, task["task_id"])


def _host_handoff(hooks, task, binding, run, generation):
    proof = hooks.host_handoff(task, binding, run, generation)
    _require(type(proof) is HostHandoffProof and proof.task_id == task["task_id"]
             and proof.run_id == run["run_id"] and proof.spec_sha256 == task["spec_sha256"]
             and list(proof.generation) == generation and isinstance(proof.source_ref, str)
             and 0 < len(proof.source_ref) <= 256 and proof.source_ref.isascii()
             and all(c.isalnum() or c in "_-.:" for c in proof.source_ref), "NOT_RESUMABLE")
    return proof.source_ref


def _raw_stop(fd, task):
    if _existing(fd, "STOP.json") is None:
        # Compatibility with T2's unstarted ledger. Any missing committed
        # journal entry must fail closed.
        _require(task["stop_epoch"] == 0)
        return None
    record = _read_json(fd, "STOP.json")
    _require(record["schema_version"] == 1 and record["task_id"] == task["task_id"]
             and type(record["epoch"]) is int
             and record["epoch"] in (task["stop_epoch"], task["stop_epoch"] + 1)
             and record["epoch"] > 0 and _valid_id(record["run_id"], "run"))
    _require(record["run_id"] in task["run_hashes"])
    return record


def _stop(store, task):
    with store._private(store._task_dir(task["task_id"])) as fd:
        record = _raw_stop(fd, task)
    return record if record and record["epoch"] > task.get("stop_retired_epoch", 0) else None


def _open_kernel_stop(task):
    link = task["workflow"]
    return _directory(Path(link["directory"]), private_from=Path(task["worktree"]["path"]) / ".codex/tasks")


def _mirror_stop(task, record):
    with _open_kernel_stop(task) as fd:
        _require(_identity(os.fstat(fd)) == task["workflow"]["directory_identity"], "BINDING_CHANGED")
        _save(fd, "STOP", record)


def _gate_name(task):
    return _sha(task["repo"]["key"].encode()) + ".lock"


@contextmanager
def _repo_gate(store, task):
    with store._private(store.root / "repo-gates", create=True) as fd:
        name = _gate_name(task)
        # Serialize creation only. The persistent gate inode is never rotated.
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            if _existing(fd, name) is None:
                _publish(fd, name, b"", exclusive=True)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        with _locked_file(fd, name) as handle:
            # The same open-file description is inherited by run-child.
            # Closing this descriptor does not release the child's lock.
            yield handle


@contextmanager
def _worker_lock(task):
    parent = Path(task["worktree"]["path"]) / ".codex/tasks"
    with _directory(parent, private_from=parent) as fd:
        try:
            handle = os.open(".worker.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC |
                             os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
        except OSError:
            raise TaskError("STATE_CORRUPT") from None
        with os.fdopen(handle, "rb") as lock:
            _regular(os.fstat(lock.fileno()), True, "STATE_CORRUPT")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TaskError("BUSY") from None
            yield


def _evidence(store, task_id, receipt):
    return _strict_json(store.read_evidence(task_id, receipt["evidence_sha256"]), "STATE_CORRUPT")


def _current(store, task):
    history = store._history(task)
    _require(bool(history), "NOT_RESUMABLE")
    return history[-1]


def _uncertain(phase, last):
    if phase in {"launching", "active", "unknown"}:
        return "RUN_UNSETTLED"
    if phase == "settled" and last["outcome"] == "cleanup_blocked":
        return "CLEANUP_BLOCKED"
    return None


def _check_repo_peers(store, task):
    """A released flock cannot erase outstanding work on sibling worktrees."""
    with store._private(store.tasks) as fd:
        references = [name for name in os.listdir(fd) if name != task["task_id"] and _valid_id(name, "task")]
    for task_id in references:
        with store._private(store._task_dir(task_id)) as fd:
            scope = store._scope(fd, task_id)
        if scope.repo_key != task["repo"]["key"]:
            continue
        peer = store.load_task(task_id)
        if not peer["run_id"]:
            continue
        _, receipts = _current(store, peer)
        if receipts and (code := _uncertain(receipts[-1][1]["phase"], receipts[-1][1])):
            raise TaskError(code, task["task_id"])


def _runtime_identity():
    interpreter = Path(sys.executable).absolute()
    paths = (_ENTRY, Path(__file__).resolve(), Path(ato_process.__file__).resolve(),
             Path(ato_product.__file__).resolve(), Path(sys.modules[HostProfile.__module__].__file__).resolve())
    return dict(python=str(interpreter), entry=str(_ENTRY),
                source_sha256=_sha(canonical_json([_sha(path.read_bytes()) for path in paths])))


def _roots(store):
    return dict(home=str(store.home), native_home=str(store.native_home),
                native_homes=[str(p) for p in store.native_homes[1:]])


def _publish_receipt(store, run, phase, generation, evidence, outcome=None, *, validate=None):
    task_id = run["task_id"]
    evidence_ref = store.put_evidence(task_id, canonical_json(evidence))
    for _ in range(30):
        task = store.load_task(task_id)
        if validate is not None:
            validate(task)
        receipt = RunReceipt(task_id, run["run_id"], run["spec_sha256"], run["runtime_seal"],
                             tuple(generation), phase, outcome, evidence_ref, time.time())
        try:
            return store.record_run_receipt(task_id, run["run_id"], receipt, task["revision"])
        except TaskError as exc:
            if exc.code != "BUSY":
                raise
            time.sleep(.01)
    raise TaskError("BUSY", task_id)


def _activity_path(store, run):
    return store._task_dir(run["task_id"]) / "runs" / run["run_id"]


def _active_proof(store, run, receipts):
    for _, receipt in receipts:
        if receipt["phase"] != "active":
            continue
        data = _evidence(store, run["task_id"], receipt)
        _require(data["task_id"] == run["task_id"] and data["run_id"] == run["run_id"]
                 and data["runtime_seal"] == run["runtime_seal"]
                 and data["generation"] == receipt["generation"]
                 and type(data["pid"]) is int and data["pid"] > 0
                 and len(data["birth"]) == 64 and type(data["lease_fence"]) is int
                 and data["lease_fence"] > 0)
        return data
    return None


def _verify_exit(owner):
    _require(ato_process.disposition(owner["pid"], owner["birth"]) in {"absent", "reused"}
             and ato_process.group_members(owner["pid"]) == {}, "CLEANUP_BLOCKED")


def _verify_released(state, run, generation, fence):
    lease = transport.managed_bus().execution_lease(recovery.execution_resource(state["repo"]))
    _require(lease and lease["state"] == "released" and lease["fence"] == fence
             and lease["task_id"] == run["workflow_dir"] and lease["contract_hash"] == run["contract_hash"]
             and lease["generation"] == canonical_json(generation).decode(),
             "RUN_UNSETTLED")


class ExecutionService:
    """Public Task execution API. The actor verifier is separate from visibility.

    authorize(principal, scope, binding, operation) must return literal True
    for the current scoped action. All paths share one fixed private driver.
    """

    def __init__(self, store, profile=None, *, authorize=None, hooks=None, startup_seconds=_STARTUP_SECONDS):
        self.store = store
        self.profile = profile
        self.authorize = authorize
        self.hooks = hooks or ExecutionHooks()
        self.startup_seconds = startup_seconds
        store.execution_projection = self._project

    def task_status(self, actor, task_id):
        return self.store.task_status(actor, task_id)

    def list_tasks(self, actor, repo=None):
        return self.store.list_tasks(actor, repo)

    def task_snapshot(self, task_id):
        for _ in range(3):
            snapshot = self.store.task_snapshot(task_id)
            with self.store._private(self.store._task_dir(task_id)) as fd:
                task = _read_json(fd, "task.json")
                if task["revision"] != snapshot["revision"]:
                    continue
                journal = _raw_stop(fd, task)
                if journal:
                    snapshot["stop_epoch"] = journal["epoch"]
                return snapshot
        raise TaskError("BUSY", task_id)

    def _actor(self, actor, task_id, operation):
        _identifier(task_id, "task")
        principal = self.store._principal(actor)
        scope = self.store._visible_scope(task_id, principal)
        _require(scope is not None, "NOT_FOUND")
        task, spec = self.store.load_task(task_id), self.store.load_spec(task_id)
        binding = _binding(self.hooks, task, spec)
        _require(self.authorize is not None, "AUTH_REQUIRED")
        _require(self.authorize(principal, scope, binding, operation) is True, "FORBIDDEN")
        return task, spec, binding

    def start(self, actor, task_id, foreground=False):
        return self._launch(actor, task_id, "initial", foreground=foreground)

    def resume(self, actor, task_id, *, foreground=False):
        return self._launch(actor, task_id, "resume", foreground=foreground)

    def recheck(self, actor, task_id, checks_only=False, *, foreground=False):
        return self._launch(actor, task_id, "recheck", foreground=foreground, checks_only=checks_only)

    @_state_errors
    def reconcile(self, actor, task_id):
        """Publish only a prepared terminal receipt for the exact current Run."""
        task, _, _ = self._actor(actor, task_id, "reconcile")
        _require(task["creation"]["phase"] == "ready" and not task["worktree_removed"], "NOT_RESUMABLE")
        with _repo_gate(self.store, task), _worker_lock(task):
            task, _, _ = self._actor(actor, task_id, "reconcile")
            _require(task["creation"]["runtime_seal"] == self.store.runtime_seal, "RUNTIME_MISMATCH")
            run, receipts = _current(self.store, task)
            _require(bool(receipts), "RUN_UNSETTLED")
            last = receipts[-1][1]
            if last["phase"] not in _TERMINAL:
                owner = _active_proof(self.store, run, receipts)
                _require(owner is not None and owner["runtime"] == _runtime_identity(), "RUN_UNSETTLED")
                _verify_exit(owner)
                state, contract, generation, _ = self.store._kernel_facts(task)
                _, child_active, _ = self.store._stage_facts(task, contract, state)
                _require(not child_active and not state.get("owner_pid") and not state.get("active_process")
                         and state["status"] != "cleanup_blocked", "CLEANUP_BLOCKED")
                try:
                    with self.store._private(_activity_path(self.store, run)) as fd:
                        prepared = _read_json(fd, "settlement.json")
                except FileNotFoundError:
                    raise TaskError("RUN_UNSETTLED", task_id) from None
                _require(prepared["run_id"] == run["run_id"] and prepared["runtime_seal"] == run["runtime_seal"]
                         and prepared["nonce_sha256"] == owner["nonce_sha256"]
                         and prepared["generation"] == generation
                         and prepared["data"]["owner_birth"] == owner["birth"]
                         and prepared["data"]["owner_pid"] == owner["pid"]
                         and prepared["data"]["cleanup_verified"] is True
                         and prepared["outcome"] not in {"cleanup_blocked", "interrupted", "launch_failed"},
                         "RUN_UNSETTLED")
                with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
                    _verify_released(state, run, generation, prepared["data"]["kernel_fence"])
                _publish_receipt(self.store, run, "settled", generation, prepared["data"], prepared["outcome"])
        return self._status_after_publication(actor, task_id)

    def _status_after_publication(self, actor, task_id):
        for _ in range(15):
            try:
                return self.store.task_status(actor, task_id)
            except TaskError as exc:
                if exc.code != "BUSY":
                    raise
            time.sleep(.02)
        raise TaskError("BUSY", task_id)

    @contextmanager
    def transaction(self, task_id, expected_revision, expected_stop_epoch, expected_binding_revision,
                    *, reviewer_change=False):
        """Short CAS for T4/T5. Yield the verified Task directory fd and snapshot.

        Read and verify slow evidence before entry. Leaf code publishes its
        committed record using the fd, and this context increments Task revision.
        """
        with self.store._mutation(task_id, expected_revision) as (fd, task, spec):
            journal = _raw_stop(fd, task)
            epoch = journal["epoch"] if journal else task["stop_epoch"]
            binding = _binding(self.hooks, task, spec)
            _require(epoch == expected_stop_epoch, "STALE_REQUEST")
            _require(binding["revision"] == expected_binding_revision, "BINDING_CHANGED")
            if reviewer_change:
                self._reviewer_change_allowed(task, spec, journal)
            yield fd, task, binding
            self.store._advance(fd, task)

    def _reviewer_change_allowed(self, task, spec, journal):
        _require(spec["reviewer"] is not None and not task["worktree_removed"] and not (
            journal and journal["epoch"] > task.get("stop_retired_epoch", 0)), "NOT_RESUMABLE")
        if not task["run_id"]:
            return
        run, receipts = _current(self.store, task)
        if not receipts:
            return
        last = receipts[-1][1]
        _require(last["phase"] not in {"launching", "unknown"}, "RUN_UNSETTLED")
        if last["phase"] == "active":
            proof = _active_proof(self.store, run, receipts)
            _require(proof and ato_process.disposition(proof["pid"], proof["birth"]) == "live",
                     "RUN_UNSETTLED")
            with self.store._private(_activity_path(self.store, run)) as fd:
                _require(_read_json(fd, "activity.json")["status"] != "reviewing", "BUSY")
        else:
            data = _evidence(self.store, task["task_id"], last)
            _require(last["outcome"] != "cleanup_blocked", "CLEANUP_BLOCKED")
            _require(data.get("reason") not in {"REVIEWER_MUTATION_VIOLATION", "review_loop_exhausted",
                                               "repeated_blockers"}, "NOT_RESUMABLE")
            _require(data.get("review_rounds", 0) < spec["reviewer"]["max_review_rounds"], "NOT_RESUMABLE")
            if last["outcome"] == "needs_acceptance" and data.get("product"):
                accepted = self.hooks.acceptance(task, data["product"])
                _require(not accepted or accepted["disposition"] != "recorded", "NOT_RESUMABLE")

    @contextmanager
    def product_transaction(self, task_id):
        """T4 holds repo admission across product verification and its short CAS."""
        task = self.store.load_task(task_id)
        with _repo_gate(self.store, task), _worker_lock(task):
            snapshot = self.task_snapshot(task_id)
            _require(snapshot["product"] is not None and snapshot["execution"]["verified"]
                     and snapshot["execution"]["state"] == "idle"
                     and not snapshot["execution"]["stop_requested"], "CHECKS_REQUIRED")
            yield snapshot

    def _project(self, task, spec, history, generation, status):
        if not history:
            return
        journal = _stop(self.store, task)
        execution = status["execution"]
        execution["stop_requested"] |= journal is not None
        run, receipts = history[-1]
        last = receipts[-1][1] if receipts else None
        phase = last["phase"] if last else "reserved"
        data = _evidence(self.store, task["task_id"], last) if last else {}
        binding = _binding(self.hooks, task, spec)
        status["pending"] = list(self.hooks.pending(task, binding))
        if status["status"] == "cleanup_blocked":
            return
        if phase in {"launching", "active", "unknown"}:
            proof = _active_proof(self.store, run, receipts)
            if proof:
                observed = ato_process.disposition(proof["pid"], proof["birth"])
                if observed == "live":
                    state = "running"
                    folder = _activity_path(self.store, run)
                    with self.store._private(folder) as fd:
                        if _existing(fd, "activity.json") is not None:
                            activity = _read_json(fd, "activity.json")
                            _require(activity["run_id"] == run["run_id"]
                                     and activity["status"] in {"running", "checking", "reviewing"})
                            state = activity["status"]
                    execution.update(state="active", pid=proof["pid"], owner_role="worker",
                                     verified=True, lease="admitted", activity=state)
                    status.update(status=state, reason=None, next_step="WAIT_FOR_EXECUTION")
                    if execution["stop_requested"]:
                        execution["state"] = "stopping"
                        status.update(status="stopping", reason="STOPPED", next_step="VERIFY_CLEANUP")
                    return
            elif phase == "launching" and ato_process.disposition(data.get("parent_pid"), data.get("parent_birth")) == "live":
                if not execution["stop_requested"]:
                    status.update(status="launching", reason=None, next_step="VERIFY_STARTUP")
                    execution["state"] = "launching"
                return
            status.update(status="delivery_unknown", reason="RUN_UNSETTLED", next_step="RECONCILE_RUN")
            execution.update(state="unknown", verified=False)
            return
        if (journal and data.get("reason") != "REVIEWER_MUTATION_VIOLATION"
                and (phase == "reserved" or (phase in _TERMINAL and data.get("cleanup_verified") is True))):
            status.update(status="stopped", reason="STOPPED", next_step="REVIEW_STOPPED_TASK")
            execution.update(state="idle", verified=True)
            return
        if phase == "settled" and data.get("reason") is not None:
            status["reason"] = data["reason"]
        decision = self.hooks.decision_status(task, binding) if not execution["stop_requested"] else None
        if decision:
            _require(decision in {"waiting", "needs_input", "delivery_unknown"})
            steps = dict(waiting="REVIEW_PENDING_ACTION", needs_input="AWAIT_USER_INPUT",
                         delivery_unknown="RECONCILE_DECISION")
            status.update(status=decision, reason="DELIVERY_UNKNOWN" if decision == "delivery_unknown" else None,
                          next_step=steps[decision])
            return
        if phase != "settled" or last["outcome"] != "needs_acceptance" or execution["stop_requested"]:
            if phase == "settled" and data.get("checks"):
                status["checks"] = data["checks"]
            if phase == "settled" and data.get("review"):
                status["review"] = data["review"]
            return
        try:
            _require(task["worktree_removed"] or generation == last["generation"], "CHECKS_REQUIRED")
            status["product"], status["checks"], status["review"] = ato_product.read_delivery(
                self.store, task, run, last["generation"], spec, data, binding["review_revision"])
        except TaskError as exc:
            if exc.code not in {"PRODUCT_CHANGED", "CHECKS_REQUIRED"}:
                raise
            status.update(status="blocked", reason=exc.code, next_step="RECHECK_PRODUCT")
            status["acceptance"]["disposition"] = "invalidated"
            return
        status.update(status="needs_acceptance", reason=None, next_step="AWAIT_USER_ACCEPTANCE")
        acceptance = self.hooks.acceptance(task, status["product"])
        if acceptance:
            status["acceptance"] = dict(acceptance)
            if acceptance["disposition"] == "recorded":
                status.update(status="completed", next_step=None)

    @_state_errors
    def request_stop(self, actor, task_id):
        for _ in range(30):
            task, spec, binding = self._actor(actor, task_id, "stop")
            _require(task["creation"]["phase"] == "ready", "NOT_RESUMABLE")
            _, receipts = _current(self.store, task)
            if receipts and receipts[-1][1]["outcome"] == "needs_acceptance":
                product = _evidence(self.store, task_id, receipts[-1][1]).get("product")
                if product and self.hooks.acceptance(task, product):
                    status = self.store.task_status(actor, task_id)
                    if status["status"] == "completed":
                        return status
            try:
                with self.store._mutation(task_id, task["revision"]) as (fd, latest, _):
                    _require(_binding(self.hooks, latest, spec) == binding, "BINDING_CHANGED")
                    current = _raw_stop(fd, latest)
                    if current and current["run_id"] == latest["run_id"] and current["epoch"] > latest.get("stop_retired_epoch", 0):
                        record = current
                    else:
                        record = dict(schema_version=1, task_id=task_id, run_id=latest["run_id"],
                                      epoch=(current["epoch"] if current else latest["stop_epoch"]) + 1,
                                      recorded_at=time.time())
                        if current:
                            _save(fd, "STOP-" + str(current["epoch"]) + ".json", current)
                        _save(fd, "STOP.json", record)
                    if record["epoch"] != latest["stop_epoch"]:
                        latest["stop_epoch"] = record["epoch"]
                        self.store._advance(fd, latest)
                    # No Git, mailbox or execution lock in the STOP path.
                    _mirror_stop(latest, record)
                return self.store.task_status(actor, task_id)
            except TaskError as exc:
                if exc.code != "BUSY":
                    raise
                time.sleep(.01)
        raise TaskError("BUSY", task_id)

    def _prior_admission(self, task, spec, binding, kind, state, kernel_stop, history):
        run, receipts = history[-1]
        last = receipts[-1][1] if receipts else None
        phase = last["phase"] if last else "reserved"
        if code := _uncertain(phase, last):
            raise TaskError(code, task["task_id"])
        stop = _stop(self.store, task)
        _require(not kernel_stop or stop is not None, "STOPPED")
        _barrier(self.hooks, task, binding, kind)
        _require(not state.get("owner_pid") and not state.get("active_process"), "CLEANUP_BLOCKED")
        for stage in state["stages"]:
            child = ato_core.read(Path(stage["run_dir"]) / "state.json")
            _require(not child.get("owner_pid") and not child.get("active_process"), "CLEANUP_BLOCKED")
        _require(not (Path(run["workflow_dir"]) / "user-closure.json").exists(), "NOT_RESUMABLE")
        if kind == "initial":
            _require(run["kind"] == "initial" and phase == "reserved"
                     and state["status"] == "created" and not state["stages"], "NOT_RESUMABLE")
            _require(stop is None, "STOPPED")
            return None, None
        if phase == "reserved":
            _require(kind == "resume" and stop is not None and state["status"] == "created"
                     and not state["stages"], "NOT_RESUMABLE")
            return stop, None
        proof = _evidence(self.store, task["task_id"], last)
        _require(proof.get("cleanup_verified") is True, "CLEANUP_BLOCKED")
        owner = _active_proof(self.store, run, receipts)
        if owner:
            _verify_exit(owner)
        _require(proof.get("reason") not in {"REVIEWER_MUTATION_VIOLATION", "review_loop_exhausted",
                                            "repeated_blockers", "PRODUCT_CHANGED_DURING_CHECKS"},
                 "NOT_RESUMABLE")
        handoff = None
        if kind == "resume":
            _require(last["outcome"] in _RETRYABLE and proof.get("reason") != "TIME_LIMIT"
                     and state["status"] not in {
                "limit", "stage_limit", "planning_limit", "permission_blocked", "contract_changed"},
                "NOT_RESUMABLE")
            if state.get("active_run"):
                child_dir = Path(state["active_run"])
                child = ato_core.read(child_dir / "state.json")
                limits = spec["worker_contract"]["limits"]
                _require(all(row["status"] == "completed" or
                             row.get("attempt_sequence", row["attempts"]) < limits["max_attempts"]
                             for row in child["tasks"].values()), "NOT_RESUMABLE")
        else:
            _require(stop is None and phase == "settled" and bool(state["stages"])
                     and (not state.get("active_run") or last["outcome"] == "needs_host"),
                     "NOT_RESUMABLE")
            if last["outcome"] == "needs_host":
                _require(bool(spec["worker_contract"].get("host_handoff"))
                         and state["status"] == "needs_host", "NOT_RESUMABLE")
                handoff = _host_handoff(self.hooks, task, binding, run, last["generation"])
            if last["outcome"] == "needs_acceptance":
                try:
                    product, _, _ = ato_product.read_delivery(
                        self.store, task, run, last["generation"], spec, proof, binding["review_revision"])
                except TaskError as exc:
                    if exc.code not in {"PRODUCT_CHANGED", "CHECKS_REQUIRED"}:
                        raise
                else:
                    current = self.hooks.acceptance(task, product) or {}
                    _require(current.get("disposition") != "recorded", "NOT_RESUMABLE")
        return stop, handoff

    @_state_errors
    def _launch(self, actor, task_id, kind, *, foreground, checks_only=False):
        task, _, _ = self._actor(actor, task_id, kind)
        _require(type(foreground) is bool and type(checks_only) is bool, "USAGE")
        _require(task["creation"]["phase"] == "ready" and not task["worktree_removed"], "NOT_RESUMABLE")
        _require(self.profile is not None, "UNSUPPORTED_CAPABILITY")
        with _repo_gate(self.store, task) as gate:
            task, spec, binding = self._actor(actor, task_id, kind)
            _require(self.store.runtime_seal == task["creation"]["runtime_seal"], "RUNTIME_MISMATCH")
            _check_repo_peers(self.store, task)
            with _worker_lock(task), ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
                state, _, generation, kernel_stop = self.store._kernel_facts(task)
                self.store._stage_facts(task, spec["worker_contract"], state)
                history = self.store._history(task)
                current_run, current_receipts = history[-1]
                reserved = not current_receipts and current_run["kind"] != "initial"
                if reserved:
                    _require(current_run["kind"] == kind and current_run["generation"] == generation,
                             "RUN_UNSETTLED")
                preceding = history[:-1] if reserved else history
                previous, receipts = preceding[-1]
                known = receipts[-1][1] if receipts else previous
                # Any uncorrelated change to the Workflow generation fences
                # execution, even once the previous driver process has gone.
                if known.get("phase") in _TERMINAL or not receipts:
                    _require(known["generation"] == generation, "RUN_UNSETTLED")
                stop, handoff = self._prior_admission(task, spec, binding, kind, state, kernel_stop, preceding)
                self.profile.verify(self.store, task, spec, work=kind != "recheck")
                if spec["reviewer"] and not checks_only:
                    _require(spec["reviewer"]["session_mode"] != "require_resume", "UNSUPPORTED_CAPABILITY")
                    self.profile.reviewer(binding["reviewer"]["kind"])
                policy = budget.time_policy()
                _require(not budget.summary(task["workflow"]["directory"], state)["exhausted"], "NOT_RESUMABLE")
                for prior, receipts in history:
                    for _, receipt in receipts:
                        if receipt["phase"] == "launching":
                            _require(_evidence(self.store, task_id, receipt)["time_policy"] == policy, "NOT_RESUMABLE")
                bus = transport.managed_bus()
                lease = bus.execution_lease(recovery.execution_resource(state["repo"]))
                if stop:
                    # A clean STOP normally releases its lease. A retained
                    # native or interrupted lease cannot be skipped at resume.
                    _require(not lease or lease["state"] != "active", "RUN_UNSETTLED")
                else:
                    try:
                        recovery.execution_available(task["workflow"]["directory"], state,
                                                     bus, explicit_run=True)
                        _require(not lease or lease["state"] != "active"
                                 or lease["lease_until"] <= time.time(), "BUSY")
                    except ValueError as exc:
                        if isinstance(exc, TaskError):
                            raise
                        raise TaskError("RUN_UNSETTLED", task_id) from None
            # Intent creation is fenced against an intervening STOP/handoff.
            if kind != "initial" and not reserved:
                if not history[-1][1]:
                    task = _publish_receipt(self.store, history[-1][0], "launch_failed", generation,
                                            {"cleanup_verified": True, "reason": "STOPPED"}, "launch_failed",
                                            validate=lambda latest: _require(latest["revision"] == task["revision"], "BUSY"))
                intent = RunIntent(new_id("run"), task["run_id"], kind, task["run_count"] + 1,
                                   "explicit_" + kind, self.store.runtime_seal, tuple(generation))
                task = self.store.append_run(task_id, intent, task["revision"])
            run, receipts = _current(self.store, task)
            _require(not receipts, "RUN_UNSETTLED")
            nonce = secrets.token_hex(32)
            parent_birth = ato_process.identity(os.getpid())
            _require(parent_birth is not None, "LAUNCH_FAILED")
            launch = dict(parent_pid=os.getpid(), parent_birth=parent_birth, nonce_sha256=_sha(nonce.encode()),
                          runtime=_runtime_identity(), time_policy=policy, profile=self.profile.document(),
                          binding_revision=binding["revision"], checks_only=checks_only,
                          host_handoff=handoff, roots=_roots(self.store),
                          stop_epoch=stop["epoch"] if stop else task["stop_epoch"],
                          stop_run_id=stop["run_id"] if stop else None)
            def still_current(latest):
                _require(latest["revision"] == task["revision"], "BUSY")
            _publish_receipt(self.store, run, "launching", generation, launch, validate=still_current)
            process = self._spawn(run, gate, nonce)
        # Gate ownership crossed exec on the same open-file description.
        try:
            self._await_start(run, process, launch)
            if foreground:
                process.wait()
            return self._status_after_publication(actor, task_id)
        except BaseException:
            process.poll()
            raise

    def _spawn(self, run, gate, nonce):
        try:
            return self._spawn_child(run, gate, nonce)
        except OSError:
            _publish_receipt(self.store, run, "launch_failed", run["generation"],
                             {"reason": "LAUNCH_FAILED", "cleanup_verified": True}, "launch_failed")
            raise TaskError("LAUNCH_FAILED", run["task_id"]) from None

    def _spawn_child(self, run, gate, nonce):
        runtime = _runtime_identity()
        capability = canonical_json(dict(task_id=run["task_id"], run_id=run["run_id"], nonce=nonce, gate=gate,
                                         seal=run["runtime_seal"]))
        read_end, write_end = os.pipe()
        try:
            # The payload fits a private pipe without relying on a running child.
            _require(len(capability) <= os.fpathconf(write_end, "PC_PIPE_BUF"), "LAUNCH_FAILED")
            os.write(write_end, capability)
            os.close(write_end)
            write_end = None
            command = [runtime["python"], "-IB", runtime["entry"], "run-child", run["task_id"], run["run_id"]]
            return subprocess.Popen(command, cwd=_ENTRY.parent, env={
                "PATH": os.defpath, "HOME": self.profile.worker_home, "LC_ALL": "C",
                "ATO_PRIVATE_PIPE_FD": str(read_end), "ATO_PRIVATE_ROOTS": canonical_json(_roots(self.store)).decode()},
                pass_fds=(read_end, gate), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, umask=0o077)
        finally:
            os.close(read_end)
            if write_end is not None:
                os.close(write_end)

    def _await_start(self, run, process, launch):
        deadline = time.monotonic() + self.startup_seconds
        birth = ato_process.identity(process.pid)
        while True:
            task = self.store.load_task(run["task_id"])
            _, receipts = self.store._run(task, run["run_id"])
            if receipts:
                last = receipts[-1][1]
                if last["phase"] == "launch_failed":
                    data = _evidence(self.store, run["task_id"], last)
                    if data["reason"] == "STOPPED":
                        return
                    raise TaskError("LAUNCH_FAILED", run["task_id"])
                proof = _active_proof(self.store, run, receipts)
                if proof:
                    _require(proof["pid"] == process.pid and proof["runtime"] == launch["runtime"]
                             and proof["nonce_sha256"] == launch["nonce_sha256"]
                             and (birth is None or proof["birth"] == birth), "RUN_UNSETTLED")
                    if last["phase"] in _TERMINAL | {"unknown"}:
                        return
                    if ato_process.disposition(proof["pid"], proof["birth"]) == "live":
                        return
            if process.poll() is not None or time.monotonic() >= deadline:
                self._mark_unknown_start(run, process.pid, birth)
                raise TaskError("RUN_UNSETTLED", run["task_id"])
            time.sleep(.025)

    def _mark_unknown_start(self, run, pid, birth):
        def still_launching(task):
            _, receipts = _current(self.store, task)
            _require(task["run_id"] == run["run_id"] and receipts[-1][1]["phase"] == "launching",
                     "RUN_UNSETTLED")
        try:
            _publish_receipt(self.store, run, "unknown", run["generation"], dict(
                reason="RUN_UNSETTLED", cleanup_verified=False, owner_pid=pid, owner_birth=birth),
                validate=still_launching)
        except TaskError as exc:
            if exc.code != "RUN_UNSETTLED":
                raise


class _RunDriver:
    """Owns one admitted Run; only the subprocess writes active/terminal evidence."""

    def __init__(self, store, run, launch, hooks, folder):
        self.store, self.run, self.launch, self.hooks = store, run, launch, hooks
        self.task_id = run["task_id"]
        self.task, self.spec = store.load_task(self.task_id), store.load_spec(self.task_id)
        self.folder = folder
        self.profile = HostProfile.from_document(launch["profile"])
        self.host = CommandHost(self, self.profile, folder)
        self.pid = os.getpid()
        self.birth = ato_process.identity(self.pid)
        self.admitted = False
        self.signal_stop = False
        self.review_rounds = 0
        self.fingerprints = []
        self.reason = None
        self.product = None
        self.checks = []
        self.review = None
        self.retained_review = None
        self.lease = None
        self.lease_renewed = 0
        self.kernel_fence = None
        self.kernel_generation = run["generation"]
        self.review_deadline = float("inf")
        self.previous_run = None
        self.previous_receipt = None
        if run["predecessor_run_id"]:
            self.previous_run, old_receipts = store._run(self.task, run["predecessor_run_id"])
            self.previous_receipt = old_receipts[-1][1]
            previous = _evidence(store, self.task_id, self.previous_receipt)
            self.review_rounds = previous.get("review_rounds", 0)
            self.fingerprints = previous.get("fingerprints", [])
            if previous.get("reason") == "REVIEW_CHANGES_REQUIRED":
                self.retained_review = previous

    def _mark(self, state):
        with self.store._private(self.folder) as fd:
            _save(fd, "activity.json", dict(run_id=self.run["run_id"], status=state))

    def stopped(self):
        if self.signal_stop:
            return True
        for _ in range(4):
            with self.store._private(self.store._task_dir(self.task_id)) as fd:
                try:
                    current = _read_json(fd, "task.json")
                    _require(current["run_id"] == self.run["run_id"], "RUN_UNSETTLED")
                    journal = _raw_stop(fd, current)
                    return bool(journal and journal["epoch"] > current.get("stop_retired_epoch", 0))
                except TaskError as exc:
                    if exc.code != "BUSY":
                        raise
            time.sleep(.005)
        raise TaskError("BUSY")

    def guard(self):
        if self.stopped():
            raise ato_core.Halt("stopped")
        with _open_kernel_stop(self.task) as fd:
            _require(_identity(os.fstat(fd)) == self.task["workflow"]["directory_identity"], "BINDING_CHANGED")
            if _existing(fd, "STOP") is not None:
                raise ato_core.Halt("stopped")
        if self.lease is not None and time.monotonic() - self.lease_renewed >= 3:
            self.lease.renew()
            self.lease_renewed = time.monotonic()
        if time.time() >= self.review_deadline:
            raise ato_core.Halt("limit")

    def _handle_stop(self, *_):
        self.signal_stop = True

    def _validate_context(self):
        _require(self.birth is not None, "LAUNCH_FAILED")
        _require(_runtime_identity() == self.launch["runtime"] and self.store.runtime_seal == self.run["runtime_seal"]
                 and self.launch["roots"] == _roots(self.store),
                 "RUNTIME_MISMATCH")
        current = self.store.load_task(self.task_id)
        _require(current["run_id"] == self.run["run_id"], "RUN_UNSETTLED")
        binding = _binding(self.hooks, current, self.spec)
        _require(binding["revision"] == self.launch["binding_revision"], "BINDING_CHANGED")
        _barrier(self.hooks, current, binding, self.run["kind"])
        if self.launch["host_handoff"] is not None:
            _require(_host_handoff(self.hooks, current, binding, self.previous_run,
                                   self.previous_receipt["generation"]) == self.launch["host_handoff"],
                     "BINDING_CHANGED")
        _require(budget.time_policy() == self.launch["time_policy"], "NOT_RESUMABLE")
        self.profile.verify(self.store, current, self.spec, work=self.run["kind"] != "recheck")

    def _prepare_stop(self, current):
        if not self.launch["stop_run_id"]:
            self.guard()
            return
        expected = self.launch["stop_epoch"]
        with self.store._mutation(self.task_id, current["revision"]) as (fd, latest, _):
            journal = _raw_stop(fd, latest)
            if (not journal or journal["epoch"] != expected
                    or latest["stop_epoch"] != expected
                    or latest.get("stop_retired_epoch", 0) >= expected
                    or latest["run_id"] != self.run["run_id"]
                    or journal["run_id"] != self.launch["stop_run_id"]):
                raise ato_core.Halt("stopped")
            # Keep STOP effective if the process dies after mirror retirement
            # and before this Task revision becomes durable.
            with _open_kernel_stop(latest) as kernel_fd:
                _require(_identity(os.fstat(kernel_fd)) == latest["workflow"]["directory_identity"],
                         "BINDING_CHANGED")
                if _existing(kernel_fd, "STOP") is not None:
                    _require(_read_json(kernel_fd, "STOP") == journal, "STOPPED")
                    os.unlink("STOP", dir_fd=kernel_fd)
                    os.fsync(kernel_fd)
            latest["stop_retired_epoch"] = expected
            self.store._advance(fd, latest)

    @contextmanager
    def _owned_host(self, *, prepare=False):
        with _worker_lock(self.task):
            current = self.store.load_task(self.task_id)
            state, _, generation, _ = self.store._kernel_facts(current)
            if prepare:
                self._prepare_stop(current)
            _require(generation == self.kernel_generation, "RUN_UNSETTLED")
            if self.kernel_fence is not None:
                _verify_released(state, self.run, generation, self.kernel_fence)
            self.guard()
            with recovery.execution_owner(self.run["workflow_dir"], state, explicit_run=True) as lease:
                self.lease = lease
                self.lease_renewed = time.monotonic()
                try:
                    yield lease, state, generation
                finally:
                    self.kernel_fence = lease.fence
                    self.lease = None

    def _startup(self):
        self._validate_context()
        self._mark("running")
        with self._owned_host(prepare=True) as (lease, _, generation):
            self.guard()
            proof = dict(task_id=self.task_id, run_id=self.run["run_id"], runtime_seal=self.run["runtime_seal"],
                         pid=self.pid, birth=self.birth, lease_fence=lease.fence, generation=generation,
                         nonce_sha256=self.launch["nonce_sha256"], runtime=self.launch["runtime"])
            _publish_receipt(self.store, self.run, "active", generation, proof)
            self.admitted = True
            if self.run["kind"] == "resume" or self.launch["host_handoff"] is not None:
                self.guard()
                task = self.store.load_task(self.task_id)
                binding = _binding(self.hooks, task, self.spec)
                ref = self.hooks.claim_continuation(task, binding, self.run, generation)
                if ref is not None:
                    self.store.read_evidence(self.task_id, ref)
                    with self.store._private(self.folder) as fd:
                        _save(fd, "decision.json", dict(run_id=self.run["run_id"],
                                  generation=generation, receipt_id=ref), exclusive=True)

    def _workflow(self, *, for_work=False):
        driver = self

        class HostedWorkflow(GuardedWorkflow):
            checked_admission = False

            def guard(self):
                if for_work and not self.checked_admission:
                    # Workflow invokes its first guard after taking its own
                    # worktree flock and reloading the kernel state.
                    generation = recovery.generation(self.dir, self.state)
                    _require(generation == driver.kernel_generation, "RUN_UNSETTLED")
                    _verify_released(self.state, driver.run, generation, driver.kernel_fence)
                    self.checked_admission = True
                super().guard()

            def event(self, kind, **data):
                super().event(kind, **data)
                if for_work and kind == "workflow_finished":
                    driver.kernel_generation = recovery.generation(self.dir, self.state)
                    driver.kernel_fence = self._execution_owner.fence

            def check_total(self):
                self.guard()
                before = ato_product.measure(driver.task)
                super().check_total()
                if before != ato_product.measure(driver.task):
                    driver.reason = "PRODUCT_CHANGED_DURING_CHECKS"
                    raise ato_core.Halt("blocked")

        return HostedWorkflow(self.run["workflow_dir"], self.guard)

    def _preseed(self, preset=None):
        with self._owned_host():
            owner = self._workflow()
            owner.guard()
            if preset is None:
                if owner.state["stages"]:
                    return
                preset = owner.contract["stages"][0]
            owner.stage(preset["plan"], preset["covers"], preset["id"])

    def _work(self, *, retry=False, host_handoff=None):
        self._mark("running")
        self.guard()
        owner = self._workflow(for_work=True)
        owner.execute(retry=retry, host_checks=host_handoff is not None, source=host_handoff)
        # Workflow.execute sets its legacy handlers. Outside that call, use
        # our own in-memory signal guard and the Task-level STOP journal.
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        self.guard()
        state = owner.state
        if state["status"] == "cleanup_blocked":
            raise ato_core.Halt("cleanup_blocked")
        if state["status"] in {"error", "interrupted"}:
            self.host.unconfirmed_attempt()
        if state["status"] not in {"needs_input", "completed"}:
            reason = "TIME_LIMIT" if state["status"] in {"limit", "stage_limit", "planning_limit"} else "WORKFLOW_BLOCKED"
            if self.reason is None:
                self.reason = reason
            return state["status"] if state["status"] in {"needs_host", "waiting", "stopped"} else "blocked"
        return None

    def _checks(self, owner, lease):
        self._mark("checking")
        self.guard()
        # Publish the full native generation for this independent check turn.
        # Its state revision and start event follow the existing Workflow fence.
        owner._execution_owner = lease
        owner._accounted_at = ato_core.now()
        owner.deadline = budget.deadline(owner.dir, owner.state, started=owner._accounted_at)
        owner.state.update(status="verifying", owner_pid=self.pid, owner_identity=self.birth,
                           execution_revision=owner.state.get("execution_revision", 0) + 1)
        owner.persist()
        owner.event("workflow_started")
        generation = recovery.generation(owner.dir, owner.state)
        lease.advance(generation)
        self.kernel_generation = generation
        try:
            # Reuse Workflow criteria and original argv under the same lease
            # used for any following technical review.
            owner.check_total()
            manifest = ato_product.measure(self.task)
            self.product, self.checks = ato_product.retain_checks(
                self.store, self.task, self.run, generation, owner.contract, owner.state["criteria"], manifest)
            passed = all(row["state"] == "passed" for row in self.checks)
            owner.state["status"] = "needs_input" if passed else "blocked"
            return passed, generation
        except ato_core.Halt as halt:
            owner.state["status"] = str(halt)
            raise
        finally:
            owner.state["owner_pid"] = None
            owner.persist()
            owner.event("workflow_finished", status=owner.state["status"])

    def _snapshot_for_review(self):
        # The tripwire includes ignored scratch and the whole kernel. An exact
        # mirror written by the trusted STOP path is the only exception.
        record = _stop(self.store, self.store.load_task(self.task_id))
        mirror = Path(self.run["workflow_dir"]) / "STOP"
        skip = None
        if record:
            with _open_kernel_stop(self.task) as fd:
                if _existing(fd, "STOP") is not None and _read_json(fd, "STOP") == record:
                    skip = mirror
        return (hub_worktree.snapshot_integrity(self.run["worktree"], trusted_stop=skip,
                    verified_shared=self.task["worktree"]["shared_link_identities"]),
                ato_product.measure(self.task))

    def _begin_review(self, generation):
        for _ in range(20):
            self.guard()
            task = self.store.load_task(self.task_id)
            binding = _binding(self.hooks, task, self.spec)
            _barrier(self.hooks, task, binding, "review")
            try:
                with self.store._mutation(self.task_id, task["revision"]) as (fd, latest, spec):
                    journal = _raw_stop(fd, latest)
                    if journal and journal["epoch"] > latest.get("stop_retired_epoch", 0):
                        raise ato_core.Halt("stopped")
                    _require(_binding(self.hooks, latest, spec) == binding, "BINDING_CHANGED")
                    with self.store._private(self.folder) as run_fd:
                        _save(run_fd, "review-" + str(self.review_rounds + 1) + ".json",
                              dict(run_id=self.run["run_id"], generation=generation, product=self.product,
                                   review_revision=binding["review_revision"]), exclusive=True)
                    self._mark("reviewing")
                    self.store._advance(fd, latest)
                    self.review_rounds += 1
                    return binding
            except TaskError as exc:
                if exc.code != "BUSY":
                    raise
                time.sleep(.01)
        raise TaskError("BUSY")

    def _review(self, generation):
        policy = self.spec["reviewer"]
        if self.review_rounds >= policy["max_review_rounds"]:
            self.reason = "review_loop_exhausted"
            return None
        binding = self._begin_review(generation)
        agent = HubAgentSpec(**binding["reviewer"])
        adapter = self.profile.reviewer(agent.kind)
        # Fresh one-shot adapters have no independently established idle
        # native session to adopt. Keep the frozen resume requirement explicit.
        _require(policy["session_mode"] != "require_resume", "UNSUPPORTED_CAPABILITY")
        bundle = hub_worktree.diff_bundle(self.task["worktree"],
            verified_shared=self.task["worktree"]["shared_link_identities"])
        prompt = ("Perform a read-only technical review. Return only the structured review verdict. "
                  "Use the frozen scope and independent check evidence. Only blocker findings request repair.\n"
                  + canonical_json(dict(objective=self.spec["worker_contract"]["objective"],
                                        scope=self.spec["worker_contract"]["scope"], checklist=policy["checklist"],
                                        design_docs=self._design_context(policy), checks=self.checks,
                                        product_id=self.product["product_id"], diff=bundle)).decode())
        request = HubTurnRequest(agent, Path(self.run["worktree"]), self.host.runtime(), prompt,
            snapshot=self._snapshot_for_review, stop=self.host.stop, session_mode="fresh_only",
            timeout_seconds=policy["timeout_seconds"],
            provenance="fresh_only" if policy["session_mode"] == "fresh_only" else "ledger_reconstruction")
        if not self.profile.fixture:
            caps = adapter.capabilities()
            _require(caps.supported("read_only_cli") or caps.supported("os_sandbox_compatible"),
                     "UNSUPPORTED_CAPABILITY")
        before = self._snapshot_for_review()
        try:
            result = adapter.run_turn(request)
        finally:
            try:
                unchanged = before == self._snapshot_for_review()
            except (ValueError, OSError):
                unchanged = False
            if not unchanged:
                self.reason = "REVIEWER_MUTATION_VIOLATION"
                raise ato_core.Halt("blocked")
        if self.host.cleanup_blocked or result.status == "cleanup_blocked":
            raise ato_core.Halt("cleanup_blocked")
        self.guard()
        current = _binding(self.hooks, self.store.load_task(self.task_id), self.spec)
        _require(current["review_revision"] == binding["review_revision"]
                 and current["reviewer"] == binding["reviewer"], "BINDING_CHANGED")
        if result.status != "success":
            self.reason = result.code
            if result.code not in {"AUTH_BLOCKED", "EXECUTABLE_UNAVAILABLE", "VERSION_MISMATCH",
                                   "REVIEWER_MUTATION_VIOLATION"}:
                self.host.unknown = True
            return None
        verdict = validate_review_verdict(result.output)
        receipt = dict(task_id=self.task_id, run_id=self.run["run_id"], spec_sha256=self.run["spec_sha256"],
                       generation=generation, product_id=self.product["product_id"],
                       checks_sha256=self.product["checks_sha256"],
                       review_revision=binding["review_revision"], verdict=verdict)
        self.review = dict(verdict=verdict["verdict"], receipt_id=self.store.put_evidence(
            self.task_id, canonical_json(receipt)))
        if verdict["verdict"] == "request_changes":
            # H1's source fingerprint excludes non-product scratch.
            digest = blocker_fingerprint(bundle["snapshot"]["tree_sha"], verdict)
            if digest in self.fingerprints:
                self.reason = "repeated_blockers"
                return None
            self.fingerprints.append(digest)
        return verdict

    def _design_context(self, policy):
        from ato_task_store import _git
        context = {}
        remaining = 64 * 1024
        for row in policy["design_docs"]:
            data = _git(Path(self.task["repo"]["root"]), "show",
                        self.task["worktree"]["base_commit"] + ":" + row["path"])[0]
            _require(_sha(data) == row["sha256"], "SPEC_CHANGED")
            context[row["path"]] = data[:remaining].decode("utf-8", errors="replace")
            remaining -= min(remaining, len(data))
        return context

    def _repair(self, verdict):
        if self.review_rounds >= self.spec["reviewer"]["max_review_rounds"]:
            self.reason = "review_loop_exhausted"
            return "blocked"
        commands = [criterion for criterion in self.spec["worker_contract"]["acceptance"]
                    if criterion["kind"] == "command"]
        blockers = [finding for finding in verdict["findings"] if finding["severity"] == "blocker"]
        preset = dict(id="review-fix-" + str(self.review_rounds), covers=[row["id"] for row in commands],
                      plan=dict(objective="Repair the technical blockers within the frozen scope",
                                tasks=[dict(id="fix_blockers", prompt=canonical_json(blockers).decode(),
                                            checks=[argv for row in commands for argv in row["checks"]])]))
        self._preseed(preset)
        return self._work()

    def _execute(self):
        recheck = self.run["kind"] == "recheck"
        if self.launch["host_handoff"] is not None:
            outcome = self._work(host_handoff=self.launch["host_handoff"])
            if outcome:
                return outcome
        elif not recheck:
            if self.retained_review:
                product = self.retained_review["product"]
                _require("product-" + _sha(canonical_json(ato_product.measure(self.task)))
                         == product["product_id"], "PRODUCT_CHANGED")
                ref = self.retained_review["review"]["receipt_id"]
                verdict = _strict_json(self.store.read_evidence(self.task_id, ref), "STATE_CORRUPT")
                binding = _binding(self.hooks, self.store.load_task(self.task_id), self.spec)
                _require(verdict["review_revision"] == binding["review_revision"], "BINDING_CHANGED")
                outcome = self._repair(verdict["verdict"])
            else:
                self._preseed()
                outcome = self._work(retry=self.run["kind"] == "resume")
            if outcome:
                return outcome
        while True:
            with self._owned_host() as (lease, _, _):
                owner = self._workflow()
                passed, generation = self._checks(owner, lease)
                if not passed:
                    self.reason = "CHECKS_REQUIRED"
                    return "blocked"
                if self.spec["reviewer"] is None:
                    return "needs_acceptance"
                if self.launch["checks_only"]:
                    self.reason = "CHECKS_REQUIRED"
                    return "blocked"
                started = ato_core.now()
                self.review_deadline = budget.deadline(owner.dir, owner.state, started=started)
                try:
                    verdict = self._review(generation)
                finally:
                    self.review_deadline = float("inf")
                    owner._accounted_at = started
                    owner.persist()
                if verdict is None:
                    return "blocked"
                if verdict["verdict"] == "approve":
                    return "needs_acceptance"
                if verdict["verdict"] == "blocked":
                    self.reason = "REVIEWER_BLOCKED"
                    return "blocked"
                if recheck:
                    self.reason = "REVIEW_CHANGES_REQUIRED"
                    return "blocked"
            self.product, self.checks, self.review = None, [], None
            outcome = self._repair(verdict)
            if outcome:
                return outcome

    def drive(self):
        signals = {sig: signal.signal(sig, self._handle_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        command_token = runner.execution_host.set(self.host)
        process_token = hub_sandbox.process_host.set(self.host)
        try:
            try:
                self._startup()
                with budget.use_time_policy(self.launch["time_policy"]):
                    outcome = self._execute()
            except ato_core.Halt as halt:
                outcome = str(halt)
                if outcome in {"limit", "stage_limit", "planning_limit"}:
                    self.reason, outcome = "TIME_LIMIT", "blocked"
                if outcome not in {"stopped", "cleanup_blocked", "blocked"}:
                    self.host.unknown = True
            except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                code = exc.code if isinstance(exc, TaskError) else "RUN_UNSETTLED"
                if self.admitted:
                    outcome = "blocked"
                    self.host.unknown = True
                    self.reason = code
                else:
                    outcome = "launch_failed"
                    self.reason = code
            if not self.admitted:
                reason = "STOPPED" if outcome == "stopped" else self.reason or "LAUNCH_FAILED"
                _publish_receipt(self.store, self.run, "launch_failed", self.run["generation"],
                    {"reason": reason, "cleanup_verified": True}, "launch_failed")
                return
            self._settle(outcome)
        finally:
            hub_sandbox.process_host.reset(process_token)
            runner.execution_host.reset(command_token)
            for sig, handler in signals.items():
                signal.signal(sig, handler)

    def _settle(self, outcome):
        task = self.store.load_task(self.task_id)
        state, contract, generation, kernel_stop = self.store._kernel_facts(task)
        _, child_active, _ = self.store._stage_facts(task, contract, state)
        if (self.host.cleanup_blocked or self.host.processes or child_active or state.get("owner_pid")
                or state.get("active_process") or state["status"] == "cleanup_blocked"):
            outcome = "cleanup_blocked"
            self.reason = "CLEANUP_BLOCKED"
        if self.host.unknown and outcome != "cleanup_blocked":
            _publish_receipt(self.store, self.run, "unknown", generation,
                {"reason": "RUN_UNSETTLED", "cleanup_verified": False, "owner_birth": self.birth})
            return
        if outcome != "cleanup_blocked" and self.reason != "REVIEWER_MUTATION_VIOLATION":
            if self.stopped() or kernel_stop:
                self.reason, outcome = "STOPPED", "stopped"
        evidence = dict(reason=self.reason, cleanup_verified=outcome != "cleanup_blocked",
                        review_rounds=self.review_rounds, fingerprints=self.fingerprints,
                        product=self.product, checks=self.checks, review=self.review,
                        owner_pid=self.pid, owner_birth=self.birth, kernel_fence=self.kernel_fence)
        if evidence["cleanup_verified"]:
            with _worker_lock(task):
                _require(generation == self.kernel_generation, "RUN_UNSETTLED")
                _verify_released(state, self.run, generation, self.kernel_fence)
                with self.store._private(self.folder) as fd:
                    _save(fd, "settlement.json", dict(
                        run_id=self.run["run_id"], runtime_seal=self.run["runtime_seal"],
                        nonce_sha256=self.launch["nonce_sha256"], generation=generation,
                        outcome=outcome, data=evidence), exclusive=True)
        _publish_receipt(self.store, self.run, "settled", generation, evidence, outcome)


def run_child(store, task_id, run_id, capability, hooks):
    """Private runtime entry after reading the one-shot inherited capability."""
    _identifier(task_id, "task")
    _identifier(run_id, "run")
    _require(capability["task_id"] == task_id and capability["run_id"] == run_id, "FORBIDDEN")
    task = store.load_task(task_id)
    run, receipts = _current(store, task)
    _require(run["run_id"] == run_id and receipts and receipts[-1][1]["phase"] == "launching",
             "RUN_UNSETTLED")
    launch = _evidence(store, task_id, receipts[-1][1])
    nonce = capability["nonce"]
    _require(isinstance(nonce, str) and secrets.compare_digest(_sha(nonce.encode()), launch["nonce_sha256"]),
             "FORBIDDEN")
    _require(launch["parent_pid"] == os.getppid()
             and ato_process.disposition(launch["parent_pid"], launch["parent_birth"]) == "live",
             "FORBIDDEN")
    gate = capability["gate"]
    with store._private(store.root / "repo-gates") as fd:
        handle = os.fstat(gate)
        slot = _existing(fd, _gate_name(task))
        _regular(handle, True, "STATE_CORRUPT")
        _require(slot is not None and _identity(handle) == _identity(slot), "FORBIDDEN")
        # The child must retain the inherited inode and it must own the
        # flock before doing anything that could affect the workspace.
        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(gate, False)
    folder = _activity_path(store, run)
    folder.mkdir(mode=0o700)
    with ato_config.use_unified_kernel(store.kernel_root, store.native_home):
        _RunDriver(store, run, launch, hooks, folder).drive()
