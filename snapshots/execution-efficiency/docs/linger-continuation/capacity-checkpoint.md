# Capacity stage checkpoint — inactive local source candidate

This checkpoint covers only the assigned third stage of [the complete task](task.md).
It does not accept the overall objective, activate a runtime, migrate live state,
grant new authority, or qualify a real provider/Pi workload. The accounting and
presentation implementations were inherited; all protected predecessor tests and
fixtures remain byte-identical. Git HEAD is older than this seeded candidate, so
the complete Git diff is not the scope of this stage.

## Configuration and explicit opt-in

The host-owned `ATO_HOME/capacity.json` uses this exact shape:

```json
{"version":1,"endpoints":{"local-fixture":{"limit":2,"reserved_interactive":1}}}
```

New plans/contracts select an alias and priority, independently of the versioned
execution accounting policy:

```json
{"capacity":{"endpoint":"local-fixture","priority":"background"}}
```

Only `interactive` and `background` are accepted. The alias is bounded plain text,
not a URL, path, key or token. Limits must be positive integers, with integer
reserve `0 <= R < N`; booleans, extra fields, duplicate JSON fields, unknown
versions, symlink configuration files and group/world writable configuration are
rejected. The ATO home and file must belong to the current user. A missing or
invalid trusted configuration/alias stops opt-in work before an invocation or
failure is charged. Plans cannot supply a configuration path or global limits.
Legacy plans without `capacity` do not consult this configuration or gain queue
records. There is no rewrite of old task plans, ledgers or attempts.

The host must keep its trusted ATO home outside worker-writable roots. This queue
does not create a privilege boundary against unrestricted same-user filesystem
access, and adds no filesystem grants. Capacity is independent of repository
ownership, actor/session credentials, permission probes and manual acceptance.

## Store and real execution paths

`capacity.py` implements the frozen `CapacityStore(path, clock=...)` API. Each
enqueue/claim/release/cancel and the admission-order/occupancy decision serialize
in a SQLite `BEGIN IMMEDIATE` transaction with full synchronous commits. The
host store is `ATO_HOME/capacity.sqlite3`; it is created with private mode. There
are no services, observers, network requests, detached jobs or new schedulers.

IDs retain immutable endpoint, priority, owner and generation. Duplicate enqueue
preserves FIFO position and terminal tombstones; duplicate claim returns the same
token and cannot allocate twice. Background claims obey FIFO and never use the
interactive reserve. When requests contend for a slot, interaction wins; a
background request may enter when all pending interaction still fits in the
remaining slots, including the reserve. Interactive requests are also FIFO.
Waiting background work can starve under continuing interaction: snapshots expose
its age, queue sequence and this limitation, without a strong fairness claim.
Trusted capacity reductions affect future admission and never revoke existing
holders or terminate their work.

`Runner.attempt` admits before either legacy or v2 attempt accounting, and before
building/launching the worker. `Workflow.propose` admits before a planning attempt.
Preset/generated stages inherit the exact frozen selection, checked again at
stage execution; stages cannot opt in separately or override priority/endpoint.
The existing repository flock, ownership lease, parent guard, plan/contract hash,
permission checks, STOP and unknown-delivery checks retain authority. A capacity
claim cannot bypass any of them. No model runs while its request is queued.

Admission waits persist a `capacity_wait` row and request binding before claim,
record real wait time and use existing external-wait accounting. They do not
increment invocations, failures, worker waits/yields or active command time.
The existing serial repository owner remains held: this stage adds endpoint
admission, not an additional execution lane within that owner. Task summaries
read the SQLite snapshot without schema/configuration writes, model wakes,
notifications or release tokens. A dead queue owner is reported as unverified.

## Cleanup, STOP and restart boundaries

Claim tokens are bound to exact request/owner/generation. Integrated release passes
all of them, after command process-group cleanup and structured delivery handling.
Wrong token/owner/generation, non-boolean cleanup confirmation, missing cleanup or
unknown delivery retain occupancy. There is no expiry reclamation. Cooperative
wait/yield releases a confirmed invocation and a later invocation queues with a
new generation; releasing capacity does not release repository/task ownership.
ACP uncertain outcomes, interrupted active receipts and cleanup refusal remain
quarantined and cannot be automatically replayed or promoted by host checks.
Release rejection also blocks a nominally successful worker or planner in the
same execution: passing artifact checks cannot erase the retained occupancy or
authorize the planner's proposed stage.

The existing STOP command cancels exact pending requests, including a dead owner's
queue and the bound active child of a stopped workflow. It does not release active
claims or compete with the owner writing task JSON. A live owner also observes
STOP under its existing guards and cancels its pending receipt. STOP is checked
again after claim before any launch. Cancellation neither removes STOP nor grants
resume. Legacy STOP behavior is unchanged when capacity is absent.

A restarted pending request retains its exact FIFO identity. It still needs the
independent repository ownership checks/lease; capacity does not preempt that
owner. A crash after claim is conservatively unknown, even before a model starts:
restart cannot adopt or release that active receipt. No automatic orphan repair
or operator release command is added. Original confirmed cleanup and authorized
operator review remain necessary; elapsed time or a dead PID alone is insufficient.
Restart also reconciles a pending JSON receipt against the read-only durable
store before acceptance or a planner invocation. This covers a crash after the
atomic SQLite claim but before the active JSON receipt is saved. In both legacy
capacity-opted-in and v2 workflows, an unknown planner receipt blocks overall
acceptance even when independent work has since satisfied every artifact check.

## Verification and exact handoff

The new `tests/test_capacity_queue.py` uses temporary Git repositories, empty
private ATO/Codex homes and synthetic CLIs only. Coverage includes independent
process races, cross-connection reservation, durable restart, real Runner queueing
and counters, cross-repository interaction reserve, STOP while queued/after owner
death, parent/child cancellation, Workflow planner and stage integration, changed
capacity, configuration refusal, unknown delivery, cleanup refusal and read-only
CLI projections. Results, hashes and logs are recorded in `capacity-validation.json`
and `evidence/capacity-*`. The unmodified external `Capacity` class is also run
directly, with receipts saved inside this checkout.
The retry adds process-crash coverage of the SQLite/JSON commit boundary and
fault-injected release refusal through actual Runner and Workflow execution.
Legacy workflow views project their existing capacity planner receipt without
inventing v2 accounting or altering the stored ledger.

Final retry verification: 34 new capacity behavior tests plus all 7 frozen
external Capacity cases passed (41 total); 79 selected accounting/presentation/
Runner/Workflow tests plus 7 frozen Lifecycle/Presentation cases passed (86
total). Both runs had zero failures, errors or skips. Current receipts are
`evidence/capacity-qualified.{log,json}` and
`evidence/capacity-compatibility.{log,json}`. Source compilation and `git diff
--check` passed. The unmodified read-only preservation check passed after both
runs. Earlier attempt logs remain historical evidence, not the current result.

The original frozen gate writes external receipts outside worker writable roots.
Only the original host/scheduler should run this exact argv:

```json
["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","capacity"]
```

No original host gate pass is claimed here. Known inherited socket/nested sandbox
restrictions are documented in `accounting-validation.json`; broad discovery is
not retried in this worker. The initial selected compatibility invocation omitted
top-level temporary homes and encountered sandbox-denied default-home writes.
It is retained as failed evidence; the corrected isolated run passed. The
unmodified external preservation function checks all 48 protected files, the
installed launcher/runtime and linked dependency without altering them.

## Complete remaining objective

1. Run the frozen capacity host gate on this exact candidate; address any real
   failures in the authorized source scope without changing external checks.
2. Perform final integrated frozen accounting/presentation/capacity/regression
   qualification, retaining all inherited tests and session-decoupling authority,
   STOP, unknown/no-replay, manual acceptance, history and quiet notification rules.
   A selected local suite or successful stage is not overall acceptance.
3. Implement/qualify the still-separate final delivery stage:
   `scripts/prepare_linger_candidate.py` and `--check`, portable source/test/docs
   SHA256 and modes, exact baseline/predecessor binding, reproducible inactive
   archive and `build/linger-continuation/candidate.json`, with
   `runtime_activated=false` and no auth/dependency/task state.
4. Finish the required full architecture, migration, validation and operator
   handoff documents, README/repository skill updates and the frozen delivery
   gate. Preserve legacy opt-in/data compatibility and rollback without overwriting
   new ledgers; make actual verification limits explicit.
5. The origin performs final full-objective checks under the existing narrow
   host contract. No activation, installation, publication, real cloud/Pi call,
   live task/hub mutation, privilege expansion, preemption or new supervisor is
   authorized by this stage or by a passing capacity gate.
