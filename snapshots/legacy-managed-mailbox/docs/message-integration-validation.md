# Existing workflow integration stage

This stage connects interaction, recovery, controller and delegation delivery to
`mailbox_routes.py`, `MessageBus` and the transport adapters. It adds managed
Claude/Pi origins, mixed managed/Codex hubs, native-generated questions and bound
acceptance events. Legacy JSON, Codex UUID bindings, cumulative workflow budgets,
recovery counts/deadlines and final acceptance remain independent authority.
No installed runtime, actual hub or native account config was activated/modified.
No scheduler, detached implementation job, commit, push or deployment was started.

## Worker evidence (2026-11-15)

The frozen integration interface passed **18 cases**, without skips:

```sh
OPERATOR_HOME/.local/bin/python3.12 .codex/tasks/agent-neutral-mailbox-preparation/accept.py integration
```

The bus/adapter modules passed **33 cases**. The independently frozen recovery
module passed **15**, controller **16**, and the six remaining deterministic
modules **59** (alerts, interaction, workflow, runner, progress and install):
**90 distinct frozen cases passed**. Interaction was additionally rechecked
against the final host acceptance-event changes (**13 cases**, already included
in those 90). Python compilation and `git diff --check` also passed.

The complete frozen **120-case baseline was attempted unchanged**. Its initial
worker run was not a pass: 11 failures, 13 errors, 94.795 seconds. It exposed
three integration regressions subsequently corrected and covered by the passing
frozen modules: original recovery queue cwd, independently queued notifications
after an uncertain Codex enqueue, and cancellation of previously released native
queue slots. The other worker failures were the known denied Unix fixture socket
binds and nested `sandbox-exec` limitation. Those restricted checks were not
repeated or weakened. The external stable runner must execute its original full
baseline on the host; **no worker full-baseline pass is claimed**.

Full logs, source SHA256 fingerprints and the explicit stage-only result are in
`build/message-transport/integration-validation.json` and its adjacent
`integration-*.log` files. These logs contain isolated fixture paths and synthetic
messages, not real authentication or messages to real sessions. They are source
checkout artifacts, not scheduler ledger changes. The scheduler still owns its
frozen checks, plans, budgets, history and workflow state.

## Behavior exercised

- Both native protocol subprocess fixtures maintain session identity and context
  across two resumed turns, route questions to the hub, receive sourced human
  answers, accept them and project exact correlated results back to the controller.
- Models with tools disabled can produce bounded question/needs-input and
  acceptance events. Actual user answers still require their real hub source;
  model text does not become user permission or extra recovery budget.
- Mixed Claude hub/Pi origin, ordinary Codex hub/native origins, bounded recovery
  and native delegation acceptance use the same mailbox. Acceptance alone does
  not start another workflow or prove execution/whole-goal completion.
- Reopening storage retains replies and outboxes; dispatch-barrier crashes and
  unknown native receipts do not resend. Busy unsent Codex work remains durable
  and can be drained by an explicit foreground pump after ownership is released.
- STOP before dispatch and racing a native reply, stale generation/contract/
  origin, expiry, disabled supervision and explicit endpoint pause fence queued
  work and late receipts. Paused work is neither recovered nor reminded.
- Existing JSON without mailbox IDs remains readable and usable without being
  re-imported as fresh sends. Original origin UUIDs, contract bytes, cumulative
  elapsed time, acceptance and recovery counters/deadlines are preserved.

## Whole-goal continuation

This is a code-ready **assigned-stage** result, not the whole goal. The external
runner must independently execute the frozen integration and baseline commands.
The earlier native host report in `host-validation.json` is historical evidence:
changed bus/adapter inputs require fresh native two-turn local-provider host
qualification before whole-goal acceptance. The worker did not rerun known
socket-denied probes, copy credentials or claim cloud-account validation.

The full contract in `agent-neutral-mailbox-task.md` still requires current full
suite/host qualification, installation and migration/rollback tooling, remaining
README/skill/release documentation and the final installation handoff. No final
criterion, business authorization or budget was removed or relaxed here.
