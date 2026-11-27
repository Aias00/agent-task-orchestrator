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
