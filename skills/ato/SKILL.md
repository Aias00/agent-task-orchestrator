---
name: ato
description: Submit, observe, stop and resume durable agent tasks. Use ATO to route human decisions and verify an exact product for acceptance.
---

# ATO

Use `ato --help` to find the available commands. Use one Task ID for all task
operations.

1. Generate the task spec from the user's instructions and preview it using
   `ato project validate --repo PATH --spec FILE`. Include the task's command
   checks. Use the installed `ato`
   under the account that owns the private ATO state. Submit using
   `ato run --repo PATH --spec FILE` and retain the Task ID.
2. Read `ato status TASK_ID` to check execution, independent checks, pending
   decisions and the next action. Use `ato list --repo PATH` for the project.
3. Use `ato inbox` from the verified interaction endpoint. Relay the actual
   request to the user. The enrolled user records their decision using
   `ato answer REQUEST_ID --answer TEXT`.
4. Report completion and the product location once `ato status` shows
   `completed`. Record optional approval only when the user explicitly wants
   a receipt. Use `ato accept TASK_ID` for a local task or
   `ato accept TASK_ID --evidence FILE` for a scoped task.
5. Use `ato stop TASK_ID` for an authorized STOP. Verify cleanup in status.
   Use `ato resume TASK_ID` only for an explicit authorized continuation.
   An unresolved delivery or execution result requires reconciliation before
   another attempt.

A technical review, message delivery, or handoff does not record user approval.
For scoped operations, select the enrolled actor's proof via `ATO_ACTOR_FILE`.
Keep the file outside the specification, worktree and conversation.
Use `ato agent binding TASK_ID`
to check the current controller, interaction endpoint, reviewer and Run owner.
