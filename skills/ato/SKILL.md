---
name: ato
description: Submit, observe, stop and resume durable agent tasks with an enrolled ATO actor. Use ATO to route human decisions and verify an exact product for acceptance.
---

# ATO

Use `ato --help` to find the available commands. Use one Task ID for all task
operations.

1. With an approved specification and a verified submission grant, preview it
   using `ato project validate --repo PATH --spec FILE`. Submit using
   `ato run --repo PATH --spec FILE` and retain the Task ID.
2. Read `ato status TASK_ID` to check execution, independent checks, pending
   decisions and the next action. Use `ato list --repo PATH` for the project.
3. Use `ato inbox` from the verified interaction endpoint. Relay the actual
   request to the user. The enrolled user records their decision using
   `ato answer REQUEST_ID --answer TEXT`.
4. After the independent checks and any configured technical review pass, show
   the user the exact product and check evidence. The enrolled user records
   acceptance using `ato accept TASK_ID --evidence FILE`.
5. Use `ato stop TASK_ID` for an authorized STOP. Verify cleanup in status.
   Use `ato resume TASK_ID` only for an explicit authorized continuation.
   An unresolved delivery or execution result requires reconciliation before
   another attempt.

A technical review, message delivery, or handoff does not record user approval.
Use the host's enrolled credentials and permissions without copying them into
the specification, worktree, or conversation. Use `ato agent binding TASK_ID`
to check the current controller, interaction endpoint, reviewer and Run owner.
