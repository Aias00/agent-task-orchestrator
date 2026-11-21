# Inactive integrated continuation candidate

The complete authorized scope is [task.md](task.md). This candidate implements
three independent, explicitly selected mechanisms inside the existing serial
Runner/Workflow execution path. It references Linger's mechanisms at revision
`8d02fe8c73b8289211b5581b483a2c16cfd76918`; it does not copy upstream code,
install upstream packages, add an observer/supervisor, or claim measured Linger
performance. The qualified session-decoupling predecessor remains historical
evidence for its own unchanged bytes, rather than qualification of this bundle.

## Execution and authority

`Runner.attempt` admits capacity before invoking either the legacy execution
path or `attempt_v2`. `Workflow.propose` admits planning work on the same basis.
Workflow validation, stage generation and active-stage execution preserve/check
the contract's exact `execution_policy` and `capacity`. The frozen checks remain
host-selected argv, not new checks supplied by worker output. Stage completion,
native turn completion and an independent branch passing do not accept the
overall objective. Manual acceptance retains its existing explicit evidence path.

The existing repository flock, execution lease, STOP, parent guards, frozen
plan/contract hashes, permissions and actor/project/session/turn/generation/owner
checks still govern execution. Capacity is admission only. Neither available
capacity nor a query grants ownership, filesystem permission, a retry, takeover,
arbitrary resume, new model capability or user approval.

## Explicit version 2 accounting

New plans/contracts can include:

```json
{"execution_policy":{"version":2,"max_continuations":6,"max_waits":4,"max_yields":4,"max_no_progress":3}}
```

All four bounds must be positive integers; unknown/missing policy fields and
other versions are refused. `execution_accounting.py` provides the ledger and
safe source observation; Runner and Workflow persist it under their existing
execution authority. `limits.max_attempts` caps failures for v2. Public
`attempts`/`attempt_sequence` still count every real invocation and retain
history; they are never relabeled as failures. Legacy plans retain their original
attempt and retry behavior with no automatic rewrite or synthetic zero ledger.

Each real invocation receives a sequence and terminal receipt. Nonzero exit,
timeout, invalid structured outcome and claimed completion with failing checks
spend failure allowance. A normal `continue` with unfinished final checks spends
continuation allowance. A finite `waiting` records its reason/deadline; a
cooperative `yielded` releases only after process cleanup is confirmed. Neither
spends failure allowance. They have separate cumulative limits and a no-progress
guard, retained across resume/host checks. Blocked, manual and authorized
needs-host outcomes remain distinct. Unknown delivery is not automatically replayed.

Continuation checkpoints include actual before/after source fingerprints,
independent check observations, sequence, structured outcome and cleanup receipt.
The host hashes only a bounded safe source set: no credential/session/task
history scan, symlink traversal, vendor/build tree or arbitrary large files.
An incomplete snapshot cannot prove source progress. A summary changing cannot
prove progress; an actual source change or independently observed failed-to-passed
check can. This is evidence of progress, not proof of correctness or overall
acceptance. Safety limits and precise filtering are in `execution_accounting.py`.

`active_seconds` records actual command intervals; endpoint queueing and finite
external waits remain outside it. Settled wait/yield time is separate, while
legacy total elapsed remains available. Queries project unsettled intervals
without writing them. Invocation sequences prevent duplicate terminal charges;
resume/host checks cannot erase counters or reopen an exhausted v2 guard.

## Quiet read-only projection

`task_presentation.py` supplies the additive `status.task_view` and
`task-summary DIRECTORY --format json|text`. It distinguishes raw recorded
status, effective status, verified process identity, full objective, current
stage, remaining checks, counters, wait deadlines, capacity, recovery, next step
and whether an actual user decision is needed. Legacy failure/active/wait
breakdowns are unknown. Missing/inconsistent ledgers remain unknown rather than
being repaired during a query. Stage/parent and successor joins require existing
strict binding and evidence checks; raw blocked history stays visible.

Queries read files, bounded telemetry copies and kernel process identity. They
do not initialize message stores, acquire execution ownership, run checks/models,
wake workers, publish notifications, accept answers, mark presentation, release
capacity or mutate STOP/approval/completion. A live PID without its recorded start
identity is unverified. Native activity and resumability remain unknown unless
the existing execution path establishes them. Ordinary notifications remain
silent; real decisions use canonical asks and existing unhandled-failure dedup.
Examples and query interpretation are in [presentation-checkpoint.md](presentation-checkpoint.md).

## Durable bounded capacity

The trusted host configuration is `ATO_HOME/capacity.json`:

```json
{"version":1,"endpoints":{"local-fixture":{"limit":2,"reserved_interactive":1}}}
```

A new plan separately selects `capacity={"endpoint":"local-fixture",
"priority":"background"}` or `interactive`. The file/ATO home must be owned by
the current user, nonsymlink and not group/world writable. Unknown/duplicate
fields, versions, unsafe aliases and invalid limits are rejected. Plans cannot
override limits. The trusted home must remain outside worker-writable roots;
same-user unrestricted filesystem access is not a security boundary created by
this queue. No new filesystem grant is introduced.

`capacity.py` exposes the frozen `CapacityStore` API and persists
`ATO_HOME/capacity.sqlite3`. Enqueue/claim/cancel/release use SQLite transactions
with atomic admission and synchronous commits across connections/processes.
Request identity includes endpoint, priority, owner and generation; duplicates
retain position and tombstones, and repeated claim cannot create another lease.
Interactive requests have priority and a static reserve. Background admission
is FIFO within available nonreserved capacity; continuing interaction can starve
it, so snapshots expose age/order without promising strong fairness. Trusted
limit reductions affect future admission and do not revoke existing holders.

Queueing starts no worker and charges no invocation/failure/active time. Pending
requests survive restart and STOP can cancel their exact identity. Active claims
require exact token/owner/generation plus confirmed cleanup to release. Missing
cleanup or unknown delivery retains occupancy; elapsed time, dead owner or TTL
does not reclaim it. The SQLite-claim/task-JSON crash window is checked before
restart can invoke work or promote acceptance. No orphan repair, automatic
preemption, adaptive concurrency, kill policy or arbitrary resume is added.
Releasing endpoint capacity does not release repository/task ownership.

## Reproducible delivery

`scripts/prepare_linger_candidate.py` packages portable source/tests/docs, the
unchanged repository test workflow YAML, modes,
and exact frozen provenance into `build/linger-continuation/candidate.tar.gz`.
Its sibling `candidate.json` contains SHA256, `runtime_activated=false`,
`overall_accepted=false`, baseline identity and predecessor manifest identity.
Fixed tar ordering/metadata and gzip metadata make identical inputs produce
identical bytes. `--check` independently rebuilds inventory and archive bytes,
without executing or extracting them. It also detects mode, removal and addition
changes. No task/auth/native history/dependency tree or installed runtime is
included. Historical predecessor manifest/archive are not overwritten.
See [migration.md](migration.md), [validation.md](validation.md) and
[operator-handoff.md](operator-handoff.md) for compatibility, limits and the
remaining original host gates. Packaging itself asserts no new acceptance.
