"""Sequential, fail-closed joint review and bounded public evidence views.

The host aggregate deliberately is not a single-reviewer wire verdict: it can
contain all 1024 findings and retains every member's source identity.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import unicodedata

from ato import config
from ato.adapters.base import ReviewResult, has_secret, secret_values
from ato.spec import (FINDING_FIELDS, TaskError, canonical_json, require,
                      sha256_bytes, validate_review_verdict)
from ato.worktree import shared_links_for, tree_fingerprint

# Fail closed on capacity instead of placing a runtime file outside a worker's
# readable sandbox or silently dropping blockers. The entire feedback is inline.
MAX_FEEDBACK_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 128 * 1024


def is_panel(cfg) -> bool:
    return isinstance(cfg, dict) and "members" in cfg


def uses_acpx(cfg) -> bool:
    return bool(cfg and any(m["agent"]["kind"] == "codex_acpx" for m in cfg.get("members", [cfg])))


def digest(cfg: dict) -> str:
    return sha256_bytes(canonical_json(cfg))


def effective_member(panel: dict, member: dict) -> dict:
    return {**member, "checklist": panel["checklist"] + member["checklist"],
            "design_docs": panel["design_docs"], "max_review_rounds": panel["max_review_rounds"]}


def aggregate(members: list[dict]) -> dict:
    require(2 <= len(members) <= 8 and all(m["state"] == "completed" for m in members), "INVALID_EVIDENCE")
    findings, by_key, verdicts = [], {}, []
    checked, risks = [], []
    for member in members:
        verdict = validate_review_verdict(member["verdict"])
        verdicts.append(verdict["verdict"])
        for finding in verdict["findings"]:
            # Paths and code indentation are significant. Normalize only display
            # title spacing and textual Unicode/newlines, never collapse code spaces.
            key = {k: finding[k] for k in FINDING_FIELDS if k != "id"}
            key["title"] = " ".join(unicodedata.normalize("NFC", finding["title"]).split())
            for field in ("evidence", "required_fix"):
                key[field] = unicodedata.normalize("NFC", finding[field]).replace("\r\n", "\n")
            fingerprint = digest(key)
            source = {"member_id": member["member_id"], "finding_id": finding["id"]}
            if fingerprint not in by_key:
                entry = {**finding, "id": "finding-" + fingerprint, "sources": []}
                by_key[fingerprint] = entry
                findings.append(entry)
            by_key[fingerprint]["sources"].append(source)
        checked.extend({"member_id": member["member_id"], "text": text} for text in verdict["checked_items"])
        risks.extend({"member_id": member["member_id"], "text": text} for text in verdict["residual_risks"])
    decision = "blocked" if "blocked" in verdicts else "request_changes" if "request_changes" in verdicts else "approve"
    return {"type": "joint_review", "policy": "all", "verdict": decision,
            "summary": f"All {len(members)} members completed; aggregate: {decision}.",
            "findings": findings, "checked_items": checked, "residual_risks": risks,
            "source_finding_count": sum(len(m["verdict"]["findings"]) for m in members)}


def run_panel(task, spec, run_id, *, round_num, store, product_id,
              previous_findings=None, stop_check=None, on_child=None, runner_override=None) -> ReviewResult:
    from ato.reviewer import run_review_turn

    require(store is not None and product_id is not None, "INVALID_EVIDENCE")
    panel = task.get("reviewer") or spec["reviewer"]
    wt, ignored = Path(task["worktree_path"]), shared_links_for(task)
    fingerprint = tree_fingerprint(wt, ignored=ignored)
    require(product_id == "product-" + fingerprint, "PRODUCT_CHANGED", task["id"])
    prior = store.review_group(task["id"])
    if not previous_findings and prior and prior["state"] == "completed" and prior["config_sha256"] == digest(panel):
        previous_findings = [f for f in prior["verdict"]["findings"] if f["severity"] == "blocker"]
    sessions = {}
    if prior and prior["state"] == "completed" and prior["run_id"] == run_id and prior["config_sha256"] == digest(panel):
        sessions = {m["member_id"]: m["session_id"] for m in prior["members"]}
    members = [effective_member(panel, m) for m in panel["members"]]
    kinds = {m["id"]: m["agent"]["kind"] for m in members}
    group_id = store.begin_review_group(task["id"], run_id, round_num, product_id=product_id,
                                       product_fingerprint=fingerprint, config_sha256=digest(panel), members=members)

    def stopped():
        return bool((stop_check and stop_check()) or not store.review_group_owned(task["id"], group_id))

    def abort(code, detail="", *, member_id=None, result=None):
        store.interrupt_review_group(task["id"], group_id, code, member_id=member_id, result=result)
        return ReviewResult(False, code, detail=detail, metadata={"group_id": group_id})

    try:
        for member in members:
            mid = member["id"]
            if stopped():
                return abort("STOPPED")
            if tree_fingerprint(wt, ignored=ignored) != fingerprint:
                return abort("MUTATION_VIOLATION")
            if not store.start_review_member(task["id"], group_id, mid):
                return abort("STOPPED")
            result = run_review_turn({**task, "reviewer": member}, spec, run_id, round_num=round_num,
                                     member_id=mid, resume_session_id=sessions.get(mid),
                                     previous_findings=previous_findings, stop_check=stopped,
                                     on_child=on_child, runner_override=runner_override, store=store)
            # Never accept an ACP receipt or ordinary approval after STOP/owner loss.
            if stopped():
                return abort("STOPPED", member_id=mid, result=result)
            if tree_fingerprint(wt, ignored=ignored) != fingerprint:
                result = replace(result, ok=False, code="MUTATION_VIOLATION")
            pending = store.acpx_review(task["id"], unresolved=True)
            if result.ok and pending:
                from ato.adapters.acpx import recovery_blocker
                result = replace(result, ok=False, code=recovery_blocker(pending) or "CLEANUP_BLOCKED")
            if result.ok:
                try:
                    validate_review_verdict(result.verdict)
                except TaskError:
                    result = replace(result, ok=False, code="INVALID_PROTOCOL", verdict=None)
                if has_secret(result.verdict, secret_values()):
                    result = replace(result, ok=False, code="SECRET_IN_REVIEW", verdict=None)
                if result.session_id and any(sid == result.session_id for other, sid in sessions.items()
                                             if other != mid and kinds[other] == kinds[mid]):
                    result = replace(result, ok=False, code="SESSION_MISMATCH")
            if not store.save_review_member(task["id"], group_id, mid, result):
                return abort("STOPPED")
            if not result.ok:
                return abort(result.code, result.detail or f"Member {mid} failed: {result.code}.")
            sessions[mid] = result.session_id
        if stopped():
            return abort("STOPPED")
        if tree_fingerprint(wt, ignored=ignored) != fingerprint:
            return abort("MUTATION_VIOLATION")
        verdict = store.finish_review_group(task["id"], group_id)
        if verdict is None:
            return abort("STOPPED")
        return ReviewResult(True, "OK", verdict=verdict, metadata={"group_id": group_id})
    except Exception as exc:
        code = exc.code if isinstance(exc, TaskError) else "REVIEW_EXECUTION_ERROR"
        return abort(code)


def approved_group(store, task_id: str, product_id: str, cfg: dict) -> bool:
    group = store.review_group(task_id)
    return bool(group and group["state"] == "completed" and group["product_id"] == product_id
                and group["config_sha256"] == digest(cfg) and group["verdict"]["verdict"] == "approve"
                and len(group["members"]) == len(cfg["members"])
                and all(m["state"] == "completed" and m["verdict"]["verdict"] == "approve" for m in group["members"])
                and store.get_run(group["run_id"])["outcome"] == "completed")


def public(value, task: dict):
    """Strip secrets and host paths without truncating individual evidence fields."""
    if isinstance(value, dict):
        return {k: public(v, task) for k, v in value.items()}
    if isinstance(value, list):
        return [public(v, task) for v in value]
    if not isinstance(value, str):
        return value
    for secret in sorted((*secret_values(), *task.get("_review_sessions", [])), key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    for path in sorted({str(config.runtime_dir()), str(config.ato_home()), str(config.codex_home()),
                        task.get("worktree_path") or "", task.get("repo_root") or "", str(Path.home())}, key=len, reverse=True):
        if path:
            value = value.replace(path, "[host-path]")
    from ato.adapters.acpx import CREDENTIAL_PATTERN
    value = CREDENTIAL_PATTERN.sub("[redacted]", value)
    value = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", value)
    return value


def member_summary(member: dict) -> dict:
    verdict = member["verdict"]
    return {"member_id": member["member_id"], "position": member["position"], "state": member["state"],
            "config_sha256": member["config_sha256"], "agent": member["config"]["agent"],
            "timeout_seconds": member["config"]["timeout_seconds"], "error": member["error"],
            "verdict": verdict["verdict"] if verdict and member["state"] == "completed" else None,
            "reported_verdict": verdict["verdict"] if verdict else None,
            "finding_count": len(verdict["findings"]) if verdict else 0,
            "started_at": member["started_at"], "finished_at": member["finished_at"],
            **({"acpx": member["acpx"]} if "acpx" in member else {})}


def group_summary(group: dict) -> dict:
    verdict = group["verdict"]
    return {"group_id": group["id"], "run_id": group["run_id"], "round": group["round"],
            "policy": group["policy"], "member_count": len(group["members"]), "state": group["state"], "complete": group["state"] == "completed",
            "product_id": group["product_id"], "config_sha256": group["config_sha256"], "error": group["error"],
            "verdict": verdict["verdict"] if verdict else None,
            "finding_count": len(verdict["findings"]) if verdict else sum(
                len(m["verdict"]["findings"]) for m in group["members"] if m["verdict"]),
            "source_finding_count": verdict["source_finding_count"] if verdict else None,
            "current_member": next((m["member_id"] for m in group["members"] if m["state"] == "running"), None),
            "members": [member_summary(m) for m in group["members"]],
            "created_at": group["created_at"], "finished_at": group["finished_at"]}


def observed_group(store, group: dict) -> dict:
    if group["state"] == "running":
        from ato.executor import _is_run_live
        if not _is_run_live(store.get_run(group["run_id"])):
            return {**group, "state": "incomplete", "error": group["error"] or "OWNER_NOT_LIVE"}
    return group


def status(store, task: dict) -> dict:
    cfg = task.get("reviewer") or task["spec"].get("reviewer")
    group = store.review_group(task["id"])
    view = group_summary(group) if group else {"state": "not_started", "complete": False, "verdict": None}
    # An old aggregate remains historical evidence, never approval of a new config.
    view.update(policy=cfg["policy"], execution="sequential", history_operation="ato_reviews",
                config_matches=bool(group and group["config_sha256"] == digest(cfg)),
                configured_members=[{"id": m["id"], "agent": m["agent"], "session_mode": m["session_mode"],
                                     "timeout_seconds": m["timeout_seconds"],
                                     "timeout_source": task.get("reviewer_sources", {}).get(m["id"], "unknown"),
                                     "checklist_count": len(m["checklist"])} for m in cfg["members"]])
    if group and (not view["config_matches"] or (group["run_id"] != task["active_run_id"] and task["status"] != "completed")):
        view.update(historical_verdict=view["verdict"], verdict=None, complete=False,
                    state="config_changed" if not view["config_matches"] else "not_started_for_run")
    view["run_completed"] = bool(group and store.get_run(group["run_id"])["outcome"] == "completed")
    view["approval_usable"] = bool(view["run_completed"] and view["complete"] and view["config_matches"]
                                  and view["verdict"] == "approve" and task["status"] == "completed"
                                  and (task.get("product") or {}).get("product_id") == group["product_id"])
    if group and group["state"] == "running":
        from ato.executor import _is_run_live
        if not _is_run_live(store.get_run(group["run_id"])):
            view.update(state="incomplete", complete=False, verdict=None, error=group["error"] or "OWNER_NOT_LIVE")
    return public(view, {**task, "_review_sessions": [m["session_id"] for m in group["members"] if m["session_id"]]} if group else task)


def reviews(store, task_id: str, *, group_id=None, member_id=None, review_id=None, section="groups", cursor=0, limit=20) -> dict:
    """Stable ID cursor for groups; immutable-array offset for selected evidence."""
    task = store.get_task(task_id)
    require(type(cursor) is int and 0 <= cursor <= 2**63 - 1 and type(limit) is int and 1 <= limit <= 20, "USAGE")
    require(section in ("groups", "members", "findings", "checked_items", "residual_risks", "legacy"), "USAGE")
    require(group_id is None or (type(group_id) is int and 1 <= group_id <= 2**63 - 1), "USAGE")
    if section == "legacy" or review_id is not None:
        require(group_id is None and member_id is None, "USAGE")
        return legacy_reviews(store, task, review_id=review_id, section=section, cursor=cursor, limit=limit)
    if group_id is None:
        require(section == "groups" and member_id is None, "USAGE")
        ids, total = store.review_group_ids(task_id, cursor=cursor, limit=limit + 1)
        rows = [group_summary(observed_group(store, store.review_group(task_id, gid))) for gid in ids[:limit]]
        next_cursor = ids[limit - 1] if len(ids) > limit else None
        return public({"task_id": task_id, "section": section, "items": rows, "total": total,
                       "next_cursor": next_cursor}, task)
    group = store.review_group(task_id, group_id)
    require(group is not None, "NOT_FOUND")
    group = observed_group(store, group)
    task = {**task, "_review_sessions": [m["session_id"] for m in group["members"] if m["session_id"]]}
    member = next((m for m in group["members"] if m["member_id"] == member_id), None)
    require(member_id is None or member is not None, "NOT_FOUND")
    require(member_id is None or section != "groups", "USAGE")
    if section == "groups":
        items = [group_summary(group)]
        metadata = {}
    elif section == "members":
        selected = [member] if member else group["members"]
        items = [{**member_summary(m), "config": m["config"],
                  "summary": (m["verdict"] or {}).get("summary"),
                  "checked_item_count": len((m["verdict"] or {}).get("checked_items", [])),
                  "residual_risk_count": len((m["verdict"] or {}).get("residual_risks", []))} for m in selected]
        metadata = {}
    else:
        verdict = (member or group)["verdict"]
        require(verdict is not None, "INVALID_EVIDENCE", task_id,
                detail="No complete aggregate; select a member to read partial evidence.")
        items = verdict[section]
        metadata = {"verdict": verdict["verdict"], "summary": verdict["summary"]}
    rows = items[cursor:cursor + limit]
    next_cursor = cursor + len(rows) if cursor + len(rows) < len(items) else None
    return public({"task_id": task_id, "group_id": group_id, "member_id": member_id, "section": section,
                   "items": rows, "total": len(items), "next_cursor": next_cursor, **metadata}, task)


def legacy_reviews(store, task, *, review_id, section, cursor, limit):
    """The old reviews table stays available without exposing session IDs."""
    require(review_id is None or (type(review_id) is int and 1 <= review_id <= 2**63 - 1), "USAGE")
    with store.tx(write=False) as con:
        if review_id is None:
            rows = con.execute("""SELECT * FROM reviews WHERE task_id = ? AND reviewer_kind != 'panel'
                                  AND id > ? ORDER BY id LIMIT ?""", (task["id"], cursor, limit + 1)).fetchall()
            total = con.execute("SELECT COUNT(*) FROM reviews WHERE task_id = ? AND reviewer_kind != 'panel'",
                                (task["id"],)).fetchone()[0]
            items = [{"review_id": r["id"], "run_id": r["run_id"], "round": r["round"],
                      "reviewer_kind": r["reviewer_kind"], "verdict": r["verdict"],
                      "summary": json.loads(r["verdict_json"])["summary"],
                      "finding_count": len(json.loads(r["verdict_json"])["findings"])} for r in rows[:limit]]
            next_cursor = rows[limit - 1]["id"] if len(rows) > limit else None
        else:
            require(section in ("findings", "checked_items", "residual_risks"), "USAGE")
            row = con.execute("SELECT * FROM reviews WHERE task_id = ? AND id = ? AND reviewer_kind != 'panel'",
                              (task["id"], review_id)).fetchone()
            require(row is not None, "NOT_FOUND")
            data = json.loads(row["verdict_json"])[section]
            items, total = data[cursor:cursor + limit], len(data)
            next_cursor = cursor + len(items) if cursor + len(items) < total else None
    return public({"task_id": task["id"], "review_id": review_id, "section": section,
                   "items": items, "total": total, "next_cursor": next_cursor}, task)
