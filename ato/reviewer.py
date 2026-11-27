"""Multi-agent read-only reviewer orchestration with worktree mutation tripwire."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from ato import config
from ato.adapters.base import ReviewRequest, ReviewResult, get_reviewer_runner, safe_review_detail, secret_values
from ato.product import collect_diff
from ato.spec import REVIEW_VERDICT_SCHEMA, SEVERITIES, CATEGORIES
from ato.worktree import shared_links_for, tree_fingerprint


def build_review_prompt(task: dict, spec: dict, *, round_num: int, previous_findings: list[dict] | None = None,
                        format_feedback: str | None = None) -> str:
    contract = spec["worker_contract"]
    reviewer = task.get("reviewer") or spec.get("reviewer") or {}
    diff = collect_diff(task, include_patch=False)
    lines = [
        "You are an independent read-only technical reviewer. Do NOT modify any file.",
        f"Task ID: {task['id']} (Review round {round_num})",
        f"Objective: {contract['objective']}",
        f"Scope: {contract['scope']}",
        "Diff summary: " + json.dumps(diff["summary"], ensure_ascii=False),
    ]
    if reviewer.get("checklist"):
        lines.append("Checklist:\n" + "\n".join(f"- {item}" for item in reviewer["checklist"]))
    if reviewer.get("design_docs"):
        lines.append("Design docs: " + ", ".join(d["path"] for d in reviewer["design_docs"]))
    if previous_findings:
        lines.append("Previous round blocker findings to re-verify:\n" + json.dumps(previous_findings, ensure_ascii=False))
    lines.extend([
        "Return ONLY a strict JSON object matching REVIEW_VERDICT_SCHEMA: " + json.dumps(REVIEW_VERDICT_SCHEMA),
        "Include exactly verdict, summary, findings, checked_items, residual_risks. "
        "verdict must be approve, request_changes, or blocked. "
        "approve forbids blocker findings; request_changes requires at least one blocker. "
        "blocked may have findings with or without blockers. Each array has at most 128 items. "
        "All strings must be nonblank, without NUL, and at most 8192 characters (file: 1024; id: 128).",
        "Each finding must contain exactly id, severity, file, lines, category, title, evidence, required_fix. "
        "Use unique ids matching ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$. "
        "file must be a repository-relative path with no leading slash, backslash or dash, "
        "and no empty, . or .. path segments. lines must be L1 or L1-L2 (positive numbers). "
        "severity must be one of: " + ", ".join(SEVERITIES) + ". category must be one of: " + ", ".join(CATEGORIES) + ".",
    ])
    if format_feedback:
        lines.append("Your previous receipt failed format validation. Correct the JSON receipt using this exact "
                     "validation feedback; keep the review read-only and preserve substantive findings:\n" + format_feedback)
    return "\n\n".join(lines)


def run_review_turn(task: dict, spec: dict, run_id: str, *, round_num: int = 1,
                    resume_session_id: str | None = None,
                    previous_findings: list[dict] | None = None,
                    stop_check=None, on_child: object = None, runner_override=None,
                    max_format_retries: int = 2) -> ReviewResult:
    """Execute one read-only review turn and fail closed if the worktree mutates."""
    reviewer_cfg = task.get("reviewer") or spec.get("reviewer")
    if not reviewer_cfg:
        return ReviewResult(ok=True, code="NO_REVIEWER", verdict=None)

    wt = Path(task["worktree_path"]).resolve()
    ignored = shared_links_for(task)
    before_fp = tree_fingerprint(wt, ignored=ignored)

    runtime = config.ensure_private_dir(config.runtime_dir() / task["id"] / run_id / f"review-{round_num}")
    prompt = build_review_prompt(task, spec, round_num=round_num, previous_findings=previous_findings)
    agent = reviewer_cfg["agent"]
    req = ReviewRequest(
        task_id=task["id"],
        kind=agent["kind"],
        worktree=wt,
        runtime_dir=runtime,
        prompt=prompt,
        model=agent.get("model"),
        effort=agent.get("effort"),
        session_mode=reviewer_cfg.get("session_mode", "fresh_only"),
        resume_session_id=resume_session_id if reviewer_cfg.get("session_mode", "fresh_only") != "fresh_only" else None,
        timeout_seconds=reviewer_cfg.get("timeout_seconds", 300),
        stop_check=stop_check, on_child=on_child,
    )
    runner = runner_override or get_reviewer_runner(agent["kind"])
    for retry in range(max(0, max_format_retries) + 1):
        if stop_check is not None and stop_check():
            return ReviewResult(ok=False, code="STOPPED")
        result = runner(req)
        after_fp = tree_fingerprint(wt, ignored=ignored)
        if after_fp != before_fp:
            return ReviewResult(ok=False, code="MUTATION_VIOLATION", metadata={"before": before_fp, "after": after_fp})
        if stop_check is not None and stop_check():
            return ReviewResult(ok=False, code="STOPPED")
        if result.code != "INVALID_PROTOCOL":
            return result
        detail = safe_review_detail(ValueError(result.detail), secret_values())
        result = replace(result, detail=detail)
        if retry == max(0, max_format_retries):
            return result
        req = replace(
            req,
            prompt=build_review_prompt(task, spec, round_num=round_num,
                                       previous_findings=previous_findings, format_feedback=detail),
            resume_session_id=(result.session_id or req.resume_session_id)
                              if req.session_mode != "fresh_only" else None,
        )
    raise AssertionError("unreachable")
