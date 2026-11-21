# Final completion notification policy, 2026-11-19

The hub user explicitly requested a single brief notice for an overall completed task. This supersedes the previous quiet-completion preference only. Ordinary progress, stages, handoffs and repeated reminders remain quiet. Drover project mute takes priority. Unattended failure and user questions retain their existing policies.

Only completions finished after `completion_since` qualify. Original command/manual evidence, all final criteria, exact workflow/origin/generation and stopped-worker state must verify. No historical backfill, fake test notifications, automatic acceptance or task execution is introduced. Durable existing outbox/mailbox claims deduplicate delivery. Unknown submission is retained without replay; queued is not read.

The immutable ctask launcher, installed queue guard, skill and existing task workers remain unchanged. This release replaces only the unique controller. The completion-only native adapter validates the exact canonical durable claim before using the installed native queue. It does not enable arbitrary messages.

## Operator entry and restart

Installed entry: `~/.local/bin/ctask-notify`.

Restart this controller with:

```
ctask-notify control start --source 'Existing approved final-completion-once notification policy, 2026-11-19' --max-reminders 0
ctask-notify control status
ctask-notify notification-policy status
```

An already alive controller is not replaced by `start`. Verify the process argv points at this release and the heartbeat advances. The formal `ctask control start` still points to the older task runtime and must not cold-start the notification controller. Reboot has no automatic start guarantee. Never run duplicate controllers or change hub identity to repair notifications.

## Future task-runtime integration

The pending execution-efficiency candidate is intentionally unchanged, preserving its manifest and native qualification binding. The surgical `completion.patch` and activation record are retained at `OPERATOR_EVIDENCE/ato-completion-policy-20261119T225017Z`. Keep this controller release when deploying that task candidate, or integrate the patch and qualify the new combined source before replacing the controller. Do not overwrite unrelated candidate changes or frozen acceptance/history.

## Verification and rollback

591 tests passed, zero failures/errors/skips, including real controller/mailbox fixture routing, manual evidence, unknown-delivery, mute, history and dedup cases. No real user test notification sent. Real activation verifies exact source, single process, advancing heartbeat, policy and unchanged task authority.

Rollback: stop only this exact controller gracefully; restore the backed-up notification policy; start the predecessor controller from the recorded release. Never restore a task/database snapshot or clear existing send claims. Re-enabling completion later establishes a new cutoff rather than replaying old results.
