# Final-stage checkpoint, with full objective retained

Assigned stage: qualify the integrated local candidate and reversible delivery.
Full scope and final criteria remain [task.md](task.md). This checkpoint is not
overall acceptance and does not modify the scheduler's frozen task/state/checks.

The accounting, quiet query and durable capacity implementations are retained.
This stage adds `scripts/prepare_linger_candidate.py`, deterministic inactive
manifest/archive generation with portable hashes/modes and immutable provenance,
ten packaging behavior tests and three actual combined lifecycle tests. It
adds architecture, migration, validation and operator handoff, and updates README
and the repository-local skill without installation. The predecessor manifest/
archive remains unchanged; new provenance is separate. No runtime source
mechanism was changed during this stage.

The 197-case selected integrated run passed without failures/errors/skips; the
new output-symlink guard was subsequently covered by the final delivery test run.
Exact counts, source hashes and earlier host-receipt identities are recorded in
`delivery-validation.json` and the evidence it names. Final artifact identities,
portable extracted checks and original delivery-gate results are recorded in
`build/linger-continuation/delivery-verification.json`. These records must be
read for actual outcomes; packaging itself does not create host acceptance.

The preceding worker could not open the frozen external regression log in its
sandbox. The host subsequently produced a 676-test receipt with zero failures,
errors or skips. This delivery worker observed that receipt after independently
checking the unchanged entry bundle. The external receipt does not contain
source hashes; its identity and the entry bundle identity are retained in
`delivery-stage-validation.json`. It is historical after the edits below.

This delivery pass found that the existing repository test workflow YAML was
omitted from the portable inventory. The packaging script now hashes and checks
its unchanged bytes and mode. One additional behavioral test verifies inclusion,
archive content and mutation/mode detection. No runtime mechanism, inherited
test/fixture, CI definition or dependency was changed. The previous Linger bundle
and receipt are preserved under `build/linger-continuation/history/`, keyed by
the entry manifest SHA256. The predecessor bundle remains unchanged.

Current delivery/integration test results and preservation observations are in
`delivery-stage-validation.json`; final artifact identities, portable extraction
and original delivery argv results are in
`build/linger-continuation/delivery-verification.json`. The assigned scheduler
gate remains exactly:

```json
["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","delivery"]
```

The full regression argv remains available to the original host for the changed
packaging/test set; do not retry its known external-log/socket/nested sandbox
restrictions in this worker:

```json
["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","regression"]
```

The full remaining objective is final integrated frozen accounting,
presentation, capacity, regression and delivery gates, all tests with zero skips,
plus the origin's complete original-criteria review of this exact inactive
candidate. Preserve 48 protected hashes and every legacy count/API/schema,
session-decoupling authority, exact owner/generation/session/turn fences,
STOP/unknown-delivery/no-replay, canonical user/manual acceptance and quiet
ordinary notification rule. Do not substitute the earlier 676-test host receipt,
an independent passing branch, agent idle/turn completion or successful stage
for that final review.

The host contract is restricted to original isolated frozen
`acceptance.py accounting/presentation/capacity/regression/delivery`; it grants
no activation/install, live hub/task mutation, real model/provider/Pi call,
publication or extra recovery. No credentials or business decision is missing.
Only host-verifiable gates remain after a successful delivery receipt, so return
`ready_for_checks`, not blocked or overall complete. If a real code gate fails,
retain that failure and continue scoped fixes without changing frozen tests.
Rollback preserves all newer data and unknown occupancy; details and original
argv are in [operator-handoff.md](operator-handoff.md) and [migration.md](migration.md).
