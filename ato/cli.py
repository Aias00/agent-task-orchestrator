"""Strict CLI interface preserving ATO v1 command groups under the slim v2 engine (Choice 1A)."""
from __future__ import annotations

import argparse
import json
import re
import sys

from ato import __version__, config, executor, inbox, observer, product
from ato.adapters.base import doctor_all
from ato.spec import (
    DEFAULT_MAX_REVIEW_ROUNDS, EFFORTS, REVIEWER_KINDS, MIN_WORKER_TIMEOUT_SECONDS, MAX_WORKER_TIMEOUT_SECONDS,
    TaskError, normalize_repository, normalize_spec,
    read_regular_file, require, require_id, result_envelope,
)
from ato.store import Store


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def error(self, message):
        raise TaskError("USAGE")


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if hasattr(namespace, self.dest):
            parser.error("repeated option")
        setattr(namespace, self.dest, True if self.nargs == 0 else values)


def _opt(parser, name: str, *, required: bool = False, flag: bool = False) -> None:
    parser.add_argument("--" + name, action=_Once, default=argparse.SUPPRESS,
                        required=required, **({"nargs": 0} if flag else {}))


def _parse_timeout_seconds(value: str) -> int:
    detail = "--timeout-seconds must contain only base-10 digits and be in [5, 7200]."
    require(re.fullmatch(r"[0-9]+", value) is not None, "USAGE", detail=detail)
    # Bound conversion even for inputs exceeding Python's integer digit limit.
    digits = value.lstrip("0") or "0"
    require(len(digits) <= len(str(MAX_WORKER_TIMEOUT_SECONDS)), "USAGE", detail=detail)
    seconds = int(digits, 10)
    require(MIN_WORKER_TIMEOUT_SECONDS <= seconds <= MAX_WORKER_TIMEOUT_SECONDS, "USAGE", detail=detail)
    return seconds


def build_parser() -> _Parser:
    parser = _Parser(prog="ato", description="Submit and manage durable agent tasks in isolated Git worktrees.")
    parser.add_argument("--version", action="version", version=f"ato {__version__}")
    daily = parser.add_subparsers(dest="operation", required=True)

    def cmd(group, name: str, desc: str, identity: str | None = None):
        sub = group.add_parser(name, help=desc, description=desc)
        if identity:
            sub.add_argument(identity)
        return sub

    def spec_opts(sub):
        _opt(sub, "repo", required=True)
        _opt(sub, "spec", required=True)

    serve = cmd(daily, "serve", "Serve authenticated REST and MCP Streamable HTTP (requires server extra).")
    _opt(serve, "config", required=True)
    _opt(serve, "host")
    _opt(serve, "port")
    mcp = cmd(daily, "mcp", "Serve MCP over local stdio with an explicitly configured principal.")
    _opt(mcp, "config", required=True)
    _opt(mcp, "stdio", required=True, flag=True)

    run = cmd(daily, "run", "Submit a task specification.")
    spec_opts(run)
    _opt(run, "foreground", flag=True)

    _opt(cmd(daily, "list", "List tasks."), "repo")
    _opt(cmd(daily, "status", "Read task and execution status.", "task_id"), "check-history", flag=True)
    cmd(daily, "stop", "Request durable STOP.", "task_id")

    res = cmd(daily, "resume", "Resume an eligible task.", "task_id")
    _opt(res, "foreground", flag=True)
    _opt(res, "timeout-seconds")

    cmd(daily, "inbox", "Read pending human questions.")
    _opt(cmd(daily, "answer", "Record an answer to a pending question.", "request_id"), "answer", required=True)
    _opt(cmd(daily, "accept", "Record optional manual acceptance of a completed task.", "task_id"), "evidence")

    project = cmd(daily, "project", "Repository, diff, check, and worktree operations.")
    psub = project.add_subparsers(dest="subcommand", required=True)
    spec_opts(cmd(psub, "validate", "Validate and preview a task specification."))
    _opt(cmd(psub, "diff", "Inspect repository changes for a task.", "task_id"), "stat", flag=True)
    cmd(psub, "trace", "Read a bounded task event timeline.", "task_id")
    _opt(cmd(psub, "recheck", "Re-run frozen checks and optional reviewer.", "task_id"), "checks-only", flag=True)
    _opt(cmd(psub, "cleanup", "Commit accepted changes to branch and remove worktree.", "task_id"),
         "confirm", required=True, flag=True)

    agent = cmd(daily, "agent", "Adapter diagnostics, reviewer selection, and user questions.")
    asub = agent.add_subparsers(dest="subcommand", required=True)
    _opt(cmd(asub, "doctor", "Inspect installed agent CLI capabilities."), "probe-versions", flag=True)
    cmd(asub, "binding", "Read task origin and reviewer configuration.", "task_id")
    rev = cmd(asub, "reviewer", "Select or update technical reviewer for a task.", "task_id")
    _opt(rev, "kind", required=True)
    _opt(rev, "model")
    _opt(rev, "effort")
    _opt(rev, "evidence")
    ask = cmd(asub, "ask", "Record a question for the user.", "task_id")
    for field in ("kind", "question", "reason"):
        _opt(ask, field, required=True)
    _opt(ask, "continuation")
    _opt(ask, "purpose-ref")

    system = cmd(daily, "system", "Runtime diagnostics and Observer controls.")
    ssub = system.add_subparsers(dest="subcommand", required=True)
    cmd(ssub, "status", "Read runtime, observer, and notification status.")
    _opt(cmd(ssub, "notification-policy", "Read notification policy and mute status."), "repo")
    cmd(ssub, "observer-start", "Start the background Observer/Watchdog daemon.")
    cmd(ssub, "observer-stop", "Stop the background Observer/Watchdog daemon.")
    return parser


def dispatch(args, *, store: Store | None = None, worker_runner=None, reviewer_runner=None):
    if args.operation in {"serve", "mcp"}:
        from ato.server.entrypoints import run
        return run(args)
    st = store if store is not None else Store()
    op = args.operation
    sub = getattr(args, "subcommand", None)

    if op == "run":
        return executor.submit(
            st, args.repo, args.spec, foreground=getattr(args, "foreground", False),
            worker_runner=worker_runner, reviewer_runner=reviewer_runner,
        )
    if op == "list":
        repo_key = normalize_repository(args.repo).key if getattr(args, "repo", None) else None
        tasks = st.list_tasks(repo_key=repo_key)
        return {
            "tasks": [
                {"task_id": t["id"], "repo": t["repo_root"],
                 "status": "blocked" if t["legacy_advisory_completion"] else t["status"],
                 "reason": t["reason"], "updated_at": t["updated_at"],
                 **({"legacy_advisory_completion": True} if t["legacy_advisory_completion"] else {})}
                for t in tasks
            ]
        }
    if op == "status":
        return executor.task_status_view(st, args.task_id, include_check_history=getattr(args, "check_history", False))
    if op == "stop":
        return executor.request_stop(st, args.task_id)
    if op == "resume":
        return executor.resume(
            st, args.task_id, foreground=getattr(args, "foreground", False),
            timeout_seconds=_parse_timeout_seconds(args.timeout_seconds) if hasattr(args, "timeout_seconds") else None,
            worker_runner=worker_runner, reviewer_runner=reviewer_runner,
        )
    if op == "inbox":
        return inbox.list_inbox(st)
    if op == "answer":
        return inbox.record_answer(st, args.request_id, args.answer)
    if op == "accept":
        note = read_regular_file(args.evidence, code="INVALID_EVIDENCE")[1] if getattr(args, "evidence", None) else None
        inbox.record_acceptance(st, args.task_id, note_bytes=note)
        return executor.task_status_view(st, args.task_id)

    if op == "project":
        if sub == "validate":
            return normalize_spec(args.repo, args.spec).preview()
        if sub == "diff":
            task = st.get_task(args.task_id)
            diff = product.collect_diff(task, include_patch=not getattr(args, "stat", False))
            return {
                "task_id": task["id"],
                "status": "blocked" if task["legacy_advisory_completion"] else task["status"],
                "base": task["base_commit"],
                "branch": task["branch"],
                "worktree_state": task["worktree_state"],
                **diff,
            }
        if sub == "trace":
            st.get_task(args.task_id)
            return {"task_id": args.task_id, "events": st.list_events(args.task_id)}
        if sub == "recheck":
            return executor.recheck(
                st, args.task_id, checks_only=getattr(args, "checks_only", False),
                reviewer_runner=reviewer_runner,
            )
        if sub == "cleanup":
            return executor.cleanup(st, args.task_id)

    if op == "agent":
        if sub == "doctor":
            return doctor_all(probe_versions=getattr(args, "probe_versions", False))
        if sub == "binding":
            task = st.get_task(args.task_id)
            return {
                "task_id": task["id"],
                "origin_kind": task["origin_kind"],
                "origin_session_id": task["origin_session_id"],
                "worker": task["spec"]["worker_contract"].get("agent"),
                "reviewer": task["reviewer"],
            }
        if sub == "reviewer":
            task = st.get_task(args.task_id)
            require(args.kind in REVIEWER_KINDS, "INVALID_SPEC")
            effort = getattr(args, "effort", None)
            require(effort in EFFORTS, "INVALID_SPEC")
            agent_cfg = {"kind": args.kind}
            if getattr(args, "model", None):
                agent_cfg["model"] = args.model
            if effort:
                agent_cfg["effort"] = effort
            existing = dict(task.get("reviewer") or {"session_mode": "fresh_only", "max_review_rounds": DEFAULT_MAX_REVIEW_ROUNDS, "timeout_seconds": 300, "checklist": [], "design_docs": []})
            if args.kind == "codex_acpx":
                require(existing["session_mode"] == "fresh_only" and set(agent_cfg) == {"kind"}, "INVALID_SPEC",
                        detail="codex_acpx requires fresh_only with the host launcher defaults.")
            existing["agent"] = agent_cfg
            existing["allowed_kinds"] = sorted(set(existing.get("allowed_kinds") or []) | {args.kind})
            st.update_task(args.task_id, reviewer=existing)
            return {"task_id": args.task_id, "reviewer": existing}
        if sub == "ask":
            q = inbox.ask_question(
                st, args.task_id, kind=args.kind, question=args.question, reason=args.reason,
                continuation=getattr(args, "continuation", None),
                purpose_ref=getattr(args, "purpose_ref", None),
            )
            return {"request_id": q["id"], "task_id": q["task_id"], "state": q["state"]}

    if op == "system":
        if sub == "status":
            settings = config.load_settings()
            return {
                "schema_version": 2,
                "version": __version__,
                "state_dir": str(config.state_dir()),
                "time_policy": settings.get("time_policy", "advisory"),
                "advisory_checks": bool(settings.get("advisory_checks", True)),
                "observer": observer.observer_status(),
                "notifications": st.notification_counts(),
            }
        if sub == "notification-policy":
            settings = config.load_settings()
            repo_root = normalize_repository(args.repo).root if getattr(args, "repo", None) else None
            return {
                "notifications_enabled": bool(settings.get("notifications_enabled", True)),
                "muted_repos": settings.get("muted_repos", []),
                "repo": repo_root,
                "repo_muted": config.is_repo_muted(repo_root, settings) if repo_root else None,
            }
        if sub == "observer-start":
            return observer.start_daemon(st.db_file)
        if sub == "observer-stop":
            return observer.stop_daemon()

    raise TaskError("USAGE")


def main(argv: list[str] | None = None, *, store: Store | None = None,
         worker_runner=None, reviewer_runner=None) -> int:
    command_args = sys.argv[1:] if argv is None else argv
    mcp_command = bool(command_args) and command_args[0] == "mcp"
    try:
        args = build_parser().parse_args(argv)
        if hasattr(args, "task_id"):
            require_id(args.task_id, "task")
        if hasattr(args, "request_id"):
            require_id(args.request_id, "request")
        data = dispatch(args, store=store, worker_runner=worker_runner, reviewer_runner=reviewer_runner)
        if args.operation in {"serve", "mcp"}:
            return 0
        output = json.dumps(result_envelope(data=data), ensure_ascii=False, allow_nan=False)
        code = 0
    except TaskError as exc:
        output = json.dumps(result_envelope(error=exc), ensure_ascii=False)
        code = exc.exit_code
    except KeyboardInterrupt:
        err = TaskError("STOPPED")
        output, code = json.dumps(result_envelope(error=err)), err.exit_code
    except Exception:
        err = TaskError("INTERNAL_ERROR")
        output, code = json.dumps(result_envelope(error=err)), err.exit_code
    # Never contaminate an MCP stdout protocol stream with CLI diagnostics.
    print(output, file=sys.stderr if mcp_command else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
