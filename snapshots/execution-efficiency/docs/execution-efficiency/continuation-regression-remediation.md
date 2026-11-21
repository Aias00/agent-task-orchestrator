# Source-only regression remediation and host diagnostic handoff

This stage preserves the full task in task.md, the Linger677/2256534 baseline,
the completed profile-evidence correction and every predecessor condition.
The candidate is inactive. Predecessor completion, actual native qualification,
provider speedup, installation and activation are not claimed. ZERO fresh
native/model/provider trials are authorized by this continuation.

## Demonstrated causes and precise changes

The original frozen host regression ran 936 tests with two failures. Its exact
log/report and hashes are preserved in
build/execution-efficiency/regression-remediation/original-regression-latest.*.
The original failed native sample and claim are untouched; their historical
profile-drift cause remains unknown.

1. **Adaptive reserve review race.** budget.poll throttled all reviews for one
   second. A guard at the soft deadline could arrive before the next poll,
   despite a newly successful independent check, and enforce the stale soft
   boundary. The retained fixture reproduced limit with the first criterion
   passed, no extension and the second check interrupted. A deterministic
   clock experiment at 100.1 then 101.0 proves the same before/after behavior:
   old code halts; corrected code releases only the existing authorized reserve.
   Some wall-clock schedules already pass the old code; that is evidence of
   the polling race, not a reason to retry until green. The minimal change lets
   a guard at its deadline bypass the optimization throttle. Runner and Workflow
   also force one final review if the enforcement clock crosses the boundary
   after the initial poll read. A second deterministic before/after probe proves
   that narrower race, without changing any deadline or envelope. Hard/fixed limits,
   extension counts/chunks, independent-command deduplication and advisory
   policy remain unchanged. Children still use the live parent guard; no new
   child budget, reset or authority is created. Full runtime/profile validation
   is still performed before reserve polling, including validation overhead.

2. **Timeout failure accounting edge.** A deterministic admitted v2 invocation
   with unconfirmed cleanup produced cleanup_unknown and delivery_unknown but
   zero failures whenever checkpoint_policy was absent. It now charges one
   failure using the existing idempotent terminal receipt. The quarantine and
   explicit retry barrier remain. A genuine hard timeout charges one failure;
   successful legacy explicit resume retains its original sequence, elapsed
   accounting, trace and checkpoint files. STOP still prevents invocation.

The original Pi timeout host result **error rather than blocked is not yet
explained**. Its traceback omitted state.error/events. Under this worker the
unchanged adapter fixture cannot enter its intended timeout: nested
sandbox-exec returns sandbox_apply Operation not permitted (exit 71). Direct
model-free Runner/process-group timeout tests return blocked with confirmed
leader/descendant cleanup. Neither observation establishes the old host cause,
and the accounting edge above is not asserted to be that cause. No speculative
catch, success conversion, sandbox fallback or timeout enlargement was added.
Runner run_error now includes only bounded exception class/code locations
(no locals, source lines or native bodies). A new diagnostic test invokes the
unchanged inherited Pi test and emits its bounded synthetic state/events/stderr
on failure, preserving the original assertion. The host can now establish the
cause if it recurs; a recurrence requires source remediation, never acceptance
of error as success.

## Local evidence and freeze

The final 18 deterministic new tests passed within the 60-test affected
budget/advisory/STOP/timeout set, zero failures/errors/skips; counts overlap.
The final refinement adds explicit once-only accounting/no-replay assertions.
Initial test-harness mistakes and the demonstrated pre-fix accounting failure
are retained, not erased. Portable directed evidence is under
evidence/regression-remediation/. Full raw synthetic state and diagnostic logs
are under build/execution-efficiency/regression-remediation/.

All 61 successor-protected predecessor hashes and every pre-existing candidate
test/fixture hash remain unchanged. The 936 existing discovery tests remain,
with 19 new cases added. Final discovery is required to run all 955 cases,
zero failures/errors/skips (and satisfies the original minimum 759).

The first frozen 953-test discovery ran all then-current tests with zero skips;
it recorded 10 failures/14 errors from nested sandbox and local socket denial,
not host acceptance. Its exact report and source/archive remain preserved. The
final guard refinement adds two deterministic boundary cases; it requires fresh
final discovery rather than reusing that earlier result.

Packaged source and documents are frozen before the final local full discovery.
The post-freeze results, exact manifest/archive hashes, extraction/provenance
receipt and current checkpoint are written only to the unbundled
build/execution-efficiency/regression-remediation/handoff.json, avoiding
manifest self-reference or editing source during checks. A worker restriction
is not a successful host gate. No local fixture result replaces frozen host
acceptance or actual Codex proof.

## Exact unchanged host source acceptance

The scheduler's acceptance argv remain exactly:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-efficiency-requalification-20261120/acceptance.py regression
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-efficiency-requalification-20261120/acceptance.py delivery
```

Origin must requalify this exact final manifest through every frozen successor
source mode: runtime, responsibility, worker, checkpoint, verification, metrics,
drift, regression and delivery. Use the same interpreter and successor
acceptance.py, replacing only the final mode argument as defined by that frozen
wrapper. Preserve original receipts; the local copies retain previous failures.
Any non-restriction failure remains actionable and cannot be waived.

host_sample remains unresolved. The successor wrapper forbids --run-origin.
The following previously recorded origin argv is **NOT authorized now**:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py host_sample --run-origin
```

Its exact conditional underlying argv is also **NOT authorized now**:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python scripts/verify_execution_efficiency_local.py --output PATH --existing-profile --max-turns 12
```

After source qualification, origin must route a separate explicit fresh-trial
decision through the hub, review this manifest and arrange an evidence-preserving
handoff before any invocation. Do not overwrite failed logs, replay a claim,
infer authority from a warm session or reset historical attempts/accounting.
The old 12-turn/1200-second ceiling is not a fresh allowance. The original full
nine conditions, all routes/compatibility/fault qualification, exact same-profile
actual Codex proof, complete portable source and migration/rollback remain.
Keep the predecessor incomplete, this runtime inactive and the newer active
compact-handoff controller/live policies unchanged. No Git publication.
