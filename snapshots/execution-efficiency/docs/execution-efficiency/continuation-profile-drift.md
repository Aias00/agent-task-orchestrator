# Source-only profile evidence continuation (2026-11-20)

This stage follows CONTINUATION.md and the origin receipt at
`OPERATOR_EVIDENCE/ato-efficiency-requalification-20261120/continuation.json`.
The actual user authorized source-only handling of the known qualification
blocker. **ZERO fresh real/native/model trials are authorized.** All nine
requirements in task.md survive unchanged. No feature is activated, installed,
committed or published; the newer active compact-handoff controller and live
notification policies are not replaced by this inactive candidate.

## Retained failure and evidence limits

The predecessor candidate manifest was
`4ff7510adf12c684a920717c470304c68f235847340344e6fb6538adf1a1414a`.
Its exact manifest is retained at `evidence/continuation-predecessor-candidate.json`;
the original portable archive is retained locally at
`build/execution-efficiency/pre-continuation-candidate.tar.gz`. The original root
and native failed claim/log remain untouched. Its actual-cli-proof.log stopped
after the first foreground native turn with
`native profile changed during turn`. The old before/after profile values were
not retained. The exact cause remains **UNKNOWN**: config, binary, environment,
model, grant and source fields were all part of that profile. The message does
not prove config drift. The predecessor workflow remains needs_host; stages,
attempts, elapsed time, failures, claims and native evidence are historical and
are neither reset nor relabeled by this stage.

The Linger677/2256534 baseline and every inherited test/fixture byte remain
protected. The successor's origin receipt lists 61 inherited test/fixture files,
including the prior efficiency suites. The original baseline still separately
protects its 53 files. These counts describe different scopes, not added tests.

## Minimal source correction

`profile_evidence.py` records fixed field labels, SHA-256 digests and enums only.
Unknown field names and config path keys are hashed. Config entries also retain
bounded absent/present enums, distinguishing materialization from a content
change without treating either as safe after admission. Full profile hashes cover
all fields; at most 128 field entries and 32 changed-field labels are displayed,
with explicit truncation. Truncation cannot permit admission: full original
Python profile equality remains mandatory. No config body, environment value,
prompt, native transcript or exception text enters these diagnostics.

The sample engine saves an atomic private pre-launch record for every turn in
`profile-evidence/ARM-INDEX-turn-N.json`, outside the temporary model-writable
sample repo. It saves a post observation on normal and exceptional exits,
including receipt/check failures. An unreadable post observation is unknown
and prevents success. An unchanged profile status alone is not delivery or
acceptance; execution_raised and the existing native/check predicates still
govern those outcomes. A killed host can leave a prepared record with post=null;
that is not a completed observation. The existing per-manifest exclusive claim
and failed/unknown no-replay journal remain intact. Successful proofs list the
profile record hashes alongside the existing native/check evidence.

Runner publishes the same bounded observations into its existing private
worker-session ledger before launch and after delivery. Accepted turns retain
their individual pre/post observations. Failed turns retain their observation
with delivery_unknown, generation, correlation and cleanup state. Native
model/grant/context mismatches attach hash-only expected/observed diagnostics;
the original native identity, source, message, turn and effective-profile
predicates still apply. Host checks cannot promote a claimed/unknown turn.

A directed synthetic test creates a missing config during the first turn. It
reproduces the class of profile drift and stops before turn two. This does not
identify the old cause. Startup-generated config is never whitelisted after
admission. No automated native-home preparation, warmup native call, copy of
user home/auth/config, new sandbox or permission widening was added. Existing
ATO-only isolation remains; any proposed task-owned native-home preparation
requires a separate explicit reviewed origin operation and evidence that its
model/auth/grants match. Unknown capabilities still fail closed.

## Verification and remaining gates

`tests/test_profile_drift_evidence.py` uses temporary Git repos and a copied
synthetic subprocess CLI only. It exercises the actual sample/Runner boundaries
for config, binary, environment, model, permissions and sources; startup config
creation; native effective model/grant mismatch; both successful turns; private
content exclusion; unreadable post state; failed receipt; bounded diagnostics;
read-only capture; unknown no-replay/host-promotion; and retained per-manifest
claims. Initial failures are retained in build/execution-efficiency, outside
the portable source archive. Current results are in continuation-checkpoint.json.

Independent host checks use the immutable successor wrapper, which preserves
the original acceptance code and disallows --run-origin in this continuation:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-efficiency-requalification-20261120/acceptance.py drift
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-efficiency-requalification-20261120/acceptance.py worker
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-efficiency-requalification-20261120/acceptance.py delivery
```

Origin also reruns all immutable successor source gates: runtime,
responsibility, worker, checkpoint, verification, metrics, regression (>=759,
zero failure/error/skip), delivery and drift, against this final manifest.
Packaging/extraction must qualify these exact bytes. No fixture result is an
actual native proof or provider speedup. The ninth original gate host_sample
stays unresolved until a separately explicit fresh-trial decision is obtained
and reviewed through the hub. This source continuation grants no such decision.

For that future decision only, the original origin-owned gate is precisely:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py host_sample --run-origin
```

Its underlying sample argv is:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python scripts/verify_execution_efficiency_local.py --output PATH --existing-profile --max-turns 12
```

Do not run either under the current authorization or blindly reuse a wrapper
that would overwrite old actual-cli-proof.log. Origin must first arrange a
reviewed evidence-preserving handoff for this exact new manifest, retain the
old failed claim/log untouched and decide whether any fresh trial is allowed.
The original upper bounds (three arms, two samples each, two turns each,
12 turns/1200 seconds maximum, same configured profile/grants, temporary Git
files only) remain ceilings, not a new allowance. No failure/unknown is replayed.
Successful actual proof must bind the exact final manifest. All nine conditions
and full Runner/Workflow/Controller/CLI compatibility remain required for the
total task. No predecessor or total-task completion is claimed here.

## Reproduce the inactive source bundle

This copied checkout already contains frozen portable baseline and predecessor
provenance. It omits the older Linger build output at
`build/linger-continuation/candidate.json`; reconstruct from the bundled verified
provenance with the existing packager functions, without synthesizing an old
manifest or changing external baseline files:

```python
from scripts import prepare_efficiency_candidate as bundle
manifest = bundle.build()
bundle.atomic_write(bundle.ROOT / bundle.MANIFEST, bundle.encoded(manifest))
bundle.atomic_write(bundle.ROOT / bundle.ARCHIVE,
                    bundle.archive_bytes(bundle.ROOT, manifest))
```

Then run `scripts/prepare_efficiency_candidate.py --check`, extract the archive
into a fresh temporary directory preserving recorded modes, and run its copied
`scripts/prepare_efficiency_candidate.py --check-extracted`. The archive excludes
dependencies, task state, native/private evidence and the locally preserved old
archive. It includes the exact prior 4ff7510 manifest as historical provenance,
not as successful current qualification. Candidate hashes and local packaging
results live in `build/execution-efficiency/continuation-handoff.json`; that file
is outside the archive, avoiding a manifest self-reference. Historical feature
checkpoints retain their original scope and do not supersede the current
source-only continuation or grant a new native sample.
