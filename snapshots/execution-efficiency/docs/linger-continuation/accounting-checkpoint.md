# Accounting stage checkpoint — inactive local candidate

This checkpoint covers only stage 1 of [task.md](task.md). The complete objective
still requires quiet truthful task views, bounded durable interaction-prioritized
capacity scheduling, all frozen gates and reproducible delivery. Nothing here
activates a runtime, migrates live state or accepts the overall objective.

## Explicit compatibility boundary

Only newly created plans/contracts containing the following complete object use
the new execution path:

```json
{"execution_policy":{"version":2,"max_continuations":6,"max_waits":4,"max_yields":4,"max_no_progress":3}}
```

Every bound must be a positive integer (booleans are rejected); all fields are
required and unknown fields/versions are rejected. Workflow freezes and copies
the exact policy into preset and generated stage plans. A stage cannot override
the workflow policy or opt in independently of a legacy parent.

Absent-policy plans retain the original decoder, schema, invocation-based
`max_attempts`, retry behavior and public fields. They are never rewritten or
given fabricated zero-failure history. A v2 state missing its ledger is rejected
rather than silently migrated. Rolling back code must retain v2 data separately:
the predecessor rejects this policy; never strip it or reinterpret its counters
to resume a v2 task with the old runtime. No live migration/rollback was performed.

## Actual execution and evidence

Runner v2 keeps cumulative `attempts` and `attempt_sequence`, including across
explicit retries and rechecks. `execution_accounting` records invocations,
continuations, waits, yields, genuine failures, active seconds, external wait
seconds and cooperative yield seconds. Each invocation has a sequence-keyed
durable start/terminal receipt; terminal charging is idempotent. `max_attempts`
is the genuine-failure ceiling. Nonzero exits, timeout, malformed structured
results, and complete/ready-for-checks claims rejected by frozen checks spend a
failure. A later final integration failure is charged once to its invocation.
Repeated resume/rechecks do not reset counters or re-charge the same failure.
Successful host repairs retain previous failures and invocation history.

`continue` runs original host checks and saves a `.progress.json` checkpoint;
checks that still fail do not spend failures. A continue never completes a task,
even if its checks already pass. The checkpoint contains host-observed source
hashes, actual check signatures, invocation/output references, cleanup observation
and progress verdict. An initial independent check establishes a baseline so an
unknown result is not invented as a failed check. Check improvement means an
observed fail became a pass; changed summaries, log timestamps and worker claims
do not establish progress.

Source observation hashes a bounded safe source set from current tracked and
untracked nonignored files. It reads no Git history or task transcripts. It
excludes hidden paths, credentials/auth/session-like names, private/dependency/
build trees, and follows no symlink component. Limits are 2048 source paths,
512 KiB per source and 8 MiB total; incomplete snapshots cannot establish source
progress. This conservative source allowlist can miss arbitrary binary/output
changes; actual independent check improvements provide another evidence path.
Hashes demonstrate a source change, not its semantic correctness; final frozen
checks remain mandatory.

Waiting requires a concrete recorded reason and positive integer retry interval,
bounded to 3600 seconds in Runner (Workflow planning uses its original finite
`max_wait_seconds`). A durable next-check timestamp survives resume. Retry flags
cannot advance that timestamp. The DAG runs independent ready branches before
idling. Active seconds meter actual worker/check commands; per-row wait seconds
continue through independent branch execution and restarts without double
charging. Existing elapsed/wait ledgers retain their prior meaning.

The v2 worker schema also accepts `yielded`. This is voluntary and becomes a
bounded cooldown only after the actual process group is cleaned up. It grants
no capacity lease, repository lock, resume capability or authority. Unknown
cleanup retains process evidence and blocks continuation/yield. Existing STOP,
owner/session/turn/generation/actor checks remain in place. Interrupted receipts
and unverified ACP delivery are quarantined; neither retry flags nor host
rechecks authorize automatic replay or promotion of unknown delivery.

Each normal outcome has a cumulative finite bound. Reaching its configured
count (or consecutive no-progress count) saves evidence and blocks further
automatic invocation; a retry flag cannot clear it. Completion, manual blockers
and authorized host handoffs remain separate. Workflow aggregates stage and
planner ledgers, meters actual host checks, bounds repeated planner waits, and
still checks every total criterion and actual manual acceptance.

## Qualification and exact next action

The worker uses the existing frozen Python, temporary Git repositories, fresh
private ATO_HOME/CODEX_HOME directories and synthetic local CLI identities only.
No provider, real Pi/native auth or installed launcher is invoked. The added
`tests/test_continuation_lifecycle.py` covers integrated Runner/Workflow execution,
progress/no-progress, failure ceilings, independent wait branches, cooperative
yield, cleanup refusal, STOP, unknown delivery, persisted wait deadlines,
host-recheck continuation, manual gates and legacy behavior. Worker evidence and
final source hashes are recorded in `accounting-validation.json` and `evidence/`.

The original scheduler/host must run this exact frozen accounting gate:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py accounting
```

Worker-side direct execution of frozen Lifecycle tests is useful evidence but
does not claim that this original gate or overall acceptance passed. The gate
writes its receipts in the external operations directory, which the worker must
not edit. Socket/nested sandbox restrictions in inherited tests belong to the
authorized original host gates; do not retry them inside this worker sandbox.

## Complete remaining objective

1. Host qualification of this accounting stage with the original argv above.
2. Stage 2: additive read-only task_view/task-summary JSON/text, complete objective
   versus stage, real owner/process/recovery/successor state, true counts including
   legacy unknown failures, remaining acceptance and truthful needs_user; preserve
   notification silence and canonical actual human questions; at least 10 new
   presentation tests and frozen `acceptance.py presentation`.
3. Stage 3: trusted static endpoint capacity/reserved interactive configuration,
   explicit opt-in inheritance, durable deduplicated queue and atomic exact
   token/owner/generation claims, priority/FIFO and starvation visibility, STOP
   cancellation and cleanup-confirmed release without TTL reclamation,
   preemption or permission changes; real Runner/Workflow integration, at least
   12 new capacity tests and frozen `acceptance.py capacity`.
4. Final regression: all inherited 563 tests plus every new suite, zero failures,
   errors or skips under original host gates; preservation of every frozen hash,
   old API/history, installed runtime/launcher and dependency.
5. Delivery: prepare_linger_candidate.py/--check, exact portable source/test/docs
   SHA256/modes, baseline and predecessor-manifest identities, repeatable inactive
   archive; architecture/migration/validation/operator-handoff docs, README and
   repository skill updates, frozen regression/delivery gates. No activation,
   installation, publication or live-state migration is part of this task.

Authorization is the frozen 2026-11-19 user “好的可以继续” source in task.md and
the assigned worker prompt. Historical Linger assessment is reference only
(`shared/projects/long-task-workflow/linger-reference`, candidate, 2026-11-19).
This implementation is independent and copies no upstream implementation.
