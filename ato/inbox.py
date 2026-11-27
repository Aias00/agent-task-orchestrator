"""Single-user human inbox (ask/answer) and optional manual product approval."""
from __future__ import annotations

import time

from ato.spec import TaskError, require, require_id, sha256_bytes
from ato.store import Store


def ask_question(store: Store, task_id: str, *, kind: str, question: str, reason: str,
                 continuation: str | None = None, purpose_ref: str | None = None) -> dict:
    task = store.get_task(task_id)
    require(bool(question.strip()) and len(question.encode("utf-8")) <= 8192 and "\x00" not in question, "INVALID_SPEC")
    require(bool(reason.strip()) and len(reason.encode("utf-8")) <= 4096 and "\x00" not in reason, "INVALID_SPEC")
    row = store.create_question(task_id, kind, question, reason, continuation=continuation, purpose_ref=purpose_ref)
    store.append_event(task_id, "question_opened", {"request_id": row["id"], "kind": kind})
    if task["status"] not in ("completed", "stopped"):
        store.update_task(task_id, status="waiting_input", reason="NEEDS_INPUT",
                          next_step=f"Answer {row['id']} via `ato answer {row['id']} --answer ...`")
    if task["origin_kind"] == "codex" and task["origin_session_id"]:
        store.enqueue_notification(
            dedupe_key=f"question:{row['id']}",
            task_id=task_id,
            kind="question",
            target_session=task["origin_session_id"],
            body=f"ATO 任务 {task_id} 需要你的确认 ({row['id']}): {question[:200]}。请用 ato inbox 查看并用 ato answer 回复。",
        )
    return row


def list_inbox(store: Store) -> dict:
    rows = store.list_questions(state="open")
    items = [
        {
            "request_id": r["id"],
            "task_id": r["task_id"],
            "kind": r["kind"],
            "question": r["question"],
            "reason": r["reason"],
            "continuation": r["continuation"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"requests": items, "count": len(items)}


def record_answer(store: Store, request_id: str, answer: str) -> dict:
    require_id(request_id, "request")
    require(isinstance(answer, str) and bool(answer.strip())
            and len(answer.encode("utf-8")) <= 8192 and "\x00" not in answer, "USAGE")
    row = store.answer_question(request_id, answer)
    store.append_event(row["task_id"], "question_answered", {"request_id": request_id})
    task = store.get_task(row["task_id"])
    open_left = store.list_questions(row["task_id"], state="open")
    if task["status"] == "waiting_input" and not open_left:
        store.update_task(row["task_id"], next_step=f"Resume task with `ato resume {row['task_id']}`")
    return {
        "request_id": row["id"],
        "task_id": row["task_id"],
        "state": row["state"],
        "answer": row["answer"],
        "answered_at": row["answered_at"],
    }


def record_acceptance(store: Store, task_id: str, *, note_bytes: bytes | None = None) -> dict:
    """Record optional user approval receipt separately from automated checks and review."""
    task = store.get_task(task_id)
    require(task["status"] == "completed" and task["product"] is not None, "CHECKS_REQUIRED")
    attachment_sha = sha256_bytes(note_bytes) if note_bytes is not None else None
    existing = task["acceptance"]
    if existing is not None:
        if attachment_sha is not None and existing.get("attachment_sha256") != attachment_sha:
            raise TaskError("DECISION_CONFLICT", task_id)
        return existing
    manual_ids = [c["id"] for c in task["spec"]["worker_contract"]["acceptance"] if c["kind"] == "manual"]
    receipt = {
        "disposition": "accepted",
        "criteria": manual_ids,
        "product_id": task["product"]["product_id"],
        "attachment_sha256": attachment_sha,
        "recorded_at": time.time(),
    }
    store.update_task(task_id, acceptance=receipt)
    store.append_event(task_id, "acceptance_recorded", {"product_id": receipt["product_id"]})
    return receipt
