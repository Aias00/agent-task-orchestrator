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
Messages already queued before the policy was activated cannot be recalled;
the hub should process non-intervention arrivals silently, without claiming
they were presented. User-initiated status queries continue to receive answers.

This policy prevents chat interruptions; it does not fix the separate logical
host-handoff identity/generation or successor-linking issues. It does not add
automatic recovery, a new observer window or business execution authority.
