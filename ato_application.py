"""Application composition for the public command groups."""
import os
import sys
from pathlib import Path
import time

import ato_config
import ato_process
import ato_product
from ato_execution import _current, _repo_gate, _worker_lock, _verify_exit, _active_proof
from ato_host import Host, host_record
from ato_private import render_notice
from ato_seal import installed_boundary, runtime_seal
from ato_task_store import (
    TaskError, _existing, _git_worktrees, _git_text, _read_json, _require, _save,
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
            return Application(host)._dispatch(args)
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
            return doctor(host)
        actor_operation = {"run": "initial", "list": "observe", "status": "observe"}.get(
            operation, subcommand or operation)
        proof = host.caller(actor_operation)
        if operation == "answer":
            principal = host.store._principal(proof)
            _, task, _, _ = api._locate(principal, args.request_id)
            _require(task["creation"]["runtime_seal"] == host.store.runtime_seal, "RUNTIME_MISMATCH")
        elif operation in {"stop", "resume"} or subcommand in {"cleanup", "handoff", "reviewer", "recheck", "ask"}:
            principal = host.store._principal(proof)
            _require(host.store._visible_scope(args.task_id, principal) is not None, "NOT_FOUND")
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
                host.execution._actor(proof, args.task_id, "trace")
                result = api.task_status(proof, args.task_id)
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
        policy = notification_state.status()
        muted = bool(normalized and any(Path(rule["repo"]).resolve() == Path(normalized.root)
                                       for rule in notification_state.project_mutes()))
    return dict(mode=policy["mode"], configured=policy["configured"],
                completion=policy.get("completion", "silent"), muted=muted,
                disposition="recorded_only" if muted else policy["mode"])


def system_status(host):
    from scripts.release_snapshot import Refused, System, mailbox
    home = host.store.kernel_root / "ctask-interaction"
    mailbox_summary = mailbox(home)
    state = "absent"
    pid = None
    try:
        row = host_record(home / "control/process.json", host.store.home)
    except FileNotFoundError:
        pass
    else:
        pid = row.get("pid")
        state = ato_process.disposition(pid, row.get("pid_identity"))
        if state == "live":
            source = dict(root=str(Path(__file__).resolve().parent), python=os.path.abspath(sys.executable),
                          surface="ato-v1", seal=host.store.runtime_seal)
            try:
                state = "watching" if System().observer(home, source) else "unverified"
            except (OSError, Refused):
                state = "unverified"
    return dict(schema_version=1, release=host.store.runtime_seal, namespace="unified-v1",
                observer=dict(pid=pid, state=state), mailbox=mailbox_summary)


def doctor(host):
    import agents
    from hub_registry import FACTORIES

    def capability(path):
        selected = Path(path)
        available = selected.is_absolute() and selected.resolve() == selected and selected.is_file()
        return dict(available=available and os.access(selected, os.X_OK),
                    version=None, probe="local_file")

    profile = host.configured_profile()
    sandbox = Path("/usr/bin/sandbox-exec").is_file()
    worker = dict(kind="codex", configured=profile is not None, available=False)
    reviewers = {}
    if profile:
        worker.update(capability(profile.worker_executable))
        reviewers = {kind: capability(path) for kind, path, _ in profile.review_tools}
    worker.update(local_ready=worker["available"] and sandbox)
    sessions = {kind: agents.registry.session_capabilities(kind).to_mapping()
                for kind in agents.registry.kinds()}
    return dict(worker=worker, reviewers=reviewers, reviewer_kinds=sorted(FACTORIES),
                session=sessions, qualification="unverified", sandbox_available=sandbox)


def cleanup(host, proof, task_id):
    store = host.store
    execution = host.execution
    task, _, _ = execution._actor(proof, task_id, "cleanup")
    if task["worktree_removed"]:
        return host.interaction.task_status(proof, task_id)
    _require(task["creation"]["phase"] == "ready", "CLEANUP_BLOCKED")
    _require(task["creation"]["runtime_seal"] == store.runtime_seal, "RUNTIME_MISMATCH")
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
        else:
            with _worker_lock(task):
                store._verify_worktree(task)
                status = store.task_status(proof, task_id)
                _require(status["status"] in {"completed", "stopped"} and status["execution"]["verified"]
                         and status["execution"]["state"] == "idle", "CLEANUP_BLOCKED")
                head = _git_text(path, "rev-parse", "--verify", "HEAD")
                if status["status"] == "completed":
                    manifest = ato_product.measure(task)
                    _require(status["product"]["product_id"] == "product-" + ato_product._sha(
                        ato_product.canonical_json(manifest)), "PRODUCT_CHANGED")
                expected = dict(path=str(path), head=head, run_id=task["run_id"])

                def admit_removal():
                    _require(intent is None or intent == expected, "CLEANUP_BLOCKED")
                    if intent is None:
                        with execution.transaction(task_id, task["revision"], task["stop_epoch"],
                                                   binding["revision"]) as (fd, _, _):
                            _save(fd, "cleanup.json", expected, exclusive=True)

                try:
                    hub_worktree.cleanup(worktree, worker_locked=True, before_remove=admit_removal)
                except (OSError, ValueError):
                    raise TaskError("CLEANUP_BLOCKED", task_id) from None
        task = store.load_task(task_id)
        # The intent fences binding changes until the removal is published.
        with execution.transaction(task_id, task["revision"], task["stop_epoch"], binding["revision"]) as (fd, latest, _):
            latest["worktree_removed"] = True
    return host.interaction.task_status(proof, task_id)
