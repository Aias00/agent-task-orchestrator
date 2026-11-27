"""Multi-agent read-only reviewer orchestration with worktree mutation tripwire."""
from __future__ import annotations

import json
from pathlib import Path

from ato import config
from ato.adapters.base import ReviewRequest, ReviewResult, get_reviewer_runner
from ato.product import collect_diff
from ato.worktree import shared_links_for, tree_fingerprint


def build_review_prompt(task: dict, spec: dict, *, round_num: int, previous_findings: list[dict] | None = None) -> str:
    contract = spec["worker_contract"]
    reviewer = spec["reviewer"]
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
    lines.append("Return ONLY a strict JSON object matching REVIEW_VERDICT_SCHEMA.")
    return "\n\n".join(lines)


def run_review_turn(task: dict, spec: dict, run_id: str, *, round_num: int = 1,
                    resume_session_id: str | None = None,
                    previous_findings: list[dict] | None = None,
                    stop_check=None, runner_override=None) -> ReviewResult:
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
        resume_session_id=resume_session_id,
        timeout_seconds=reviewer_cfg.get("timeout_seconds", 300),
        stop_check=stop_check,
    )
    runner = runner_override or get_reviewer_runner(agent["kind"])
    result = runner(req)

    after_fp = tree_fingerprint(wt, ignored=ignored)
    if after_fp != before_fp:
        return ReviewResult(ok=False, code="MUTATION_VIOLATION", metadata={"before": before_fp, "after": after_fp})
    return result
