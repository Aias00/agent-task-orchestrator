---
name: ato
description: Submit, observe, stop and resume durable agent tasks. Route human decisions and report verified task completion.
---

# ATO

Use `ato --help` to find the available commands. Use one Task ID for all task
operations.

1. Generate the task spec from the user's instructions and preview it using
   `ato project validate --repo PATH --spec FILE`. Include the task's command
   checks. Manual criteria are optional. Use the installed `ato` as the
   account that owns the private ATO state for local tasks. Submit using
   `ato run --repo PATH --spec FILE` and retain the Task ID. When submitting from AGY, prefix that command
   with `ATO_SUBMISSION_KIND=agy` so the worker uses its default Codex
   configuration even when AGY inherited a Codex thread ID. Use the Task ID
   for status updates in AGY.
2. Read `ato status TASK_ID` to check execution, independent checks, pending
   decisions and the next action. Use `ato list --repo PATH` for the project.
3. Use `ato inbox` from the verified interaction endpoint. Relay the actual
   request to the user. The enrolled user records their decision using
   `ato answer REQUEST_ID --answer TEXT`.
4. Report completion and the product location once `ato status` shows
   `completed`. The Observer can notify the verified submitting Codex session
   on the first successful completion. Record optional approval only when the
   user explicitly wants a receipt. Use `ato accept TASK_ID` for a local task or
   `ato accept TASK_ID --evidence FILE` for a scoped task.
5. Use `ato stop TASK_ID` for an authorized STOP. Verify cleanup in status.
   Use `ato resume TASK_ID` only for an explicit authorized continuation.
   An unresolved delivery or execution result requires reconciliation before
   another attempt.
6. Use `ato project cleanup TASK_ID --confirm` for authorized removal of a
   verified idle worktree. Completed Tasks retain their verified product on
   the task branch. Stopped Tasks require clean worktrees.

A technical review, message delivery, or handoff does not record user approval.
For scoped operations, select the enrolled actor's proof via `ATO_ACTOR_FILE`.
Keep the file outside the specification, worktree and conversation.
Use `ato agent binding TASK_ID`
to check the current controller, interaction endpoint, reviewer and Run owner.

## Deletion, refactoring and merge review

Specify both deletion targets and retained boundaries. Encode required behavior in
frozen acceptance checks. For explicit path protection, use optional spec
`guardrails.protected_paths` or `guardrails.forbid_delete_paths`.

Before reporting completion or merging, inspect `product.diff_summary` or run
`ato project diff TASK_ID --stat`. Check `files_deleted`, `tests_deleted` and
`tests_modified` against the requested scope. Inspect the full diff where needed.
Disclose any merge decision that changes the verified product, including files
restored or dropped, and rerun the frozen checks on the merged result.
