# Source publication and runtime upgrades

Git is the authoritative source history. Keep development on reviewed branches,
commit the selected source and tests, and verify the exact source that will be
deployed. A deployment directory should be an immutable versioned snapshot;
editing or moving it can affect existing subprocesses and future continuations.

Publishing source or changing the default Git branch does not update an
installed launcher. `scripts/install.py` points a launcher at the selected
checkout and starts no services. Before an authorized runtime upgrade, inspect
the current launcher, controller, active workflows, child processes, queued or
unknown deliveries, policy and source versions. Existing active work may still
depend on its original directory; leave that directory available.

Stage and qualify the new version alongside the old one. Switch only the
explicitly approved entrypoints at a verified safe boundary. Keep one active
controller, stop its predecessor gracefully, and independently read back the
actual command, heartbeat, selected source and preserved user policy. Do not
restart business services, replay uncertain messages or migrate old task state
merely to install source. Any such operation requires its own scope.

Rollback restores the approved executable entrypoints and observation process,
not an old task/SQLite snapshot over newer work. Preserve STOP, attempts,
criteria, accumulated time, native identities and delivery claims throughout.

The execution-efficiency development snapshot contains stronger per-task
runtime pinning work. Its presence in a source branch is not a claim that all
current workflows already use that unfinished feature.

## Standard snapshot tool

Use [release_snapshot.py](../scripts/release_snapshot.py) from the reviewed source
with Python 3.12+. Every mode requires explicit absolute paths. The tool never
selects an implicit Git HEAD, creates business authorization, drains a queue,
replays an uncertain delivery, restarts a supervisor, or restores task/SQLite
backups. `scripts/install.py` retains its inert local installation behavior.

The following paths are examples for an isolated rehearsal. Select authorized
paths separately for a real release; source review and staging confer no live
activation permission.

```sh
# Prepare the parent directory yourself; the snapshot destination must not exist.
.venv/bin/python scripts/release_snapshot.py --stage \
  --repo /tmp/ato-source --commit FULL_COMMIT_HASH \
  --snapshot /tmp/ato-releases/FULL_COMMIT_HASH

.venv/bin/python scripts/release_snapshot.py --dry-run \
  --repo /tmp/ato-source --commit FULL_COMMIT_HASH \
  --snapshot /tmp/ato-releases/FULL_COMMIT_HASH \
  --bin-dir /tmp/ato-bin --ato-home /tmp/ato-state --codex-home /tmp/ato-native

# Only after actual operator authorization. A fresh private transaction directory
# is created outside both state homes, bin-dir and snapshot.
.venv/bin/python scripts/release_snapshot.py --activate --allow-runtime-change \
  --commit FULL_COMMIT_HASH --snapshot /tmp/ato-releases/FULL_COMMIT_HASH \
  --bin-dir /tmp/ato-bin --ato-home /tmp/ato-state --codex-home /tmp/ato-native \
  --transaction /tmp/ato-release-transaction

# If the inspected installation selects mixed old sources, operator approval
# must explicitly include convergence. This additional flag never skips checks.
.venv/bin/python scripts/release_snapshot.py --activate --allow-runtime-change \
  --converge-mixed-entrypoints \
  --commit FULL_COMMIT_HASH --snapshot /tmp/ato-releases/FULL_COMMIT_HASH \
  --bin-dir /tmp/ato-bin --ato-home /tmp/ato-state --codex-home /tmp/ato-native \
  --transaction /tmp/ato-mixed-release-transaction

.venv/bin/python scripts/release_snapshot.py --rollback --allow-runtime-change \
  --bin-dir /tmp/ato-bin --ato-home /tmp/ato-state --codex-home /tmp/ato-native \
  --transaction /tmp/ato-release-transaction
```

`--dry-run` checks the commit/tree and each of the three installed launchers,
reporting its own absolute `python`, `root`, `script`, checksum and mode. Different
source roots **or interpreter paths** set `mixed_sources=true` and
`converge_required=true`; they do not end inspection early. The old observer is
always checked against **ctask-notify's** interpreter and runner source, even
when ctask and the handoff reader select other sources. Exactly one controller
with that precise command, matching kernel start `pid_identity` and healthy
heartbeat is required. `observer_source` makes this choice explicit (`source`
remains its compatibility alias).

The report continues through policy hashes and selected settings in
`policy_status`, plus mailbox envelope counts, blocking states and `turn_slots`
counts (`idle`, `busy`, `total`). An idle slot here means a stored envelope with
`busy=0`, not authorization to drain or replay that envelope. A queued/unknown
envelope still blocks activation even if its slot is idle. `mailbox.idle` is true
only when both envelope states and all external turn slots permit the boundary.
`activation_blockers` explains missing qualification, mixed sources and busy
mailbox conditions; `activation_requires` lists explicit activation flags.
`activation_ready` describes the default activation mode, so it stays false for
a mixed installation even with a qualified target and idle mailbox.

Dry-run prints this plan without creating a snapshot, runtime directories,
locks, backups or entrypoints, or changing policies or processes. SQLite inspection reads a stable main/WAL copy
in a temporary directory, so it cannot create shared-memory files in the live
mailbox. A changing database, missing policy/binding, unknown schema or ambiguous
process refuses inspection; busy mailboxes appear in the plan without draining.

`--stage` uses `git archive`, builds a temporary Git index and compares its tree
to the selected commit before installing dependencies. It creates a private
`.venv` from the pinned ACP lock and uses `npm ci --ignore-scripts` with the npm
lock. Qualification runs the full offline suite (including structural, CLI,
metadata, eight-core AST and source-distribution tests), architecture gate and
all tracked Python compilation **in the destination snapshot** with disposable
ATO/Codex homes. Logs remain in `.release-logs/`. A source mutation or any failed
check prevents publication. The temporary `.git` is removed, all regular files
and directories lose write permission, and `.release-manifest.json` records
source, dependency and log hashes, exact modes and symlink targets. Activation
rechecks the entire inventory, including external interpreter target hashes.
Only the conventional venv interpreter symlinks may point outside the snapshot;
sealing never follows symlinks to chmod the host interpreter. Read-only modes
are an operational seal, not protection against the owning user deliberately
changing permissions. The host Python standard library and OS remain trusted
prerequisites. Failed stage directories retain their evidence, have no qualified
manifest, and must not be reused as a destination.

`--activate` requires a full 40-character commit and `--allow-runtime-change`.
Mixed old entrypoints are refused by default. Only **both** that permission flag
and `--converge-mixed-entrypoints` allow explicit convergence; the latter is
accepted only in activate mode. Every old launcher must still be a regular
executable with recognized absolute interpreter/script paths and existing source
files. The three old entries may each select a different interpreter and source;
no old entry is inferred from another. The unique old controller must select
ctask-notify's exact old command and match its recorded `pid_identity`. These
requirements, policy checks and mailbox gates are identical in convergence mode.

It refuses queued, claimed, dispatching or delivery_unknown envelopes and **any
busy external turn slot**, including delivered or cancelled envelopes. It
records private launcher bytes/modes/checksums and policy hashes in
`transaction.json` (0600 inside a 0700 transaction directory). It verifies the
old observer's exact command and `pid_identity`, sends only SIGTERM, waits for
its exit, then reserves the mailbox writer boundary with `BEGIN IMMEDIATE`
and checks it again. Each of the three entrypoints is replaced atomically with
fsync; the set uses a durable recovery journal, not a filesystem-wide atomic
three-file rename. Concurrent launcher use should remain quiescent during this
brief cutover. The writer reservation ends before starting the observer; later
new business messages are ordinary activity, not replay by the release tool.

The observer starts through `control.start()` from the selected snapshot using
a minimal environment. It preserves existing hub, notification, completion,
reminder and time policies. Successful activation requires exactly one observer
with the expected command/start identity and two fresh, advancing healthy
heartbeats, plus independent policy/entrypoint readback. The tool never calls
the enable CLI or supplies new policy defaults. A failed cutover attempts to
restore the backed-up launchers and old observer, records `rolled_back` or
`recovery_required`, and returns nonzero. An uncooperative process is never
SIGKILLed. Changed policy, ambiguous identity, a newly busy mailbox or unrelated
entrypoint replacement stops automatic recovery for operator inspection.

`--rollback` accepts the same transaction and home/bin context. It checks idle
mailbox and observer identity again, restores **only the three executable
entrypoints** and starts the prior observer from its still-available source.
For a convergence transaction, `before.entries` retains each launcher's own
original bytes, mode and checksum, and `before.sources` records all three old
sources. Rollback restores the distinct entries individually, then starts the
observer from the backed-up **ctask-notify** source, rather than from ctask's
possibly different old root. It needs `--allow-runtime-change`, without another
convergence flag. Existing version-1 transactions remain supported. Failed
partial convergence uses the same per-entry restoration and old observer source.
It does not copy any task, STOP marker, contract, attempt counter, elapsed time,
mailbox or SQLite ledger. Progress since activation therefore remains current.
Keep the transaction and all old snapshots as long as existing workers or
future continuations may need them. The release lock serializes this tool's
transactions; it cannot prevent an unrelated installer from replacing files.

Home selection must match the installed runtime. Supplying distinct explicit
`--ato-home`/`--codex-home` selects the namespaced ATO context. For the original
CODEX_HOME-only layout, pass identical home paths **and** `--legacy-context` to
dry-run, activate and rollback; the observer then omits ATO_HOME. This flag does
not migrate records between layouts. Opaque actor/session tokens and Python,
Node or dynamic-loader hooks are never inherited from the caller's environment.
The offline release tests use temporary homes/bin directories and fake process
facades. They do not establish real-provider qualification or approve a live
cutover.

Stage resolves Node and the npm CLI once from the invoking operator's PATH, or
from explicit `--node-bin /absolute/node --npm-bin /absolute/npm` arguments. It
checks owners, ancestor write permissions and source/snapshot exclusion, invokes
npm through the selected absolute Node, and passes only that `CTASK_NODE_BIN`
into the sanitized qualification environment. This supports private NVM installs
without inheriting the entire shell PATH or Node/loader hooks. The seal records
both executable hashes; verification rejects changed dependency tools. Observer
startup uses the recorded Node locator (or a freshly validated host locator for
older snapshots without this manifest). This selects a binary, not an actor or
provider credential.
