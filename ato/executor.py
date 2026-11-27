"""Unified single-layer Task execution engine (run, checks, review, stop, resume, recheck, cleanup)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import threading
import subprocess
import sys
import time

from ato import codex_notify, config, inbox, process, product, reviewer, worktree
from ato.adapters.base import WorkerRequest, get_worker_runner, safe_review_detail, secret_values
from ato.spec import (
    DEFAULT_WORKER_TIMEOUT_SECONDS, MIN_WORKER_TIMEOUT_SECONDS, MAX_WORKER_TIMEOUT_SECONDS,
    TaskError, new_id, normalize_spec, require,
)
from ato.store import Store


def _build_worker_prompt(task: dict, spec: dict, *, attempt: int,
                         feedback: str | None = None, answers: list[dict] | None = None) -> str:
    contract = spec["worker_contract"]
    lines = [
        f"Task ID: {task['id']} (attempt {attempt})",
        f"Objective:\n{contract['objective']}",
        f"Scope:\n{contract['scope']}",
        f"Authorization:\n{contract['authorization']}",
    ]
    if contract.get("acceptance"):
        acc_lines = [f"- [{c['id']}] ({c['kind']}): {c['description']}" for c in contract["acceptance"]]
        lines.append("Acceptance criteria:\n" + "\n".join(acc_lines))
    if spec.get("guardrails"):
        lines.append("Guardrails (DO NOT violate): " + json.dumps(spec["guardrails"]))
    if answers:
        qa = [f"Q ({a['id']}): {a['question']}\nA: {a['answer']}" for a in answers]
        lines.append("User answers to prior questions:\n" + "\n\n".join(qa))
    if feedback:
        lines.append(f"Previous attempt feedback (fix these issues):\n{feedback}")
    lines.append(
        "Complete the objective inside this repository worktree. "
        "Return a JSON object with fields: status (completed|blocked|needs_input), summary, blocker, next_step."
    )
    return "\n\n".join(lines)


def _controller_args_for(task: dict) -> tuple[str, ...]:
    if task["origin_kind"] != "codex" or not task["origin_session_id"]:
        return ()
    return tuple(codex_notify.worker_config_args(config.codex_home(), task["origin_session_id"]))


def _enqueue_terminal_notice(store: Store, task: dict, run_id: str, *, kind: str, body: str) -> None:
    if task["origin_kind"] == "agy":
        initial = "unsupported"
    elif task["origin_kind"] == "codex" and task["origin_session_id"]:
        initial = "muted" if config.is_repo_muted(task["repo_root"]) else "pending"
    else:
        initial = "unsupported"
    store.enqueue_notification(
        dedupe_key=f"{kind}:{task['id']}:{run_id}",
        task_id=task["id"],
        kind=kind,
        target_session=task["origin_session_id"],
        body=body,
        initial_state=initial,
    )


def _is_run_live(run: dict | None) -> bool:
    if not run or run["phase"] == "settled":
        return False
    if run["pid"] and run["pid_identity"]:
        return process.disposition(run["pid"], run["pid_identity"]) == "live"
    return False


def _ensure_idle(store: Store, task: dict) -> None:
    if task["active_run_id"]:
        run = store.get_run(task["active_run_id"])
        require(run["phase"] != "launching" and not _is_run_live(run), "BUSY", task["id"])
    pending = store.acpx_review(task["id"], unresolved=True)
    if pending:
        from ato.adapters.acpx import recovery_blocker
        raise TaskError(recovery_blocker(pending), task["id"])


def _run_timeout(task: dict, run: dict | None = None) -> dict:
    """Resolve old nullable rows without guessing the source of a frozen default."""
    run = run or {}
    seconds = run.get("timeout_seconds")
    if seconds is None:
        seconds = task["spec"]["worker_contract"].get("timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)
    return {
        "timeout_seconds": seconds,
        "timeout_source": run.get("timeout_source") or task.get("spec_timeout_source") or "legacy",
    }


def submit(store: Store, repo: str | Path, spec_file: str | Path, *,
           foreground: bool = False, worker_runner=None, reviewer_runner=None) -> dict:
    norm = normalize_spec(repo, spec_file)
    task_id = new_id("task")
    origin_kind, origin_session = config.detect_submission_origin()
    if origin_kind == "codex" and origin_session:
        try:
            codex_notify.session_path(config.codex_home(), origin_session)
        except ValueError:
            origin_session = None

    wt_path, branch = worktree.create_worktree(
        norm.repository.root, task_id, norm.document["base_ref"],
        shared_env_links=norm.document["worktree"]["shared_env_links"],
    )
    store.create_task(
        task_id=task_id,
        repo_root=norm.repository.root,
        repo_key=norm.repository.key,
        base_commit=norm.document["base_ref"],
        branch=branch,
        worktree_path=wt_path,
        spec_doc=norm.document,
        spec_sha256=norm.spec_sha256,
        spec_timeout_source=norm.timeout_source,
        origin_kind=origin_kind,
        origin_session_id=origin_session,
    )
    timeout = {"timeout_seconds": norm.document["worker_contract"]["timeout_seconds"],
               "timeout_source": norm.timeout_source}
    run = store.create_run(task_id, "initial", **timeout)
    store.append_event(task_id, "task_submitted", {"origin": origin_kind, "run_id": run["id"], **timeout},
                       run_id=run["id"])

    if foreground:
        execute_run(store, task_id, run["id"], worker_runner=worker_runner, reviewer_runner=reviewer_runner)
        return task_status_view(store, task_id)

    _spawn_background_run(store, task_id, run["id"])
    return task_status_view(store, task_id)


def _spawn_background_run(store: Store, task_id: str, run_id: str) -> None:
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE")
    }
    env["ATO_HOME"] = str(config.ato_home())
    env["CODEX_HOME"] = str(config.codex_home())
    log_dir = config.ensure_private_dir(config.runtime_dir() / task_id / run_id)
    log_file = (log_dir / "runner.log").open("ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ato.executor", "_run", task_id, run_id, str(store.db_file)],
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            env=env, start_new_session=True, close_fds=True,
        )
        birth = process.identity(proc.pid)
        if birth is not None:
            store.mark_run_running(task_id, run_id, proc.pid, birth)
    except OSError:
        store.update_run(run_id, phase="settled", outcome="failed", error_code="LAUNCH_FAILED", settled_at=time.time())
        store.update_task(task_id, status="blocked", reason="LAUNCH_FAILED")
        raise TaskError("LAUNCH_FAILED", task_id) from None
    finally:
        log_file.close()


def execute_run(store: Store, task_id: str, run_id: str, *,
                worker_runner=None, reviewer_runner=None) -> dict:
    """Drive one Run synchronously: worker -> checks -> guardrails -> reviewer -> settle."""
    stopped = False

    def handle_term(signum, frame):
        nonlocal stopped
        stopped = True

    def stop_requested() -> bool:
        current = store.get_task(task_id)
        return stopped or current["stop_requested"] or current["active_run_id"] != run_id

    def on_child(pid, birth):
        store.update_run(run_id, child_pid=pid, child_pid_identity=birth)

    old_handler = None
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGTERM, handle_term)
    try:
        task = store.get_task(task_id)
        review = task.get("reviewer") or task["spec"].get("reviewer") or {}
        uses_acpx = review.get("agent", {}).get("kind") == "codex_acpx"
        pid = os.getpid()
        if not store.claim_run_execution(task_id, run_id, pid, process.identity(pid), match_owner=uses_acpx):
            return store.get_task(task_id)
        return _execute_claimed_run(store, task_id, run_id, stop_requested=stop_requested,
                                    on_child=on_child, worker_runner=worker_runner,
                                    reviewer_runner=reviewer_runner)
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)


def _execute_claimed_run(store: Store, task_id: str, run_id: str, *, stop_requested,
                         on_child, worker_runner=None, reviewer_runner=None) -> dict:
    started = time.monotonic()
    settings = config.load_settings()
    advisory_checks = bool(settings.get("advisory_checks", True))
    task = store.get_task(task_id)
    run = store.get_run(run_id)
    timeout = _run_timeout(task, run)
    spec = task["spec"]
    contract = spec["worker_contract"]
    active_reviewer = task.get("reviewer") or spec.get("reviewer")
    uses_acpx = bool(active_reviewer and active_reviewer["agent"]["kind"] == "codex_acpx")
    worker_cfg = contract.get("agent") or {"kind": "codex"}
    worker_kind = worker_cfg["kind"]
    model = worker_cfg.get("model") or contract.get("model")
    max_attempts = contract.get("max_attempts", 3)
    controller_args = _controller_args_for(task)
    answers = store.list_questions(task_id, state="answered")
    runner = worker_runner or get_worker_runner(worker_kind)

    feedback: str | None = None
    attempt = 0
    review_session: str | None = None
    review_round = 0
    max_review_rounds = (active_reviewer or {}).get("max_review_rounds", 1)

    try:
        while attempt < max_attempts:
            if stop_requested():
                raise TaskError("STOPPED", task_id)
            attempt += 1
            store.update_task(
                task_id,
                stage={"stage_id": "implement", "attempts": attempt, "max_attempts": max_attempts,
                       "elapsed_seconds": round(time.monotonic() - started, 3),
                       "remaining_stage_ids": [], "steps": [{"stage_id": "implement", "step_id": "worker", "state": "running", "attempts": attempt}]},
            )
            rt = config.ensure_private_dir(config.runtime_dir() / task_id / run_id / f"attempt-{attempt}")
            req = WorkerRequest(
                task_id=task_id,
                kind=worker_kind,
                worktree=Path(task["worktree_path"]).resolve(),
                runtime_dir=rt,
                prompt=_build_worker_prompt(task, spec, attempt=attempt, feedback=feedback, answers=answers),
                sandbox=contract.get("sandbox", "workspace-write"),
                model=model,
                permissions=contract.get("permissions"),
                controller_args=controller_args,
                timeout_seconds=timeout["timeout_seconds"],
                stop_check=stop_requested, on_child=on_child,
            )
            store.append_event(task_id, "worker_started", {"attempt": attempt, "kind": worker_kind}, run_id=run_id)
            wres = runner(req)
            store.append_event(task_id, "worker_finished", {"attempt": attempt, "status": wres.status}, run_id=run_id)

            if stop_requested() or wres.blocker == "STOPPED":
                raise TaskError("STOPPED", task_id)

            if wres.status == "needs_input":
                q = inbox.ask_question(
                    store, task_id, kind="clarification",
                    question=wres.summary, reason=wres.blocker or "Worker requested user input",
                )
                store.update_run(run_id, phase="settled", outcome="needs_input", settled_at=time.time())
                store.update_task(
                    task_id, status="waiting_input", reason="NEEDS_INPUT",
                    next_step=f"Answer {q['id']} via `ato answer {q['id']} --answer ...` then `ato resume {task_id}`",
                    stage={"stage_id": "implement", "attempts": attempt, "elapsed_seconds": round(time.monotonic() - started, 3),
                           "remaining_stage_ids": [], "steps": [{"stage_id": "implement", "step_id": "worker", "state": "waiting", "attempts": attempt}]},
                )
                return store.get_task(task_id)

            if wres.status == "blocked":
                store.update_run(run_id, phase="settled", outcome="blocked", error_code=wres.blocker or "BLOCKED", settled_at=time.time())
                store.update_task(
                    task_id, status="blocked", reason=wres.blocker or "WORKER_BLOCKED",
                    next_step=wres.next_step or wres.summary,
                    stage={"stage_id": "implement", "attempts": attempt, "elapsed_seconds": round(time.monotonic() - started, 3),
                           "remaining_stage_ids": [], "steps": [{"stage_id": "implement", "step_id": "worker", "state": "blocked", "attempts": attempt}]},
                )
                _enqueue_terminal_notice(store, store.get_task(task_id), run_id, kind="failure",
                                         body=f"ATO 任务 {task_id} 受阻（{wres.blocker or 'WORKER_BLOCKED'}）。请用 ato status {task_id} 查看详情。")
                return store.get_task(task_id)

            if wres.status == "failed":
                feedback = f"Worker failed: {wres.summary}\n{wres.blocker}"
                if attempt < max_attempts:
                    continue
                store.update_run(run_id, phase="settled", outcome="failed", error_code="WORKER_FAILED", settled_at=time.time())
                store.update_task(task_id, status="blocked", reason="WORKER_FAILED", next_step=wres.summary)
                _enqueue_terminal_notice(store, store.get_task(task_id), run_id, kind="failure",
                                         body=f"ATO 任务 {task_id} 执行失败。请用 ato status {task_id} 查看详情。")
                return store.get_task(task_id)

            # Worker reported completed -> run opt-in guardrails and independent host checks.
            task = store.get_task(task_id)
            diff_data = product.collect_diff(task)
            violations = product.check_guardrails(task, spec, diff=diff_data)
            if violations:
                feedback = "Guardrail violations detected: " + json.dumps(violations)
                store.append_event(task_id, "guardrail_violation", {"attempt": attempt, "violations": violations}, run_id=run_id)
                if attempt < max_attempts:
                    continue
                store.update_run(run_id, phase="settled", outcome="blocked", error_code="GUARDRAIL_VIOLATION", settled_at=time.time())
                store.update_task(task_id, status="blocked", reason="GUARDRAIL_VIOLATION",
                                  next_step=f"Inspect violations with `ato project diff {task_id} --stat`")
                _enqueue_terminal_notice(store, store.get_task(task_id), run_id, kind="failure",
                                         body=f"ATO 任务 {task_id} 触发 guardrail 保护规则。请用 ato status {task_id} 查看。")
                return store.get_task(task_id)

            check_rows, checks_passed = product.run_checks(task, spec, stop_check=stop_requested, on_child=on_child)
            if stop_requested():
                raise TaskError("STOPPED", task_id)
            store.save_checks(task_id, run_id, check_rows)
            prod = product.build_product(task, check_rows, diff_data=diff_data)
            store.append_event(task_id, "checks_finished", {"attempt": attempt, "passed": checks_passed}, run_id=run_id)

            if not checks_passed:
                failed_ids = [c["criterion_id"] for c in check_rows if c["state"] != "passed"]
                feedback = "Failed independent checks: " + ", ".join(failed_ids) + "\n" + "\n".join(
                    f"[{c['criterion_id']}]\n{c['output_excerpt'][-1000:]}" for c in check_rows if c["state"] != "passed"
                )
                if attempt < max_attempts:
                    continue
                # Advisory settlement: preserve product & surface CHECKS_FAILED honestly, skip reviewer.
                if advisory_checks:
                    store.update_run(run_id, phase="settled", outcome="completed", settled_at=time.time())
                    store.update_task(
                        task_id, status="completed", reason="CHECKS_FAILED", product=prod, next_step=None,
                        stage={"stage_id": "implement", "attempts": attempt, "elapsed_seconds": round(time.monotonic() - started, 3),
                               "remaining_stage_ids": [], "steps": [{"stage_id": "implement", "step_id": "worker", "state": "completed", "attempts": attempt}]},
                    )
                    _enqueue_terminal_notice(
                        store, store.get_task(task_id), run_id, kind="completion",
                        body=f"ATO 任务 {task_id} 已结束（reason=CHECKS_FAILED）。请用 ato status {task_id} 核对检查结果与产物。",
                    )
                    return store.get_task(task_id)
                store.update_run(run_id, phase="settled", outcome="blocked", error_code="CHECKS_FAILED", settled_at=time.time())
                store.update_task(task_id, status="blocked", reason="CHECKS_FAILED", product=prod)
                return store.get_task(task_id)

            # Optional read-only technical review loop.
            if active_reviewer is not None:
                review_round += 1
                rev = reviewer.run_review_turn(
                    task, spec, run_id, round_num=review_round,
                    resume_session_id=review_session, stop_check=stop_requested, on_child=on_child,
                    runner_override=reviewer_runner, store=store,
                )
                if rev.code == "STOPPED" or (not uses_acpx and stop_requested()):
                    raise TaskError("STOPPED", task_id)
                if not rev.ok or rev.verdict is None:
                    detail = safe_review_detail(ValueError(rev.detail), secret_values())
                    store.append_event(task_id, "review_failed", {"code": rev.code, "detail": detail}, run_id=run_id)
                    store.update_run(run_id, phase="settled", outcome="blocked", error_code=rev.code, settled_at=time.time())
                    store.update_task(task_id, status="blocked", reason=rev.code, product=prod,
                                      next_step=detail)
                    return store.get_task(task_id)
                review_session = rev.session_id or review_session
                store.add_review(task_id, run_id, review_round, active_reviewer["agent"]["kind"], rev.verdict, session_id=review_session)
                store.append_event(task_id, "review_finished", {"round": review_round, "verdict": rev.verdict["verdict"]}, run_id=run_id)
                if rev.verdict["verdict"] == "request_changes":
                    if review_round < max_review_rounds and attempt < max_attempts:
                        feedback = "Reviewer requested changes:\n" + json.dumps(rev.verdict["findings"], ensure_ascii=False)
                        continue
                    store.update_run(run_id, phase="settled", outcome="blocked", error_code="REVIEW_CHANGES_REQUESTED", settled_at=time.time())
                    store.update_task(task_id, status="blocked", reason="REVIEW_CHANGES_REQUESTED", product=prod)
                    return store.get_task(task_id)
                if rev.verdict["verdict"] == "blocked":
                    store.update_run(run_id, phase="settled", outcome="blocked", error_code="REVIEW_BLOCKED", settled_at=time.time())
                    store.update_task(task_id, status="blocked", reason="REVIEW_BLOCKED", product=prod)
                    return store.get_task(task_id)

            if stop_requested():
                raise TaskError("STOPPED", task_id)
            # All checks (and optional review) passed -> settle completed!
            stage = {"stage_id": "implement", "attempts": attempt, "elapsed_seconds": round(time.monotonic() - started, 3),
                     "remaining_stage_ids": [], "steps": [{"stage_id": "implement", "step_id": "worker", "state": "completed", "attempts": attempt}]}
            if uses_acpx:
                if not store.complete_acpx_run(task_id, run_id, prod, stage=stage):
                    raise TaskError("STOPPED", task_id)
            else:
                store.update_run(run_id, phase="settled", outcome="completed", settled_at=time.time())
                store.update_task(task_id, status="completed", reason=None, product=prod, next_step=None, stage=stage)
            _enqueue_terminal_notice(
                store, store.get_task(task_id), run_id, kind="completion",
                body=f"ATO 任务 {task_id} 已完成。请用 ato status {task_id} 查看产物和检查结果。",
            )
            return store.get_task(task_id)

        store.update_run(run_id, phase="settled", outcome="blocked", error_code="MAX_ATTEMPTS", settled_at=time.time())
        store.update_task(task_id, status="blocked", reason="MAX_ATTEMPTS")
        return store.get_task(task_id)
    except TaskError as exc:
        if exc.code == "STOPPED":
            if store.get_task(task_id)["active_run_id"] != run_id:
                return store.get_task(task_id)
            store.update_run(run_id, phase="settled", outcome="stopped", error_code="STOPPED", settled_at=time.time())
            store.update_task(task_id, status="stopped", reason="STOPPED")
            return store.get_task(task_id)
        store.update_run(run_id, phase="settled", outcome="failed", error_code=exc.code, settled_at=time.time())
        store.update_task(task_id, status="blocked", reason=exc.code)
        return store.get_task(task_id)
    except Exception:
        store.update_run(run_id, phase="settled", outcome="failed", error_code="INTERNAL_ERROR", settled_at=time.time())
        store.update_task(task_id, status="blocked", reason="INTERNAL_ERROR")
        return store.get_task(task_id)


def request_stop(store: Store, task_id: str) -> dict:
    task = store.update_task(task_id, stop_requested=True)
    if task["active_run_id"]:
        run = store.get_run(task["active_run_id"])
        acpx = store.acpx_review(task_id)
        # A later worker attempt may have its own child after a review closes.
        if acpx and acpx["run_id"] == run["id"] and (not acpx["bridge_closed"] or run["child_pid"] is None):
            return _stop_acpx_review(store, task_id, run, acpx)
        active_reviewer = task.get("reviewer") or task["spec"].get("reviewer") or {}
        if run["kind"] == "recheck" and _is_run_live(run) and active_reviewer.get("agent", {}).get("kind") == "codex_acpx":
            return task_status_view(store, task_id)

        def stop_recorded(row, prefix):
            pid, birth = row.get(prefix + "pid"), row.get(prefix + "pid_identity")
            if not pid:
                return True
            if not birth:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                except OSError:
                    pass
                return False
            return process.stop_pid_group(pid, birth)

        child_cleaned = stop_recorded(run, "child_")
        executor_cleaned = True
        if run["phase"] != "settled":
            executor_cleaned = stop_recorded(run, "")
        # A child may have spawned between the first read and executor shutdown.
        latest = store.get_run(run["id"])
        latest_child_cleaned = stop_recorded(latest, "child_")
        if not child_cleaned:
            child_cleaned = stop_recorded(run, "child_")
        if not (executor_cleaned and child_cleaned and latest_child_cleaned):
            store.update_task(task_id, status="blocked", reason="CLEANUP_BLOCKED")
            raise TaskError("CLEANUP_BLOCKED", task_id)
        if run["phase"] != "settled":
            store.update_run(run["id"], phase="settled", outcome="stopped", error_code="STOPPED",
                             settled_at=time.time(), child_pid=None, child_pid_identity=None)
        else:
            store.update_run(run["id"], child_pid=None, child_pid_identity=None)
    if task["status"] != "completed":
        store.update_task(task_id, status="stopped", reason="STOPPED")
    store.append_event(task_id, "stop_requested", {})
    return task_status_view(store, task_id)


def _stop_acpx_review(store: Store, task_id: str, run: dict, row: dict) -> dict:
    from ato.adapters.acpx import STOP_GRACE, reconcile_cleanup, recovery_blocker

    store.append_event(task_id, "stop_requested", {}, run_id=run["id"])
    deadline = time.monotonic() + STOP_GRACE
    while _is_run_live(run) and time.monotonic() < deadline:
        time.sleep(0.05)
        row = store.acpx_review(task_id)
        run = store.get_run(run["id"])
    if not row["bridge_closed"]:
        reconcile_cleanup(store, row, stop=True)
        row = store.acpx_review(task_id)
    if not _is_run_live(run):
        store.settle_acpx_stop(task_id, run["id"], recovery_blocker(row))
    return task_status_view(store, task_id)


def resume(store: Store, task_id: str, *, foreground: bool = False,
           worker_runner=None, reviewer_runner=None, timeout_seconds: int | None = None) -> dict:
    if timeout_seconds is not None:
        require(type(timeout_seconds) is int
                and MIN_WORKER_TIMEOUT_SECONDS <= timeout_seconds <= MAX_WORKER_TIMEOUT_SECONDS,
                "USAGE", detail="timeout_seconds must be an integer in [5, 7200].")
    task = store.get_task(task_id)
    require(task["worktree_state"] == "present", "NOT_RESUMABLE", task_id)
    _ensure_idle(store, task)
    require(task["status"] in ("stopped", "blocked", "waiting_input"), "NOT_RESUMABLE", task_id)
    if task["status"] == "waiting_input":
        require(not store.list_questions(task_id, state="open"), "NOT_RESUMABLE", task_id)
    worktree.verify_worktree(task)
    previous_run = store.get_run(task["active_run_id"]) if task["active_run_id"] else None
    previous_timeout = _run_timeout(task, previous_run)["timeout_seconds"]
    timeout = _run_timeout(task)
    if timeout_seconds is not None:
        timeout = {"timeout_seconds": timeout_seconds, "timeout_source": "resume_override"}
    run = store.create_run(task_id, "resume", expected_revision=task["revision"], **timeout)
    store.append_event(task_id, "task_resumed", {
        "run_id": run["id"], **timeout, "previous_timeout_seconds": previous_timeout,
        "timeout_overridden": timeout_seconds is not None,
    }, run_id=run["id"])
    if foreground:
        execute_run(store, task_id, run["id"], worker_runner=worker_runner, reviewer_runner=reviewer_runner)
        return task_status_view(store, task_id)
    _spawn_background_run(store, task_id, run["id"])
    return task_status_view(store, task_id)


def recheck(store: Store, task_id: str, *, checks_only: bool = False, reviewer_runner=None) -> dict:
    task = store.get_task(task_id)
    _ensure_idle(store, task)
    worktree.verify_worktree(task)
    spec = task["spec"]
    active_rev = task.get("reviewer") or spec.get("reviewer")
    acpx = active_rev and active_rev["agent"]["kind"] == "codex_acpx"
    owner = (os.getpid(), process.identity(os.getpid())) if acpx else None
    run = store.create_run(task_id, "recheck", expected_revision=task["revision"], owner=owner)

    def stop_requested():
        current = store.get_task(task_id)
        return current["stop_requested"] or current["active_run_id"] != run["id"]

    diff_data = product.collect_diff(task)
    violations = product.check_guardrails(task, spec, diff=diff_data)
    if violations:
        store.update_run(run["id"], phase="settled", outcome="blocked", error_code="GUARDRAIL_VIOLATION", settled_at=time.time())
        store.update_task(task_id, status="blocked", reason="GUARDRAIL_VIOLATION")
        return task_status_view(store, task_id)
    try:
        check_rows, passed = product.run_checks(task, spec, stop_check=stop_requested if acpx else None)
    except TaskError as exc:
        if not acpx or exc.code != "STOPPED":
            raise
        store.settle_acpx_stop(task_id, run["id"], None)
        return task_status_view(store, task_id)
    store.save_checks(task_id, run["id"], check_rows)
    prod = product.build_product(task, check_rows, diff_data=diff_data)
    reason = None if passed else "CHECKS_FAILED"
    if passed and not checks_only and active_rev:
        rev = reviewer.run_review_turn(task, spec, run["id"], round_num=1, runner_override=reviewer_runner,
                                       stop_check=stop_requested if acpx else None, store=store)
        if acpx and rev.code == "STOPPED":
            store.settle_acpx_stop(task_id, run["id"], None)
            return task_status_view(store, task_id)
        if not rev.ok or rev.verdict is None:
            detail = safe_review_detail(ValueError(rev.detail), secret_values())
            store.append_event(task_id, "review_failed", {"code": rev.code, "detail": detail}, run_id=run["id"])
            store.update_run(run["id"], phase="settled", outcome="blocked", error_code=rev.code, settled_at=time.time())
            store.update_task(task_id, status="blocked", reason=rev.code, product=prod,
                              next_step=detail)
            return task_status_view(store, task_id)
        store.add_review(task_id, run["id"], 1, active_rev["agent"]["kind"], rev.verdict, session_id=rev.session_id)
        if rev.verdict["verdict"] != "approve":
            reason = "REVIEW_CHANGES_REQUESTED" if rev.verdict["verdict"] == "request_changes" else "REVIEW_BLOCKED"
            store.update_run(run["id"], phase="settled", outcome="blocked", error_code=reason, settled_at=time.time())
            store.update_task(task_id, status="blocked", reason=reason, product=prod)
            return task_status_view(store, task_id)
    if acpx:
        if not store.complete_acpx_run(task_id, run["id"], prod, reason=reason):
            store.settle_acpx_stop(task_id, run["id"], None)
            return task_status_view(store, task_id)
    else:
        store.update_run(run["id"], phase="settled", outcome="completed", settled_at=time.time())
        store.update_task(task_id, status="completed", reason=reason, product=prod)
    store.append_event(task_id, "recheck_completed", {"passed": passed, "checks_only": checks_only}, run_id=run["id"])
    return task_status_view(store, task_id)


def cleanup(store: Store, task_id: str) -> dict:
    task = store.get_task(task_id)
    _ensure_idle(store, task)
    if task["worktree_state"] == "removed":
        return task_status_view(store, task_id)
    require(task["status"] in ("completed", "stopped"), "CLEANUP_BLOCKED", task_id)
    wt = worktree.verify_worktree(task)
    with store.remove_worktree(task_id, task["revision"]):
        if task["status"] == "stopped":
            require(worktree.is_clean(wt, ignored=worktree.shared_links_for(task)), "CLEANUP_BLOCKED", task_id)
        else:
            worktree.commit_worktree_product(task)
        worktree.remove_worktree(task)
    store.append_event(task_id, "worktree_removed", {})
    return task_status_view(store, task_id)


def task_status_view(store: Store, task_id: str) -> dict:
    from ato.watchdog import public_status as watchdog_public_status

    task = store.get_task(task_id)
    runs = store.list_runs(task_id)
    active = next((r for r in reversed(runs) if r["id"] == task["active_run_id"]), runs[-1] if runs else None)
    if active and active["phase"] != "settled":
        live = _is_run_live(active)
        exec_state = "active" if live else "unknown"
    else:
        exec_state = "idle"
    checks = store.latest_checks(task_id)
    rev = store.latest_review(task_id)
    open_q = store.list_questions(task_id, state="open")
    notices = store.list_notifications(task_id)
    comp = next((n for n in reversed(notices) if n["kind"] == "completion"), None)
    status, reason, next_step = task["status"], task["reason"], task["next_step"]
    acpx_view = None
    acpx = store.acpx_review(task_id)
    if acpx:
        from ato.adapters.acpx import delivery_of, recovery_blocker

        active_review = active and active["id"] == acpx["run_id"] and exec_state == "active"
        delivery = delivery_of(acpx)
        if active_review and acpx["receipt"] is None:
            delivery = "approval_recorded" if acpx["dispatch"] else "pending"
        receipt = acpx["receipt"] or {}
        acpx_view = {
            "run_id": acpx["run_id"], "round": acpx["round"],
            "dispatch_recorded": acpx["dispatch"] is not None,
            "delivery": delivery, "transport": receipt.get("transport"),
            "cancel_requested": receipt.get("cancel_requested"),
            "cancel_written": receipt.get("cancel_written"),
            "cancel_acknowledged": receipt.get("cancel_acknowledged"),
            "group_cleanup": acpx["cleanup"], "execution_cleanup_verified": bool(acpx["bridge_closed"]),
        }
        blocker = recovery_blocker(acpx)
        if blocker and not active_review:
            status = "blocked"
            reason = blocker if reason != "MUTATION_VIOLATION" else reason
            next_step = (f"Use `ato stop {task_id}` to verify local process cleanup. "
                         if not acpx["bridge_closed"] else "")
            if blocker == "ACPX_DELIVERY_UNKNOWN":
                next_step += "Inspect the unresolved reviewer outcome. ATO will not replay the prompt."

    if task["origin_kind"] == "agy":
        comp_state = "unsupported"
        verified_session = False
    elif task["origin_kind"] == "codex":
        verified_session = bool(task["origin_session_id"])
        if not verified_session:
            comp_state = "unbound"
        elif comp is None:
            comp_state = "no_delivery_record"
        else:
            comp_state = comp["state"]
    else:
        verified_session = None
        comp_state = "unsupported"

    return {
        "task_id": task["id"],
        "repo": task["repo_root"],
        "status": status,
        "reason": reason,
        "updated_at": task["updated_at"],
        "spec_sha256": task["spec_sha256"],
        "execution": {
            **_run_timeout(task, active),
            "state": exec_state,
            "run_id": active["id"] if active else None,
            "pid": active["pid"] if active and exec_state == "active" else None,
            "verified": exec_state != "unknown",
            "stop_requested": task["stop_requested"],
        },
        "run_history": [
            {"run_id": r["id"], "kind": r["kind"], "phase": r["phase"], "outcome": r["outcome"],
             **_run_timeout(task, r),
             "created_at": r["created_at"], "settled_at": r["settled_at"]}
            for r in runs
        ],
        "stage": task["stage"],
        "checks": [{"criterion_id": c["criterion_id"], "state": c["state"], "receipt_id": c["receipt_id"]} for c in checks],
        "review": ({"round": rev["round"], "reviewer_kind": rev["reviewer_kind"], "verdict": rev["verdict"], "summary": rev["detail"]["summary"]} if rev else None),
        "product": task["product"],
        "acceptance": task["acceptance"] or {"disposition": "not_recorded", "criteria": []},
        "pending": [{"request_id": q["id"], "kind": q["kind"], "question": q["question"]} for q in open_q],
        "delivery": [{"id": n["id"], "kind": n["kind"], "state": n["state"], "error": n["error"]} for n in notices],
        "worktree_state": task["worktree_state"],
        "next_step": next_step,
        **({"acpx_reviewer": acpx_view} if acpx_view is not None else {}),
        "completion_notification": {
            "origin": task["origin_kind"],
            "session_verified": verified_session,
            "state": comp_state,
        },
        "watchdog": watchdog_public_status(store, task_id),
    }


def _main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "_run":
        _, _, task_id, run_id, db_file = sys.argv
        store = Store(Path(db_file))
        execute_run(store, task_id, run_id)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
