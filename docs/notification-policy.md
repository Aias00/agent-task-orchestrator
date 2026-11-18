# Hub notifications only when user intervention is required

`ctask notification-policy set --mode user_input_only --source 'actual user instruction'`
configures the currently bound hub. `ctask notification-policy status` reads it.
Only that hub conversation can configure it; a different hub does not inherit
the binding. `all` restores the previous delivery policy without changing tasks.

With `user_input_only`, ordinary progress, watchdog/supervisor status, budget
status, host handoffs, origin status replies and software completion records
remain available in `status`, `inbox` and `followups`. They do not queue messages
into the user's conversation. Their history is retained; silence is never an
answer, presentation, acceptance, permission grant, recovery or completion.

Only a current canonical pending question created through `ask` or recovery
`needs_input` can notify the hub. Its normal bounded reminders and actual
answer/accept-answer path remain intact. Automatic supervision alerts are
recorded separately from real user questions. An origin must use `ask` if it
needs a specific human decision, information, authorization or acceptance.

Normal operational delivery to the original task session is unchanged.
Origin replies are stored with `delivery.state=recorded_only`, not a fabricated
queue receipt. Existing sent/ambiguous receipts are retained without replay.
Previously queued messages are not automatically removed by this policy.
Inspect supported queue-list/delete operations if old pending notifications
must be removed; never delete a user's messages or valid questions in bulk.
Already displayed messages remain in conversation history. Non-intervention
arrivals should be processed silently, without claiming they were presented.
User-initiated status queries continue to receive answers.

## Direct queue ingress

An origin calling native `codex queue` directly bypasses the controller. It
must store ordinary progress in its own task evidence instead of sending it
to the hub. Use `ctask ask` for actual human decisions or acceptance.

`hub_queue_gate.py` is an optional local Codex launcher guard. It only gates
queue submissions to the currently bound `user_input_only` hub; other commands
and recipients execute the native CLI with unchanged arguments. Valid pending
ctask question IDs are checked against canonical state, contract, generation,
STOP and sender before delivery. Operational status only creates a bounded
0600 metadata audit; it never emits a fabricated `Queued message` receipt.

The regular installer does not replace a Codex launcher. To opt in, first
verify and retain the original launcher target, ensure Python 3.12 is available,
then configure a wrapper that invokes this script with the original argv.
Set `CTASK_NATIVE_CODEX` to the verified native executable if it is not the
standalone package default. It must not point back to the guard or wrapper.
Keep a reversible backup of the launcher; no native binary is modified.
Other app-server clients or direct native paths bypass a launcher guard and
must follow the same source-session policy. Recheck after launcher upgrades.

This policy prevents chat interruptions; it does not fix the separate logical
host-handoff identity/generation or successor-linking issues. It does not add
automatic recovery, a new observer window or business execution authority.
