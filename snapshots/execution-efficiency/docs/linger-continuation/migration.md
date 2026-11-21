# Compatibility and reversible delivery

This is an inactive source candidate, not an installed runtime or a live-state
migration. [task.md](task.md) preserves the full objective and authorization.
No current task, hub, launcher, real model configuration or account is changed.
Any later activation/publication requires separate concrete authorization.

## Plan and data versions

| Input/data | Candidate behavior | Rollback condition |
| --- | --- | --- |
| Legacy plan without execution_policy | Original attempt/retry semantics and public counters | Keep original plan/state/history; no migration required |
| New explicit execution_policy.version=2 | Failure cap plus independent continuation/wait/yield/no-progress bounds | Use a v2-capable reader/executor for these tasks; preserve all new fields |
| Legacy historical breakdown | Query reports failures/active/wait as unknown | Never backfill false zero failures from attempts |
| Plan without capacity | Does not consult capacity configuration or queue | No queue opt-in added to existing tasks |
| New explicit capacity selection | Host-owned version 1 config and additive SQLite store | Preserve queued/active/tombstone receipts; do not roll back the queue database |
| Existing session/message authority | Same bindings, actors, contracts, generations and no-replay checks | Keep live authority/history, uncertain deliveries and STOP unchanged |

Choose the versioned accounting policy and capacity independently when creating
a new authorized immutable plan/contract. Workflow inherits the exact selection
into every generated/preset stage. Editing an existing frozen plan to add opt-in,
resetting attempts, reconstructing an old failure ledger, changing stage limits
or reusing another task's authorization is not a supported migration path.

Example new-plan fields, within the usual complete authorized contract:

```json
{
  "execution_policy":{"version":2,"max_continuations":6,"max_waits":4,"max_yields":4,"max_no_progress":3},
  "capacity":{"endpoint":"local-fixture","priority":"background"}
}
```

The trusted capacity configuration remains outside the plan and outside
worker-writable roots. Preserve its configured limits/reserve and SQLite state
together. Do not copy this candidate's synthetic test configuration into a live
home. Alias selection conveys no new provider permission, account or secret.
No current task is automatically queued or rewritten by installing source.

## Offline review and rehearsal

Preparation reads the frozen external baseline and unchanged predecessor
manifest, copies their exact bytes into bundle provenance, and refuses to
overwrite different provenance. It never mutates those originals. Review with:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python scripts/prepare_linger_candidate.py --check
```

The bundle carries its own baseline and predecessor manifest for portable
checking. Keep the archive beside `candidate.json` under
`build/linger-continuation/` after extraction into a new private temporary
directory; run that extracted script with `--check`. This needs only Python's
standard library. Tests that use ACP still require the unchanged qualified
host's interpreter/dependency; no dependencies or symlinks are bundled or installed.
The old `build/session-decoupling` archive/manifest remain history. Their proof
does not automatically qualify changed sources, and regenerating that old
manifest against this new candidate would destroy the intended distinction.

## Rollback without losing newer evidence

Current delivery is reversible by retaining/removing this separate inactive
bundle; the installed runtime is unaffected. If a later separately authorized
switch is considered, first establish an offline quiescent boundary, preserve
the exact old/new source identities, launcher target, plans, states, messages,
capacity database with consistent SQLite WAL state, and authority/history.
Do not take a partial online file copy and describe it as a consistent backup.

Return the launcher only to a separately preserved compatible runtime, if that
switch is later authorized. Never restore an older state/database over newer
data to make old software run. Legacy executors must not resume v2/capacity-opted-in
tasks or ignore active/unknown claims; preserve them read-only until a compatible
executor and authorized operator can resolve them. Do not drop new tables,
strip fields, clear attempts, free slots based on age/dead PID, resend uncertain
messages, rebind identities, remove STOP or infer manual acceptance.

All actual migration, activation, production checks, credentials, Git publication
and real models/Pi calls are outside this task. The original host contract
allows only the frozen isolated acceptance modes in [operator-handoff.md](operator-handoff.md).
