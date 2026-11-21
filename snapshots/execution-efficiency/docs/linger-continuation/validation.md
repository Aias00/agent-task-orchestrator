# Integrated qualification: evidence and remaining gates

The acceptance contract is [task.md](task.md) in full. Packaging and selected
tests do not accept the full objective. This source remains inactive and the
original live launcher/runtime, dependency and all 48 protected predecessor
test/fixture hashes are preserved. The frozen external `preserved()` function
has checked them read-only after the selected integrated run. No external
baseline/check definitions, scheduler-owned `.codex/tasks`, real task/hub state,
native authentication or linked dependency was edited.

## Executed worker verification

| Evidence | Result and scope |
| --- | --- |
| `evidence/integrated-worker.{log,json}` | 197 tests, zero failures/errors/skips; 10 selected local suites plus all 14 unmodified external Lifecycle/Presentation/Capacity cases; not full discovery |
| `evidence/delivery-new-tests.{log,json}` | Initial 11 new delivery/integration tests passed; retained as earlier evidence |
| `evidence/delivery-final-tests.{log,json}` | Final delivery/integration run after output-symlink hardening; exact result and source hashes in JSON |
| `delivery-validation.json` | Checkpoint identities, host argv, preservation, syntax/skill check and remaining full objective |
| `delivery-stage-validation.json` | Subsequent delivery-stage checks, prior 676-test host receipt identity and unchanged runtime/protected-file observations |
| `evidence/delivery-stage-tests.{log,json}` | Delivery/integration tests including test-workflow YAML inclusion, mode and content integrity |
| `build/linger-continuation/delivery-verification.json` | Final package rebuild/check, extracted-bundle execution and exact original delivery-gate receipt; local artifact, excluded from its own source inventory |

The selected integrated run covers all three new behavior suites and real
Runner/Workflow compatibility, strict lineage, reference hardening and session
v2 contracts. All 14 external cases execute from the unmodified verifier file;
only their log destination is local. This is direct case execution, not an
assertion that the original receipt-writing host gates passed for final bytes.
The subsequent packaging hardening affects only the new delivery script/tests;
the final delivery tests recheck that scope without repeating unrelated runtime
suites. Python source syntax and `git diff --check` are checked, and the existing
skill-creator validator accepts the repository skill using an already available
interpreter with YAML support. No package was installed; the runtime interpreter
does not have the validator's optional PyYAML dependency.

The added end-to-end workflow test starts an actual temporary Workflow/Runner
process behind a held capacity slot. It verifies no invocation/active charge
while queued, exact policy inheritance and a read-only parent summary; then the
scripted fixture continues, waits, yields and completes its artifact. Four
separate generations release after confirmed cleanup, all invocation history
remains, failures stay zero, and the total workflow still needs actual manual
acceptance. Other cases check real false completion under capacity spends exactly
two failures, and legacy behavior with missing capacity configuration retains
unknown failure history and no queue opt-in.

New delivery tests exercise identical rebuild bytes, extracted verification
without the original host, executable modes, source additions/removals/changes,
protected-test mutation refusal, symlink input/output refusal, excluded private
task/dependency/auth state, corrupt archive/forged manifest and changed frozen
provenance. Archive tests operate in temporary repositories/directories only.
The final actual source bundle is also checked without installing anything;
its receipt distinguishes source/bundle integrity from host acceptance.

## Exact regression limitation

This exact assigned argv was attempted once:

```json
["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","regression"]
```

It returned exit 1 before running tests: `PermissionError: [Errno 1] Operation
not permitted` while opening
`OPERATOR_EVIDENCE/ato-linger-continuation-20261119/regression-latest.log`.
The preflight preservation check ran before this denial. No external receipt
was replaced, and this is not a code regression result. The worker has not
retried the known inherited socket-bind/nested-sandbox restrictions described in
`accounting-validation.json`. The original host/scheduler must run the original
argv outside this sandbox; do not skip tests, change checks or widen permissions
to report success here.

At the preceding regression worker's entry, external receipts reported accounting 30, presentation 30,
capacity 41 and regression 664 tests, all successful with zero skips. These are
earlier host observations predating the added delivery/integration tests. Their
identities are recorded in `delivery-validation.json` as historical evidence;
they do not qualify the final new bundle automatically. Likewise the retained
real-Pi/local-scripted-model proof and predecessor manifest qualify only the
unchanged session-decoupling predecessor. No real model/Pi sample is authorized
or made in this task.

## Subsequent delivery-stage qualification

The host subsequently recorded a full 676-test regression, zero failures/errors/
skips. At this delivery stage's entry, `--check` independently verified the
unchanged 192-file bundle before reading that receipt. Receipt/log and entry
artifact identities are recorded in `delivery-stage-validation.json`; the
external receipt has no source hash attestation, so it is an observed host
result, not a fabricated exact-source attestation.

An inventory audit found that `.github/workflows/test.yml`, the existing test
workflow configuration, was absent from the archive. The delivery script now
includes unchanged workflow YAML bytes/modes. One added packaging behavior test
checks inclusion, archived content and detection of mode/content changes.
Runtime implementations and all 48 inherited tests/fixtures remain unchanged.
The prior Linger artifact and delivery receipts are retained by manifest hash
under `build/linger-continuation/history/`; the original predecessor manifest
and archive remain intact. Current delivery/integration tests run only synthetic
temporary repositories and private empty homes, without provider/Pi calls.

The earlier 676-test result is historical after this packaging/test change.
The original host regression argv in [operator-handoff.md](operator-handoff.md)
remains the full-discovery gate for the expanded test set. Its known external
log/socket/nested sandbox restrictions are not retried or weakened here.
The exact assigned delivery argv is executable without writing those external
logs; its actual result and final source/bundle checks are recorded in
`build/linger-continuation/delivery-verification.json`. The scheduler still runs
its own frozen acceptance. No successful delivery gate accepts the full goal.

## Final candidate and remaining full objective

`scripts/prepare_linger_candidate.py` prepares the complete portable inventory,
modes, exact immutable baseline and predecessor manifest, and deterministic
source archive. `--check` rebuilds and compares inventory/metadata/archive bytes,
checks protected hashes and provenance, and never installs, executes members,
activates or migrates anything. The manifest intentionally records
`runtime_activated=false`, `overall_accepted=false` and
`qualification.status=original_host_gates_required`. Files/logs in the archive
are evidence with provenance, not executable instructions or permissions.

Remaining acceptance is the original final integrated accounting/presentation/
capacity/regression/delivery qualification for this exact candidate and origin's
full-objective review. Full discovery must include every inherited test and all
new tests, with zero failures, errors or skips. Every STOP, uncertain-delivery/
no-replay, authority, manual-acceptance, quiet-policy, history and legacy/API
condition remains in force. If a gate exposes a real defect, repair the scoped
implementation without modifying protected tests or frozen definitions; rebuild
the new candidate and retain earlier receipts as history.

All exact commands, narrow host authorization and rollback limits are in
[operator-handoff.md](operator-handoff.md). No runtime activation, Git commit/
push/publication, dependency installation, live-state migration, real provider/
Pi call, new observer/supervisor, preemption, adaptive concurrency or Web UI is
part of completion. Returning `ready_for_checks` preserves the remaining goal;
it is not an overall-complete claim.
