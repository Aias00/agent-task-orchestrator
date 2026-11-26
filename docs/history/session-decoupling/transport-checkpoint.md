# Durable transport stage checkpoint

This checkpoint covers only the assigned durable authority, fencing and exclusive
execution/recovery stage. The full frozen objective remains in `task.md`.
No live runtime, launcher, policy, real session, installed skill, external checker,
external baseline, scheduler task state or inherited test/fixture was modified.
No commit, installation, provider call or detached implementation job was used.

## Implemented authority and transitions

`message_bus.py` remains the sole managed mailbox. Its additive schema v2 keeps
old IDs, events, bodies and v1 idempotency fingerprints. New envelopes persist
the canonical question/request ID, routing authorization source, immutable route,
frozen contract, execution generation, route generation, endpoint owner generation,
unique dispatch attempt and accepted native turn. SQLite FULL/WAL transactions
commit the possible-external-effect barrier before invoking an adapter.

V2 receiver receipts authenticate the private endpoint credential and check the
exact session, canonical message correlation, project, request, attempt, execution,
route and endpoint generations. The first acceptance binds the native turn;
responses and turn completion must use that turn. Duplicate receipts are durable
no-ops; conflicting duplicates roll back. Old owner/generation receipts stay in
history and cannot settle or free a new execution. Receipt source and all fences
are retained in a bounded event record; endpoint/owner secrets stay private.

States are queued → claimed → dispatching → delivered or delivery_unknown, then
accepted → responded → turn_completed. A known no-effect failure releases the
slot. A crash before dispatch can reclaim the same envelope; a crash after the
barrier or possible native effect cannot replay it. Unknown never becomes queued.
Explicit current-host native reconciliation can release a quarantined slot for
*distinct* work, without resending the original. None of these states accepts a
workflow criterion, authorizes a human decision, or proves business completion.

Native queue release uses the trusted registry's verified
`native_queue_serialization` capability, without a vendor-kind branch in the bus.
A native enqueue receipt releases the submission slot, not receiver acceptance.
A non-queue managed transport retains its slot until exact turn completion or
explicit verified reconciliation.

## Canonical integration and compatibility

New managed question/answer, recovery and controller follow-up deliveries point
to one mailbox ID. Their JSON/controller outboxes are compatibility projections;
only the mailbox can claim/send their envelope. Losing a projection after send
reconstructs the same canonical envelope without another native call. Questions
retain their original hub and authorization source. Follow-ups for a question
use its canonical ID, route and generation; reminder phases retain the existing
configured ceiling. Distinct authorization sources get distinct host identities,
so a recovery source cannot silently reauthorize an interaction route.

The projection's `state` preserves old caller expectations (`queued` for a known
native submission); `mailbox_state` exposes the actual durable transport state.
`message_id` remains the external native receipt ID, and `mailbox_id` identifies
the managed envelope. Existing legacy outboxes without a mailbox pointer retain
their original at-most-once barrier and are never automatically imported/replayed.

Preserved v1 third-party references only promised `send()` and have no verified
managed native lifecycle. Their public submission shim retains the existing
outbox as their *single* authority, with `authority=legacy_submission`; it never
also creates a mailbox envelope or advertises v2 support. V2 references use the
managed mailbox. V1 adapters with a verified native queue capability can use it
as well. This compatibility seam is required by the inherited correlated relay
fixture; it does not upgrade installation/worker capability into coordinator
capability. Legacy raw UUIDs still use the trusted Codex compatibility adapter;
old headers without cwd retain a legacy home namespace rather than fabricated
v2 project verification.

Pre-generation controller diagnostic records also keep their historic completion
projection via an explicit adapter compatibility marker. It cannot create a v2
turn receipt, approve a user decision or complete workflow criteria. New typed
native observation still requires exact turn IDs.

## Execution and recovery ownership

The same mailbox persists execution leases for origin, worker and session host,
with an immutable project/task/contract/generation binding, private token hash,
monotonic ownership fence and finite deadline. These cooperate with the existing
repository flock: lease expiry never authorizes running through a held flock.
Foreground workflow/top-level run owners acquire and renew a lease; nested stage
workers inherit the workflow guard and owner rather than becoming a second owner.
Run state is reloaded after lock acquisition. Origin takeover retains its lease
across CLI calls and releases it on repaired, needs_input or blocked disposition.
An expired origin/session-host lease also requires verified idle activity of its exact prior workflow before another owner can execute; unknown remains quarantined. No attempts, deadlines, supervision ceilings or cumulative history are reset.

Actual absolute-workflow native load/resume joins that same repository lock and
execution lease, rechecks STOP/closure, contract, project, generation, owner and
verified idle activity, then invokes the adapter. Abstract authenticated adapter
contracts retain their low-level API for non-directory workflow identifiers;
that API is not the workflow recovery entry point. Native load/resume cannot
silently replace the native session ID. Reconstruction into a new session must
be explicit in the later managed coordinator stage. Unknown activity stays
unknown and cannot authorize session-host recovery.

Every managed dispatch rechecks its immutable endpoint and the workflow's
STOP/closure, contract and generation immediately before and after committing
the send barrier. Corrupt canonical sources remain queryable diagnostics and
cannot authorize a new delivery. Silence/presentation/actual user answer behavior
and independent workflow acceptance remain owned by the existing interaction
and controller paths.

## Validation and handoff

The new `tests/test_session_v2_transport.py` contains 44 actual behavior cases.
They cover SQLite reconstruction, real subprocess crash windows, thread races
across independent SQLite connections, duplicate/forged/late receipts, exact
turns, generation advance, queue capability release, explicit reconciliation,
canonical question/answer/follow-up integration, private credential recovery,
and origin/worker/session-host recovery exclusion. All principals and effects
are synthetic temporary fixtures. No actual Pi CLI or cloud model was tested.

A selected compatibility run passed 147 tests with zero failures/errors/skips
before the final five session-host cases were added; those cases passed in the
44-test transport run. The final transport report is retained under
`build/session-decoupling/transport-fixture-results.json`.
All 38 externally baselined inherited test/fixture files retain their exact hash.

One exploratory full discovery ran 462 then-existing tests and did **not** pass:
11 failures and 26 errors. Socket fixtures were denied by the worker sandbox,
and native adapter fixtures using nested sandboxing could not complete. It also
exposed existing broader qualification issues outside this stage: the missing
`session_router` import in `workflow_closure.py`, historical origin-evidence text
compatibility, and unhandled-failure notification fixtures. These are recorded
for the later integrated regression stage; they are not claimed as sandbox-only
failures or as passing regression. The broad run was not repeated after source
fixes because this worker cannot qualify the prohibited socket/nested sandbox
paths. No old test was altered or skipped to hide these results.

The frozen external acceptance command must be run by the scheduler host:

```
python3.12 OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py transport
```

This worker does not run that external writer or claim host acceptance. The
stage is ready for that check; the total six-step objective is still unfinished.
The final real-Pi local smoke remains reserved for the final delivery stage and
its explicitly authorized origin host handoff.

Schema rollback is a read boundary: the old mailbox implementation refuses
schema v2. Do not lower the schema version or overwrite the database with a
snapshot. Retain/drain or quarantine new managed messages with the matching
reader before rollback; legacy outboxes keep their original no-replay status.
No runtime activation or rollback was performed.
