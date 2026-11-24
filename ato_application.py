"""Application composition for the public command groups."""
import os
import subprocess
import sys
from pathlib import Path
import time

import ato_config
import ato_process
import ato_product
import budget
import hub_sandbox
from ato_execution import _current, _repo_gate, _worker_lock, _verify_exit, _active_proof
from ato_host import Host, host_record
from ato_private import render_notice
from ato_release_lib import ObserverProbe, Refused, mailbox
from ato_seal import installed_boundary, runtime_seal
from ato_task_store import (
    TaskError, _existing, _git, _git_worktrees, _git_text, _read_json, _require, _save, _sha, _strict_json,
    normalize_repository,
)
import hub_worktree
import notification_state


class Application:
    def __init__(self, host=None):
        self.host = host

    def dispatch(self, args):
        if self.host is not None:
            return self._dispatch(args)
        operation = args.operation
        subcommand = getattr(args, "subcommand", None)
        read_only = (operation in {"status", "list", "inbox", "system"}
                     or subcommand in {"validate", "trace", "doctor", "binding"})
        seal = runtime_seal(required=not read_only)
        host = Host(seal=seal, read_only=read_only).compose(render_notice=render_notice)
        if read_only:
            try:
                return Application(host)._dispatch(args)
            finally:
                host.close_mailbox_snapshot()
        with installed_boundary(seal):
            return Application(host)._dispatch(args)

    def _dispatch(self, args):
        operation = args.operation
        subcommand = getattr(args, "subcommand", None)
        host = self.host
        api = host.interaction
        if operation == "project" and subcommand == "validate":
            return host.store.normalize_spec(args.repo, args.spec).preview()
        if operation == "system":
            if subcommand == "status":
                return system_status(host)
            return notification_policy(host, getattr(args, "repo", None))
        if operation == "agent" and subcommand == "doctor":
            return doctor(host, probe_versions=getattr(args, "probe_versions", False))
        actor_operation = {"run": "initial", "list": "observe", "status": "observe"}.get(
            operation, subcommand or operation)
        proof = host.caller(actor_operation)
        if operation == "answer":
            principal = host.store._principal(proof)
            _, task, _, _ = api._locate(principal, args.request_id)
            _require(task["creation"]["runtime_seal"] == host.store.runtime_seal, "RUNTIME_MISMATCH")
        elif operation == "resume" or subcommand in {"handoff", "reviewer", "recheck", "ask"}:
            principal = host.store._principal(proof)
            observe = host.binding.handoff_observe if subcommand == "handoff" else None
            _require(host.store._visible_scope(args.task_id, principal, observe=observe) is not None, "NOT_FOUND")
            task = host.store.load_task(args.task_id)
            _require(task["creation"]["runtime_seal"] == host.store.runtime_seal, "RUNTIME_MISMATCH")
        if operation == "run":
            return self.run(args, proof)
        if operation == "list":
            return host.store.list_tasks(proof, getattr(args, "repo", None))
        if operation == "status":
            status = api.task_status(proof, args.task_id)
            task = host.store.load_task(args.task_id)
            if (host.store.runtime_seal is not None and status["status"] != "completed"
                    and not task["worktree_removed"]
                    and task["creation"]["runtime_seal"] != host.store.runtime_seal):
                status.update(reason="RUNTIME_MISMATCH", next_step="SELECT_TASK_RUNTIME")
            return status
        if operation == "stop":
            result = host.execution.request_stop(proof, args.task_id)
            return api.task_status(proof, result["task_id"])
        if operation == "resume":
            result = host.execution.resume(proof, args.task_id)
            return api.task_status(proof, result["task_id"])
        if operation == "inbox":
            return api.inbox(proof)
        if operation == "answer":
            return api.record_answer(proof, args.request_id, args.answer, None)
        if operation == "accept":
            return api.accept_product(proof, args.task_id, getattr(args, "evidence", None))
        if operation == "project":
            if subcommand == "trace":
                result = api.task_status(proof, args.task_id, trace=True)
                return {key: result[key] for key in ("task_id", "status", "execution", "stage",
                                                      "run_history", "checks", "delivery")}
            if subcommand == "cleanup":
                return cleanup(host, proof, args.task_id)
            host.execution._actor(proof, args.task_id, "recheck")
            return self.wait(proof, args.task_id, lambda: host.execution.recheck(
                proof, args.task_id, getattr(args, "checks_only", False), foreground=True))
        if subcommand == "binding":
            return host.binding.binding(proof, args.task_id)
        if subcommand == "handoff":
            return host.binding.handoff(proof, args.task_id, args.to, getattr(args, "interaction", None),
                                        args.evidence)
        if subcommand == "ask":
            continuation = getattr(args, "continuation", None)
            purpose = getattr(args, "purpose_ref", None)
            if args.kind == "host_handoff" and continuation is None and purpose is None:
                continuation = "recheck"
                purpose = api.host_handoff_purpose(host.store.load_spec(args.task_id))
            return api.ask(proof, args.task_id, kind=args.kind, question=args.question,
                           reason=args.reason, continuation=continuation, purpose_ref=purpose)
        reviewer = dict(kind=args.kind, model=getattr(args, "model", None), effort=getattr(args, "effort", None))
        return host.binding.select_reviewer(proof, args.task_id, reviewer, args.evidence)

    def run(self, args, proof):
        host = self.host
        spec = host.store.normalize_spec(args.repo, args.spec)
        principal = host.store._principal(proof)
        grant = host.initial(spec, principal)
        _require(host.execution.profile is not None, "UNSUPPORTED_CAPABILITY")
        task = host.store.create_task(spec, proof, grant.binding)
        task_id = task["task_id"]
        try:
            foreground = getattr(args, "foreground", False)
            launch = lambda: host.execution.start(proof, task_id, foreground=foreground)
            if foreground:
                return self.wait(proof, task_id, launch)
            launch()
            return host.interaction.task_status(proof, task_id)
        except TaskError as exc:
            raise TaskError(exc.code, task_id) from None
        except Exception:
            raise TaskError("INTERNAL_ERROR", task_id) from None

    def wait(self, proof, task_id, execute):
        try:
            execute()
        except KeyboardInterrupt:
            self.host.execution.request_stop(proof, task_id)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = self.host.execution.task_status(proof, task_id)
                if status["status"] == "completed":
                    return self.host.interaction.task_status(proof, task_id)
                if status["status"] == "stopped":
                    raise TaskError("STOPPED", task_id) from None
                if status["status"] == "cleanup_blocked":
                    break
                time.sleep(.05)
            raise TaskError("CLEANUP_BLOCKED", task_id) from None
        status = self.host.interaction.task_status(proof, task_id)
        if status["status"] == "completed":
            return status
        code = {
            "stopped": "STOPPED",
            "cleanup_blocked": "CLEANUP_BLOCKED",
            "delivery_unknown": "DELIVERY_UNKNOWN",
        }.get(status["status"], "RUN_UNSETTLED")
        raise TaskError(code, task_id)


def notification_policy(host, repo):
    store = host.store
    normalized = normalize_repository(repo) if repo is not None else None
    with ato_config.use_unified_kernel(store.kernel_root, store.native_home):
        def read(path):
            return host_record(path, store.home)

        try:
            policy = notification_state.status(read=read)
            muted = bool(normalized and any(Path(rule["repo"]).resolve() == Path(normalized.root)
                                           for rule in notification_state.project_mutes(read=read)))
        except (OSError, ValueError, TypeError, KeyError):
            raise TaskError("STATE_CORRUPT") from None
    return dict(mode=policy["mode"], configured=policy["configured"],
                completion=policy.get("completion", "silent"), muted=muted,
                disposition="recorded_only" if muted or not policy["configured"] else policy["mode"])


def system_status(host):
    home = host.store.kernel_root / "ctask-interaction"
    delays = (.01, .03, .08, None)
    for delay in delays:
        try:
            mailbox_summary = mailbox(home)
            break
        except (OSError, Refused) as exc:
            if isinstance(exc, Refused) and "changed during" not in str(exc):
                raise TaskError("STATE_CORRUPT") from None
            if delay is None:
                raise TaskError("BUSY") from None
            time.sleep(delay)
    state = "absent"
    pid = None
    row = None
    for delay in delays:
        try:
            row = host_record(home / "control/process.json", host.store.home)
            break
        except FileNotFoundError:
            break
        except (OSError, ValueError) as exc:
            if delay is None:
                raise TaskError(getattr(exc, "code", "STATE_CORRUPT")) from None
            time.sleep(delay)
    if row is not None:
        pid = row.get("pid")
        state = ato_process.disposition(pid, row.get("pid_identity"))
        if state == "live":
            source = dict(root=str(Path(__file__).resolve().parent), python=os.path.abspath(sys.executable),
                          surface="ato-v1", seal=host.store.runtime_seal)
            for delay in delays:
                try:
                    state = "watching" if ObserverProbe().observer(home, source) else "unverified"
                    break
                except (OSError, Refused, ValueError):
                    if delay is None:
                        state = "unverified"
                    else:
                        time.sleep(delay)
    with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
        try:
            time_policy = budget.time_policy(read=lambda path: host_record(path, host.store.home))["mode"]
        except (OSError, ValueError, TypeError, KeyError):
            raise TaskError("STATE_CORRUPT") from None
    return dict(schema_version=1, release=host.store.runtime_seal, namespace="unified-v1", time_policy=time_policy,
                observer=dict(pid=pid, state=state, **host.observer_transport()), mailbox=mailbox_summary)


def doctor(host, *, probe_versions=False):
    import agents
    from hub_registry import FACTORIES

    def capability(kind, path, expected=None):
        selected = Path(path)
        try:
            available = (selected.is_absolute() and selected.resolve() == selected and selected.is_file()
                         and os.access(selected, os.X_OK))
        except (OSError, RuntimeError):
            available = False
        result = dict(available=available, version=None, probe="local_file", expected_version=expected,
                      version_match=None)
        if probe_versions and available and sandbox:
            paths = profile.protected_reads(host.store)
            probe = hub_sandbox.doctor(kind, selected, expected, protected_reads=paths)
            result.update(version=probe["version"], probe="sandboxed_version", code=probe["code"],
                          version_match=probe["code"] == "OK" if expected is not None else None)
        return result

    profile = host.configured_profile()
    sandbox = Path("/usr/bin/sandbox-exec").is_file()
    worker = dict(kind="codex", configured=profile is not None, available=False)
    reviewers = {}
    if profile:
        worker.update(capability("codex", profile.worker_executable))
        for kind, path, _ in profile.review_tools:
            adapter = FACTORIES[kind]
            reviewers[kind] = capability(kind, path, adapter.expected_version)
            reviewers[kind]["capabilities"] = adapter.capabilities().values
    worker.update(local_ready=worker["available"] and sandbox)
    sessions = {kind: agents.registry.session_capabilities(kind).to_mapping()
                for kind in agents.registry.kinds()}
    return dict(worker=worker, reviewers=reviewers, reviewer_kinds=sorted(FACTORIES),
                session=sessions, qualification="unverified", sandbox_available=sandbox,
                observer=host.observer_transport())


def cleanup(host, proof, task_id):
    store = host.store
    execution = host.execution
    task, _, _ = execution._actor(proof, task_id, "cleanup")
    if task["worktree_removed"]:
        return host.interaction.task_status(proof, task_id)
    _require(task["creation"]["phase"] == "ready", "CLEANUP_BLOCKED")
    with _repo_gate(store, task):
        task, _, binding = execution._actor(proof, task_id, "cleanup")
        if task["worktree_removed"]:
            return host.interaction.task_status(proof, task_id)
        store._repo_matches(task)
        worktree = task["worktree"]
        path = Path(worktree["path"])
        with store._private(store._task_dir(task_id)) as fd:
            intent = _read_json(fd, "cleanup.json") if _existing(fd, "cleanup.json") is not None else None
        run, receipts = _current(store, task)
        if owner := _active_proof(store, run, receipts):
            _verify_exit(owner)
        branch = "refs/heads/" + worktree["branch"]
        if not path.exists() and not path.is_symlink():
            _require(intent and intent["path"] == str(path) and intent["run_id"] == task["run_id"]
                     and path not in _git_worktrees(worktree["repo"])
                     and _git_text(worktree["repo"], "rev-parse", "--verify", branch) == intent["head"],
                     "CLEANUP_BLOCKED")
            if task["creation"]["runtime_seal"] != store.runtime_seal:
                status, _ = store._project(dict(task, worktree_removed=True), store.load_spec(task_id))
                _require(status["status"] in {"completed", "stopped"} and status["execution"]["verified"]
                         and status["execution"]["state"] == "idle", "RUNTIME_MISMATCH")
        else:
            with _worker_lock(task):
                store._verify_worktree(task)
                status = store.task_status(proof, task_id)
                retained = intent and "product" in intent
                _require((retained or status["status"] in {"completed", "stopped"}) and status["execution"]["verified"]
                         and status["execution"]["state"] == "idle", "CLEANUP_BLOCKED")
                try:
                    _remove_task_worktree(store, execution, task, binding, status, branch, intent)
                except TaskError:
                    raise
                except (OSError, ValueError, subprocess.SubprocessError):
                    raise TaskError("CLEANUP_BLOCKED", task_id) from None
        task = store.load_task(task_id)
        # The intent fences binding changes until the removal is published.
        with execution.transaction(task_id, task["revision"], task["stop_epoch"], binding["revision"]) as (fd, latest, _):
            latest["worktree_removed"] = True
    return host.interaction.task_status(proof, task_id)


def _remove_task_worktree(store, execution, task, binding, status, branch, intent):
    task_id = task["task_id"]
    path = Path(task["worktree"]["path"])
    head = _git_text(path, "rev-parse", "--verify", "HEAD")
    expected = dict(path=str(path), head=head, run_id=task["run_id"])
    retained = intent and "product" in intent
    original = prepared = None
    if not retained and status["status"] == "completed":
        _require(status["product"] is not None, "PRODUCT_CHANGED")
    if retained or (status["status"] == "completed" and intent is None):
        product = intent["product"] if retained else status["product"]["product_id"].removeprefix("product-")
        manifest = _strict_json(store.read_evidence(task_id, product), "PRODUCT_CHANGED")
        _require(manifest["task_id"] == task_id and manifest["spec_sha256"] == task["spec_sha256"],
                 "PRODUCT_CHANGED")
        measured = ato_product.measure(task)
        if retained and head == intent["head"]:
            _require(ato_product.retained_entries(measured) == ato_product.retained_entries(manifest),
                     "PRODUCT_CHANGED")
        else:
            _require(measured == manifest, "PRODUCT_CHANGED")
        original = (store.read_evidence(task_id, intent["index"]) if retained else ato_product.index_bytes(task))
        _require(_sha(original) == manifest["index_sha256"], "PRODUCT_CHANGED")
        expected["head"], tree, prepared = ato_product.build_retention(
            task, manifest, store._task_dir(task_id), existing=intent["head"] if retained else None)
        _require(ato_product.measure(task) == measured, "PRODUCT_CHANGED")
        expected.update(product=product, prior_head=manifest["head"], tree=tree,
                        index=store.put_evidence(task_id, original))
    if intent is not None and not retained and status["status"] == "completed":
        # A prior release may have journaled a clean removal before exiting.
        measured = ato_product.measure(task)
        _require(status["product"]["product_id"] ==
                 "product-" + _sha(ato_product.canonical_json(measured)), "PRODUCT_CHANGED")
    _require(intent is None or intent == expected, "CLEANUP_BLOCKED")

    def mutate():
        current = store.load_task(task_id)
        _require(current["run_id"] == task["run_id"] and not current["worktree_removed"], "CLEANUP_BLOCKED")
        return execution.transaction(task_id, current["revision"], task["stop_epoch"], binding["revision"])

    if intent is None:
        with mutate() as (fd, _, _):
            _save(fd, "cleanup.json", expected, exclusive=True)
    try:
        if prepared is not None:
            if head != expected["head"]:
                _git(path, "update-ref", branch, expected["head"], head)
            if ato_product.index_bytes(task) != prepared:
                ato_product.install_index(task, prepared)
            _require(ato_product.retained_entries(ato_product.measure(task)) == ato_product.retained_entries(manifest),
                     "PRODUCT_CHANGED")
        hub_worktree.cleanup(task["worktree"], worker_locked=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        if path.is_dir() and path.resolve() == path:
            store._verify_worktree(task)
            if prepared is not None:
                current = _git_text(path, "rev-parse", "--verify", "HEAD")
                _require(current in (expected["prior_head"], expected["head"]), "CLEANUP_BLOCKED")
                _require(ato_product.index_bytes(task) in (prepared, original), "CLEANUP_BLOCKED")
                if current != expected["prior_head"]:
                    _git(path, "update-ref", branch, expected["prior_head"], current)
                if ato_product.index_bytes(task) != original:
                    ato_product.install_index(task, original)
            with mutate() as (fd, _, _):
                _require(_read_json(fd, "cleanup.json") == expected, "CLEANUP_BLOCKED")
                os.unlink("cleanup.json", dir_fd=fd)
                os.fsync(fd)
        raise TaskError("CLEANUP_BLOCKED", task_id) from None
