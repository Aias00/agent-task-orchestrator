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
    DEFAULT_MAX_REVIEW_ROUNDS, DEFAULT_MAX_NO_PROGRESS_ROUNDS,
    TaskError, blocker_fingerprint, canonical_json, new_id, normalize_spec, require, sha256_bytes,
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
        require(not _is_run_live(run), "BUSY", task["id"])


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
        return stopped or store.get_task(task_id)["stop_requested"]

    def on_child(pid, birth):
        store.update_run(run_id, child_pid=pid, child_pid_identity=birth)

    old_handler = None
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGTERM, handle_term)
    try:
        if not store.claim_run_execution(task_id, run_id, os.getpid(), process.identity(os.getpid())):
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
    worker_cfg = contract.get("agent") or {"kind": "codex"}
    worker_kind = worker_cfg["kind"]
    model = worker_cfg.get("model") or contract.get("model")
    max_attempts = contract.get("max_attempts", 3)
    controller_args = _controller_args_for(task)
    answers = store.list_questions(task_id, state="answered")
    runner = worker_runner or get_worker_runner(worker_kind)

    feedback: str | None = None
    turn = 0
    execution_failures = verification_failures = review_rework_rounds = 0
    review_session: str | None = None
    review_kind: str | None = None
    review_round = 0
    active_reviewer = task.get("reviewer") or spec.get("reviewer")
    max_review_rounds = (active_reviewer or {}).get("max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS)
    max_no_progress = contract.get("max_no_progress_rounds", DEFAULT_MAX_NO_PROGRESS_ROUNDS)
    no_progress_streak = 0
    progress_made = True
    repeated_kind = None
    repeated_items: list = []
    prev_failure_signature = None
    prev_blocker_fp = None
    prev_blocker_keys: set = set()
    previous_findings: list[dict] = []
    category = "implement"
    prod = task.get("product")
    wt = Path(task["worktree_path"]).resolve()
    ignored = worktree.shared_links_for(task)

    def stage(*, state="running", stage_id=None, step_id="worker") -> dict:
        sid = stage_id or category
        return {
            "stage_id": sid, "category": category, "turn": turn,
            "attempts": turn, "max_attempts": max_attempts,
            "execution_failures": execution_failures, "max_execution_failures": max_attempts,
            "verification_failures": verification_failures,
            "review_rework_rounds": review_rework_rounds, "max_review_rounds": max_review_rounds,
            "no_progress_streak": no_progress_streak, "max_no_progress": max_no_progress,
            "progress_made": progress_made, "repeated_kind": repeated_kind,
            "repeated_items": repeated_items,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "remaining_stage_ids": [],
            "steps": [{"stage_id": sid, "step_id": step_id, "state": state, "attempts": turn}],
        }

    def settle_blocked(reason, next_step, *, outcome="blocked") -> dict:
        store.update_run(run_id, phase="settled", outcome=outcome, error_code=reason, settled_at=time.time())
        store.update_task(task_id, status="blocked", reason=reason, product=prod,
                          next_step=next_step, stage=stage(state="blocked"))
        _enqueue_terminal_notice(store, store.get_task(task_id), run_id, kind="failure",
                                 body=f"ATO 任务 {task_id} 受阻（{reason}）。请用 ato status {task_id} 查看详情。")
        return store.get_task(task_id)

    def reset_progress() -> None:
        nonlocal no_progress_streak, progress_made, repeated_kind, repeated_items, prev_failure_signature
        no_progress_streak = 0
        progress_made = True
        repeated_kind = None
        repeated_items = []
        prev_failure_signature = None

    def failure_progress(kind, details, items) -> None:
        nonlocal prev_failure_signature, no_progress_streak, progress_made, repeated_kind, repeated_items
        signature = (kind, worktree.tree_fingerprint(wt, ignored=ignored), canonical_json(details))
        no_progress_streak = no_progress_streak + 1 if signature == prev_failure_signature else 0
        prev_failure_signature = signature
        progress_made = no_progress_streak == 0
        repeated_kind = kind if no_progress_streak else None
        repeated_items = items if no_progress_streak else []

    def phase_passed(kind) -> None:
        # A successful worker must not clear a repeated host-check or review failure.
        if prev_failure_signature is not None and prev_failure_signature[0] == kind:
            reset_progress()

    def repeated_pause() -> dict | None:
        nonlocal category, feedback
        if not no_progress_streak:
            return None
        warning = (f"Repeated issue ({repeated_kind}, no-progress streak {no_progress_streak}/{max_no_progress}): "
                   + json.dumps(repeated_items, ensure_ascii=False)
                   + ". Resolve these issues with a substantive change before retrying.")
        feedback = (feedback or "") + "\nWARNING: " + warning
        if no_progress_streak < max_no_progress:
            return None
        category = "repeated_no_progress"
        store.append_event(task_id, "repeated_no_progress",
                           {"kind": repeated_kind, "streak": no_progress_streak, "items": repeated_items},
                           run_id=run_id)
        return settle_blocked("REPEATED_NO_PROGRESS", warning)

    try:
        while True:
            if stop_requested():
                raise TaskError("STOPPED", task_id)
            turn += 1
            task = store.get_task(task_id)
            active_reviewer = task.get("reviewer") or spec.get("reviewer")
            max_review_rounds = (active_reviewer or {}).get("max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS)
            store.update_task(task_id, stage=stage())
            rt = config.ensure_private_dir(config.runtime_dir() / task_id / run_id / f"attempt-{turn}")
            req = WorkerRequest(
                task_id=task_id, kind=worker_kind, worktree=wt, runtime_dir=rt,
                prompt=_build_worker_prompt(task, spec, attempt=turn, feedback=feedback, answers=answers),
                sandbox=contract.get("sandbox", "workspace-write"), model=model,
                permissions=contract.get("permissions"), controller_args=controller_args,
                timeout_seconds=timeout["timeout_seconds"], stop_check=stop_requested, on_child=on_child,
            )
            store.append_event(task_id, "worker_started", {"attempt": turn, "kind": worker_kind}, run_id=run_id)
            wres = runner(req)
            store.append_event(task_id, "worker_finished", {"attempt": turn, "status": wres.status}, run_id=run_id)

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
                    stage=stage(state="waiting"),
                )
                return store.get_task(task_id)
            if wres.status == "blocked":
                return settle_blocked(wres.blocker or "WORKER_BLOCKED", wres.next_step or wres.summary)
            if wres.status == "failed":
                execution_failures += 1
                category = "execution_failure"
                feedback = f"Worker failed: {wres.summary}\n{wres.blocker}"
                failure_progress("execution_failure", [wres.blocker, " ".join(wres.summary.split())],
                                 [{"blocker": wres.blocker, "summary": wres.summary}])
                paused = repeated_pause()
                if paused is not None:
                    return paused
                if execution_failures >= max_attempts:
                    return settle_blocked("WORKER_FAILED", wres.summary, outcome="failed")
                store.update_task(task_id, stage=stage())
                continue
            phase_passed("execution_failure")

            # Worker completion is only a claim; guardrails and host checks remain independent.
            task = store.get_task(task_id)
            store.update_task(task_id, stage=stage(stage_id="verify", step_id="checks"))
            diff_data = product.collect_diff(task)
            violations = product.check_guardrails(task, spec, diff=diff_data)
            if violations:
                verification_failures += 1
                category = "verification_repair"
                feedback = "Guardrail violations detected: " + json.dumps(violations)
                store.append_event(task_id, "guardrail_violation", {"attempt": turn, "violations": violations}, run_id=run_id)
                failure_progress("guardrail", sorted(violations, key=canonical_json), violations)
                paused = repeated_pause()
                if paused is not None:
                    return paused
                if verification_failures >= max_attempts:
                    return settle_blocked("GUARDRAIL_VIOLATION",
                                          f"Inspect violations with `ato project diff {task_id} --stat`")
                store.update_task(task_id, stage=stage())
                continue
            phase_passed("guardrail")

            check_rows, checks_passed = product.run_checks(task, spec, stop_check=stop_requested, on_child=on_child)
            if stop_requested():
                raise TaskError("STOPPED", task_id)
            store.save_checks(task_id, run_id, check_rows)
            prod = product.build_product(task, check_rows, diff_data=diff_data)
            store.append_event(task_id, "checks_finished", {"attempt": turn, "passed": checks_passed}, run_id=run_id)
            if not checks_passed:
                verification_failures += 1
                category = "verification_repair"
                failed = [c for c in check_rows if c["state"] != "passed"]
                failed_ids = sorted(c["criterion_id"] for c in failed)
                feedback = "Failed independent checks: " + ", ".join(failed_ids) + "\n" + "\n".join(
                    f"[{c['criterion_id']}]\n{c['output_excerpt'][-1000:]}" for c in failed
                )
                details = sorted((c["criterion_id"], c["state"],
                                  sha256_bytes(" ".join(c["output_excerpt"].split()).encode())) for c in failed)
                failure_progress("checks", details, failed_ids)
                paused = repeated_pause()
                if paused is not None:
                    return paused
                if verification_failures < max_attempts:
                    store.update_task(task_id, stage=stage())
                    continue
                # Advisory settlement remains available when the repair cap is reached without repetition.
                if advisory_checks:
                    store.update_run(run_id, phase="settled", outcome="completed", settled_at=time.time())
                    store.update_task(task_id, status="completed", reason="CHECKS_FAILED", product=prod,
                                      next_step=None, stage=stage(state="completed"))
                    _enqueue_terminal_notice(
                        store, store.get_task(task_id), run_id, kind="completion",
                        body=f"ATO 任务 {task_id} 已结束（reason=CHECKS_FAILED）。请用 ato status {task_id} 核对检查结果与产物。",
                    )
                    return store.get_task(task_id)
                return settle_blocked("CHECKS_FAILED", "Fix failed independent checks: " + ", ".join(failed_ids))
            phase_passed("checks")
            verification_failures = 0

            # Each review uses the effective task override and receives prior blocker findings.
            task = store.get_task(task_id)
            active_reviewer = task.get("reviewer") or spec.get("reviewer")
            if active_reviewer is not None:
                max_review_rounds = active_reviewer.get("max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS)
                if review_kind != active_reviewer["agent"]["kind"]:
                    review_session = None
                review_kind = active_reviewer["agent"]["kind"]
                review_round += 1
                category = "review_rework"
                store.update_task(task_id, stage=stage(stage_id="review", step_id="reviewer"))
                rev = reviewer.run_review_turn(
                    task, spec, run_id, round_num=review_round, previous_findings=previous_findings,
                    resume_session_id=review_session, stop_check=stop_requested, on_child=on_child,
                    runner_override=reviewer_runner,
                )
                if stop_requested() or rev.code == "STOPPED":
                    raise TaskError("STOPPED", task_id)
                if not rev.ok or rev.verdict is None:
                    detail = safe_review_detail(ValueError(rev.detail), secret_values())
                    store.append_event(task_id, "review_failed", {"code": rev.code, "detail": detail}, run_id=run_id)
                    return settle_blocked(rev.code, detail)
                review_session = rev.session_id or review_session
                store.add_review(task_id, run_id, review_round, review_kind, rev.verdict, session_id=review_session)
                store.append_event(task_id, "review_finished", {"round": review_round, "verdict": rev.verdict["verdict"]}, run_id=run_id)
                previous_findings = [f for f in rev.verdict["findings"] if f["severity"] == "blocker"]
                if rev.verdict["verdict"] == "request_changes":
                    review_rework_rounds += 1
                    tree_fp = worktree.tree_fingerprint(wt, ignored=ignored)
                    blocker_fp = blocker_fingerprint(tree_fp, rev.verdict)
                    blocker_keys = {(f["file"], f["category"], " ".join(f["title"].casefold().split()))
                                    for f in previous_findings}
                    if prev_blocker_fp is not None and (blocker_fp == prev_blocker_fp
                                                       or (prev_blocker_keys and prev_blocker_keys <= blocker_keys)):
                        no_progress_streak += 1
                        progress_made = False
                        repeated_kind = "review_blocker"
                        repeated_items = [{"file": f, "title": title} for f, _, title
                                          in sorted(prev_blocker_keys & blocker_keys)]
                    else:
                        reset_progress()
                    prev_blocker_fp, prev_blocker_keys = blocker_fp, blocker_keys
                    feedback = "Reviewer requested changes:\n" + json.dumps(rev.verdict["findings"], ensure_ascii=False)
                    paused = repeated_pause()
                    if paused is not None:
                        return paused
                    if review_round >= max_review_rounds:
                        return settle_blocked("REVIEW_CHANGES_REQUESTED", "Resolve reviewer blockers: "
                                              + "; ".join(f"{f['file']}: {f['title']}" for f in previous_findings))
                    store.update_task(task_id, stage=stage())
                    continue
                if rev.verdict["verdict"] == "blocked":
                    return settle_blocked("REVIEW_BLOCKED", rev.verdict["summary"])

            if stop_requested():
                raise TaskError("STOPPED", task_id)
            reset_progress()
            store.update_run(run_id, phase="settled", outcome="completed", settled_at=time.time())
            store.update_task(task_id, status="completed", reason=None, product=prod, next_step=None,
                              stage=stage(state="completed"))
            _enqueue_terminal_notice(
                store, store.get_task(task_id), run_id, kind="completion",
                body=f"ATO 任务 {task_id} 已完成。请用 ato status {task_id} 查看产物和检查结果。",
            )
            return store.get_task(task_id)
    except TaskError as exc:
        if exc.code == "STOPPED":
            store.update_run(run_id, phase="settled", outcome="stopped", error_code="STOPPED", settled_at=time.time())
            store.update_task(task_id, status="stopped", reason="STOPPED", stage=stage(state="stopped"))
            return store.get_task(task_id)
        return settle_blocked(exc.code, f"Inspect status and trace for {exc.code}.", outcome="failed")
    except Exception:
        return settle_blocked("INTERNAL_ERROR", "Inspect executor logs and task trace.", outcome="failed")


def request_stop(store: Store, task_id: str) -> dict:
    task = store.get_task(task_id)
    store.update_task(task_id, stop_requested=True)
    if task["active_run_id"]:
        run = store.get_run(task["active_run_id"])

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


def resume(store: Store, task_id: str, *, foreground: bool = False,
           worker_runner=None, reviewer_runner=None, timeout_seconds: int | None = None) -> dict:
    if timeout_seconds is not None:
        require(type(timeout_seconds) is int
                and MIN_WORKER_TIMEOUT_SECONDS <= timeout_seconds <= MAX_WORKER_TIMEOUT_SECONDS,
                "USAGE", detail="timeout_seconds must be an integer in [5, 7200].")
    task = store.get_task(task_id)
    require(task["worktree_state"] == "present", "NOT_RESUMABLE", task_id)
    require(task["status"] in ("stopped", "blocked", "waiting_input"), "NOT_RESUMABLE", task_id)
    _ensure_idle(store, task)
    if task["status"] == "waiting_input":
        require(not store.list_questions(task_id, state="open"), "NOT_RESUMABLE", task_id)
    worktree.verify_worktree(task)
    previous_run = store.get_run(task["active_run_id"]) if task["active_run_id"] else None
    previous_timeout = _run_timeout(task, previous_run)["timeout_seconds"]
    timeout = _run_timeout(task)
    if timeout_seconds is not None:
        timeout = {"timeout_seconds": timeout_seconds, "timeout_source": "resume_override"}
    store.update_task(task_id, stop_requested=False, status="queued", reason=None)
    run = store.create_run(task_id, "resume", **timeout)
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
    run = store.create_run(task_id, "recheck")
    diff_data = product.collect_diff(task)
    violations = product.check_guardrails(task, spec, diff=diff_data)
    if violations:
        store.update_run(run["id"], phase="settled", outcome="blocked", error_code="GUARDRAIL_VIOLATION", settled_at=time.time())
        store.update_task(task_id, status="blocked", reason="GUARDRAIL_VIOLATION")
        return task_status_view(store, task_id)
    check_rows, passed = product.run_checks(task, spec)
    store.save_checks(task_id, run["id"], check_rows)
    prod = product.build_product(task, check_rows, diff_data=diff_data)
    reason = None if passed else "CHECKS_FAILED"
    if passed and not checks_only and (task.get("reviewer") or spec.get("reviewer")):
        rev = reviewer.run_review_turn(task, spec, run["id"], round_num=1, runner_override=reviewer_runner)
        if not rev.ok or rev.verdict is None:
            detail = safe_review_detail(ValueError(rev.detail), secret_values())
            store.append_event(task_id, "review_failed", {"code": rev.code, "detail": detail}, run_id=run["id"])
            store.update_run(run["id"], phase="settled", outcome="blocked", error_code=rev.code, settled_at=time.time())
            store.update_task(task_id, status="blocked", reason=rev.code, product=prod,
                              next_step=detail)
            return task_status_view(store, task_id)
        active_rev = task.get("reviewer") or spec["reviewer"]
        store.add_review(task_id, run["id"], 1, active_rev["agent"]["kind"], rev.verdict, session_id=rev.session_id)
        if rev.verdict["verdict"] != "approve":
            reason = "REVIEW_CHANGES_REQUESTED" if rev.verdict["verdict"] == "request_changes" else "REVIEW_BLOCKED"
            store.update_run(run["id"], phase="settled", outcome="blocked", error_code=reason, settled_at=time.time())
            store.update_task(task_id, status="blocked", reason=reason, product=prod)
            return task_status_view(store, task_id)
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
    if task["status"] == "stopped":
        require(worktree.is_clean(wt, ignored=worktree.shared_links_for(task)), "CLEANUP_BLOCKED", task_id)
    else:
        worktree.commit_worktree_product(task)
    worktree.remove_worktree(task)
    store.update_task(task_id, worktree_state="removed", stage=None)
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
        "status": task["status"],
        "reason": task["reason"],
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
        "next_step": task["next_step"],
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
