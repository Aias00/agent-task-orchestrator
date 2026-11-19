# Runtime cutover and rollback

Source publication does not install, restart or migrate a running task service.
An operator needs the user's explicit installation/cutover authority for the
exact target and must validate the selected revision before activation.

Use a durable versioned source directory. Run the full isolated tests and inspect
current launcher, controller identity/heartbeat, live workers, pending questions,
ambiguous outboxes, STOP and config. Preserve the previous runtime/interpreter,
launcher, Codex ingress guard and canonical data. Stop only the approved controller
through `control stop` and verify exit before changing entries or starting its
replacement. Existing immutable workers may finish on their previous runtime;
do not kill them or rewrite their contracts as a deployment shortcut.

The optional `hub_queue_gate.py` is a template, not an automatically installed
Codex wrapper. Its source default references this checkout and the user's native
Codex package. Before explicitly installing a copy, pin `RUNTIME` to the durable
approved runtime and verify `NATIVE`. Keep the wrapper's interpreter compatible.
Both ingress and controller must load the same validation revision, including
controller-escalation evidence checks. Verify canonical pending questions,
changed-evidence rejection, ordinary commands, quiet status suppression and
project mutes without contacting real recipients.

Atomically replace only approved launchers, then start one controller with the
original hub/reminder/mute/time settings. Check actual command identity, advancing
heartbeat, source hashes, old status readability and ACP trace behavior. Preserve
all task state, attempts, failure logs, recovery windows and pending/unknown sends.
An observation-only workflow-link may register a genuinely completed successor;
it never clears the predecessor's STOP or changes its raw completion state.

Rollback switches approved entries/controller back to the exact retained runtime.
Do not restore an old SQLite/inbox/outbox snapshot over newer receipts. Reconcile
new-format pending questions before running an older validator; do not replay
unknown delivery or silently weaken evidence guards. Retain source, receipts and
lineage history. The local verified rollout and this source release have separate
validation scopes, summarized in [release validation](recovery-release.md).
