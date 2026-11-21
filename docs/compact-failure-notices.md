# Compact failure notices

The failure-alert path used the old full follow-up template even after host
handoffs became compact. A failure queued before origin work began could remain
visible as a long native user turn after its controller action had become stale.
An empty assistant response cannot hide that already delivered native message.

New once-only failure wakeups preserve their correlation marker and use the
existing `ato-read-handoff ID` helper. Full material is retained in a private,
immutable 0600 file, separate from the displayed message (under 300 UTF-8 bytes).
The controller uses `coordination.reader_command()` for installed/custom helper
paths and shell quoting. That public function and direct failure readers share
the same standard-library configuration primitive in `ato_config`, keeping the
passive ledger and notification modules free of new import cycles.
The helper validates the current canonical action, hub actor, exact generation,
failure evidence hashes, STOP, completion and handler classification. A stale or
handled failure returns only `end_silently`; reading does not mark presentation,
approval or recovery. Dispatch guards also recheck failures before both native
send barriers; acquisition of a handler cancels an unsubmitted notice.

This does not retract existing terminal history, suppress genuine unattended
failures, disable task continuation, change human questions or final-completion
policy, or establish that an active origin turn owns any particular workflow.
No workflow state, permissions, attempts, STOP or unknown deliveries are reset.
Qualification uses isolated synthetic sessions and queue mocks, not live user
questions or external model calls.
