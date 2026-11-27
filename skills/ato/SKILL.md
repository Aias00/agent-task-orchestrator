---
name: ato
description: Submit, observe, stop and resume durable agent tasks in isolated Git worktrees. Route human questions and report verified task completion.
---

# ATO

Use `ato --help` to inspect available commands. Use one Task ID for all operations.

1. Write a task spec and validate it with `ato project validate --repo PATH --spec FILE`.
   Include command checks in `worker_contract.acceptance`; manual criteria and `guardrails`
   (`protected_paths`, `forbid_delete_paths`) are optional.
   Submit using `ato run --repo PATH --spec FILE` (add `--foreground` to block until settled).
   When submitting from AGY, prefix with `ATO_SUBMISSION_KIND=agy` so Codex workers keep
   their default `workspace-write` sandbox without inheriting ambient `CODEX_THREAD_ID`.
2. Check progress with `ato status TASK_ID` and `ato list --repo PATH`.
   Read `completion_notification` (AGY reports `unsupported`; unverified Codex reports `unbound`)
   and `watchdog` (`state`, `sampled_at`, `alerts` — alerts are advisory).
3. If the task pauses for user input, run `ato inbox`, relay the question to the user,
   record their reply via `ato answer REQUEST_ID --answer TEXT`, then run `ato resume TASK_ID`.
   To change the worker timeout for only that new run, use
   `ato resume TASK_ID --timeout-seconds SECONDS` (base-10 digits, 5–7200).
   A later plain resume returns to the spec/default timeout; Task ID, worktree,
   frozen spec digest, and acceptance criteria stay unchanged.
4. When `ato status TASK_ID` shows `completed`, inspect `reason` and `checks[*].state`
   before reporting success: failed independent checks appear as `reason="CHECKS_FAILED"`
   even when `status` is `completed`. Also inspect `product.diff_summary` or run
   `ato project diff TASK_ID --stat` to verify `files_deleted`, `tests_deleted`, and
   `tests_modified`. Record optional approval with `ato accept TASK_ID` only if requested.
5. Use `ato stop TASK_ID` to stop a running task, `ato project recheck TASK_ID` to re-run
   frozen checks/review, and `ato project cleanup TASK_ID --confirm` to commit verified
   changes to the task branch and remove the worktree.

Validate previews show `timeout_seconds` and `timeout_source` (`default` or `spec`),
plus reviewer timeout/source when configured. Worker/reviewer defaults are 600/300
seconds. Run, resume, and status receipts show worker timeout/source in `execution`
and `run_history`; `project trace` submission/resume events record them too.
Single-run overrides report `resume_override`; old unknown sources report `legacy`.

Review defaults to `max_review_rounds=10` (integer 1..50). Normal review rework
has its own counter and does not consume the worker execution-failure budget
(`max_attempts=3` by default). Guardrail/check repair uses a separate counter with
the same cap, reset when verification passes. Task reviewer overrides also control
the effective review limit; reaching it blocks with `REVIEW_CHANGES_REQUESTED`.

Optional `worker_contract.max_no_progress_rounds` defaults to 2 (integer 1..10).
Its omission preserves canonical spec/hash compatibility. The first issue sets a
baseline; unchanged failures or unresolved review blockers increase the streak.
At streak 1 the worker gets a repeated-issue warning; at the limit the executor
pauses with `REPEATED_NO_PROGRESS`, records the issue, and queues a failure notice.
Inspect `stage.category`, the separate failure/rework counters, `progress_made`,
`no_progress_streak`, and `repeated_items` before deciding how to resume.

Watchdog distinguishes `EXECUTION_FAILURE_RETRY` from `REPEATED_NO_PROGRESS`;
healthy `review_rework` produces no failure alerts. Blocked alerts include their
category. Terminal tasks with a matching successful sample and no active/pending
alerts report Watchdog `state="settled"`, remaining settled after 180 seconds.
