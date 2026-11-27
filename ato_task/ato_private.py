"""Version-bound compact notices and the scoped reader for the private runtime."""
from pathlib import Path
import shlex

from ato_common.ato_config import private_runtime_command
from ato_task.ato_task_store import DELIVERY_OUTCOMES, TaskError, _identifier, _require, _sha, canonical_json


def _valid_notice_body(body, ref):
    if not isinstance(body, str):
        return False
    lines = body.split("\n")
    if len(lines) != 2 or lines[0] != f"[ato handoff {ref}]":
        return False
    try:
        tokens = shlex.split(lines[1])
    except ValueError:
        return False
    return (len(tokens) >= 4 and Path(tokens[-3]).name == "ato_runtime.py"
            and tokens[-2:] == ["read-handoff", ref]
            and all(flag.startswith("-") for flag in tokens[1:-3]))


def handoff_id(task, binding, phase, correlation, item, target):
    facts = [task["task_id"], task["creation"]["runtime_seal"], task["spec_sha256"], task["run_id"],
             binding["revision"], task["stop_epoch"], item["generation"], phase, correlation, target]
    return "handoff-" + _sha(canonical_json(facts))[:32]


def render_notice(task, binding, phase, correlation, item, target):
    ref = handoff_id(task, binding, phase, correlation, item, target)
    command = shlex.join(private_runtime_command("read-handoff", ref))
    return f"[ato handoff {ref}]\n{command}"


def render_local_completion(task, binding, phase, correlation, item, target):
    ref = handoff_id(task, binding, phase, correlation, item, target)
    task_id = _identifier(task["task_id"], "task")
    return (f"[ato handoff {ref}]\n"
            f"ATO 任务 {task_id} 已完成。请在当前对话简短汇报任务结果一次，"
            f"可用 ato status {task_id} 查看产物和检查结果。")


def read_handoff(host, proof, ref):
    _identifier(ref, "handoff")
    actor = host.store._principal(proof)
    service = host.interaction
    for task_id in service._task_ids():
        try:
            if host.store._visible_scope(task_id, actor) is None:
                continue
            task, _, binding = service._task(task_id)
            candidates = delivery_candidates(service, task)
        except TaskError as exc:
            if exc.code in {"STATE_CORRUPT", "SPEC_CHANGED", "BUSY", "CHECKS_REQUIRED", "PRODUCT_CHANGED"}:
                continue
            raise
        for phase, correlation in candidates:
            try:
                _, item, notice = service._delivery_item(task, host.store.load_spec(task_id), correlation, phase)
                target = binding["controller_ref" if phase == "answer" else "interaction_ref"]
                if actor.session.reference != target or ref != handoff_id(
                        task, binding, phase, correlation, item, target):
                    continue
                _require(notice and not notice["superseded"] and notice["binding_revision"] == binding["revision"]
                         and (notice["body"] == render_notice(task, binding, phase, correlation, item, target)
                              or _valid_notice_body(notice["body"], ref)),
                         "STALE_REQUEST")
                _require(task["stop_epoch"] == notice["stop_epoch"] and task["run_id"] == notice["run_id"]
                         and item["generation"] == notice["generation"], "STALE_REQUEST")
                scope = service._scope(task_id, actor)
                if phase == "answer":
                    return service.read_answer(proof, correlation)
                if phase == "question":
                    result = service.read(proof, item["request_id"])
                else:
                    _require(service._allowed(actor, scope, task, binding, "read", item, item["generation"]),
                             "FORBIDDEN")
                    status = service.task_status(proof, task_id)
                    if phase == "completion":
                        completed = service.completion_item(task, status, item["generation"])
                        _require(completed and completed[0] == correlation, "STALE_REQUEST")
                        result = {key: status[key] for key in ("task_id", "status", "product", "checks", "review")}
                    else:
                        failure = service.failure_item(task, status, item["generation"])
                        _require(failure and failure[0] == correlation, "STALE_REQUEST")
                        result = {key: status[key] for key in ("task_id", "status", "reason", "next_step")}
                latest, _, current = service._task(task_id)
                _require(latest["revision"] == task["revision"] and current == binding, "STALE_REQUEST")
                return result
            except TaskError as exc:
                if exc.code not in {"STALE_REQUEST", "CHECKS_REQUIRED", "PRODUCT_CHANGED"}:
                    raise
    raise TaskError("NOT_FOUND")


def delivery_candidates(service, task):
    if task["creation"]["phase"] != "ready":
        return []
    candidates = _current_delivery_candidates(service, task)
    selected = set(candidates)
    for phase, correlation, notice in service._notice_files(task):
        if phase in {"completion", "failure"} and (phase, correlation) not in selected:
            message = service._message(task, notice)
            if message and message["state"] == "queued":
                candidates.append((phase, correlation))
    return candidates


def _current_delivery_candidates(service, task):
    candidates = []
    for row in service._rows(task):
        ref = row["request"]["request_id"]
        if not row["answer"] or "question" in row["outbox"]:
            candidates.append(("question", ref))
        if row["answer"] and (not row["consumption"] or "answer" in row["outbox"]):
            candidates.append(("answer", ref))
    if published := service._completion_reservation(task):
        correlation, _, notice = published
        message = service._message(task, notice)
        if message and message["state"] != "queued":
            candidates.append(("completion", correlation))
            if notice["run_id"] == task["run_id"]:
                return candidates
            _, receipts = service.store._run(task, task["run_id"])
            if receipts and receipts[-1][1]["outcome"] in DELIVERY_OUTCOMES:
                return candidates
    if not task["run_id"]:
        return candidates
    _, receipts = service.store._run(task, task["run_id"])
    if not receipts or receipts[-1][1]["phase"] != "settled" or receipts[-1][1]["outcome"] not in (
            DELIVERY_OUTCOMES | {"blocked", "failed"}):
        return candidates
    status, generation, _ = service._view(task, service.store.load_spec(task["task_id"]))
    if service.render_notice is not None and (failed := service.failure_item(task, status, generation)):
        candidates.append(("failure", failed[0]))
    if completed := service.completion_item(task, status, generation):
        candidates.append(("completion", completed[0]))
    return candidates
