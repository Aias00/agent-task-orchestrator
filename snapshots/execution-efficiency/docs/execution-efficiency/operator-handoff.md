# Exact inactive-candidate handoff

This handoff belongs to the full [execution-efficiency task](task.md), not to a
live deployment. The source is an isolated copy of completed Linger677. No Git
commit/push, install, activation, live hub/task migration, credential extraction,
new service/supervisor, business trigger or historical transcript scan is
performed or authorized by this candidate. The repository skill is delivered
as source and must not be installed by the worker.

## Candidate and checkpoint

`build/execution-efficiency/candidate.json` inventories all portable source,
tests, docs and modes. `candidate.tar.gz` is reproducible USTAR/gzip. Frozen
`provenance/baseline.json` links the exact external baseline;
`provenance/predecessor-candidate.json` must hash to
`2256534fb9f58ec277f18511b5662479a8da82aa0433a58657a121b200bc02f6`.
`runtime_activated=false` and `overall_accepted=false` are deliberate. A manifest
is a byte inventory, not a self-issued host acceptance receipt. See
[validation.md](validation.md) for limits and historical evidence and
[migration.md](migration.md) for extraction, future version selection and rollback.

The bounded integration checkpoint is `integration-stage-checkpoint.json`.
Its evidence names current source hashes, actual local tests and explicit
omissions. Earlier feature checkpoints retain their original stage scope and
all failures; stale source hashes are never promoted to current proof. The
worker may return `ready_for_checks` only when original host gates remain; it
must not label sandbox-only failures as full candidate failures or claim full
success from a selected fixture subset.

## Current source-only continuation handoff

The 2026-11-20 current authorization supersedes the old sample instruction:
**ZERO fresh native/model trials.** Read [continuation-profile-drift.md](continuation-profile-drift.md)
and `CONTINUATION.md` first. The old failed sample remains failed/unknown with
its exclusive claim intact; its exact drift cause is unknown because original
before/after values were not retained. Do not replay it or mark the predecessor
complete. Source gates cannot resolve the actual host_sample gate.

Run the immutable successor host commands for `drift`, `worker` and `delivery`
listed in the continuation handoff, then all original successor source modes
including fresh full regression. Preserve every external receipt and inherited
test. This worker returns ready_for_checks for those ordinary host tests.
Verify `scripts/prepare_efficiency_candidate.py --check` and extracted source
bytes/modes before origin review. Scheduler-owned .codex/tasks are untouched.

After source gates qualify, origin reports the concrete remaining native
trial/authority choice through the hub. Only a separately explicit reviewed
fresh-sample decision can enable the exact original host_sample/underlying
sample argv recorded in continuation-profile-drift.md. The original wrapper
must not overwrite old failed logs; origin must preserve evidence before any
future authorized handoff. Current source-only work does not grant a sample,
installation, live activation or Git publication. The newer active compact
handoff controller and live policies must remain deployed unchanged.

The correction stores bounded hash-only pre/post profile evidence on every
sample and Runner turn, including failed deliveries. Exact equality is still
required for every original profile field. Startup config creation after
admission is drift, not a whitelist exception. No native home/auth/config is
copied or prepared automatically. Per-turn evidence is private and excluded
from the source archive; failed/unknown claims never permit another turn.

Every arm uses a separate small temporary Git/file sample, actual native tool
operations and fresh Workflow independent checks. Two direct turns reuse one
exact task-owned session; legacy cold starts twice; warm invokes the real
Runner/worker-session/checkpoint path. At most 12 turns and 1200 seconds, with
85-second per-turn protection and reserved closeout time. Failed or unknown
native delivery stops immediately, preserving all evidence; no cold replacement,
repair process, resample, permission widening or business effect follows it.
Model request/read counts remain unknown if not available; two CLI processes
per sample are honestly reported even when native context is warm. No provider
speedup is claimed before measurement, and the sample may pass without a clear
latency improvement.

## Complete task conditions still apply

All six feature routes must remain integrated into actual Runner/Workflow/
Controller/CLI and preserve legacy semantics, STOP, original IDs and accounting.
All original compatibility/fault gates need fresh full discovery >=759 and
zero failure/error/skip. All inherited protected bytes and immutable selected
old runtime/dependency/installed guard/skill invariants must hold. Only the
separate precisely authorized session-decoupling global launcher transition is
allowed; this feature candidate stays inactive. Portable complete packaging,
extracted validation, migration/rollback and honest fixture/timing provenance
must pass. The exact final manifest needs the origin actual default-Codex
same-profile warm native proof. Source changes after that proof invalidate its
binding and require origin review, never automatic new model samples.

## Regression continuation superseding the earlier source gate list

Read [continuation-regression-remediation.md](continuation-regression-remediation.md).
The scheduler's exact current source argv are successor acceptance.py regression
and delivery with the recorded pinned Python. Origin must also requalify runtime,
responsibility, worker, checkpoint, verification, metrics and drift against the
final manifest. The original Pi error cause remains unknown; the unchanged
fixture now has a bounded diagnostic companion. An actual recurrence is not
waived. Use build/execution-efficiency/regression-remediation/handoff.json for
post-freeze results and hashes. The full objective, predecessor-incomplete and
inactive status, preserved failed claims, zero fresh native authority and exact
conditional NOT-authorized origin argv all remain as recorded above.
