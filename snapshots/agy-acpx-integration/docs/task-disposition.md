# User closure and failed-task observation

A user's acceptance of a delivered task is distinct from passing automated checks.
`ctask workflow-close WORKFLOW --evidence decision.json --source SOURCE` records an
immutable, evidence-pinned `user-closure.json`. The decision must name the exact
workflow, the current hub and the actual user text. Only the bound origin or hub
may record it, and an active task/stage refuses closure. Existing contract, state,
attempts, criteria, elapsed history and STOP are not modified. `status.delivery`
reports `effective_status=completed`, `completion_basis=user_acceptance`, original
raw status and unresolved automated criteria. Changed state, contract, origin or
evidence invalidates this projection. This is acceptance of current delivery with
recorded residuals, never proof that failed historical checks passed. Closed tasks
cannot be casually resumed or asked stale questions; a real new requirement needs
an explicitly authorized continuation. Controller questions and old handoffs close
as user-accepted while their canonical question/answer history remains unchanged.

New workflows register with the configured controller independently of supervision.
Omitting an explicit origin uses only the current verified runtime session, never
the most recent thread in storage. A missing origin or registration failure gets
an explicit creation warning and registration evidence, rather than a false claim
of observation. `ctask watch WORKFLOW --source SOURCE` explicitly registers an
existing bound task; it grants no retry, execution, permission or repair authority.

`ctask failure-alerts --source SOURCE` enables the user's once-only exception for
newly watched failed tasks with no executor or effective handler. Existing historical
registry rows are not swept into a notification flood. Ordinary statuses, progress,
host handoffs, watchdog messages and acceptance reminders retain their prior quiet
policy. STOP, manual/host waits, active workers, valid user closure/successors and
ambiguous or active recovery delivery cannot produce a missing-handler notice.
The controller pins task/contract/origin/stage evidence and generation, and persists
a stable notice identity. The queue gate accepts it only with a matching canonical
controller action, exact message hash and current dispatch claim. Already presented,
already attempted and unknown sends are never replayed as new reminders. Project
mutes still apply. This is an informational failure escalation, not an approval,
workflow restart or automatic retry. A handler must inspect the original evidence
and use an authorized supported recovery path without resetting history.

The added isolated regression suite covers user closure, invalidation, unchanged
history, active-process refusal, automatic registration, missing-origin visibility,
new-policy cutover, once-only delivery, exact queue ingress, STOP/wait/recovery fences
and retained normal completion behavior. The full existing suite must also pass.
No Git publication, model/provider calls or business service changes are required.
