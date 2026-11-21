# Original host qualification and inactive delivery

The complete objective remains [task.md](task.md), not this stage's successful
tests or packaging output. This worker delivers a reviewable local candidate;
`runtime_activated=false` and `overall_accepted=false` remain explicit. No live
task/hub changes, installation, activation, publication or real model/Pi calls
are authorized. Older session-decoupling host instructions are historical for
that predecessor and are not instructions to rerun its real-Pi sample here.

## Exact candidate

Source: `OPERATOR_SOURCE/ato-linger-continuation-20261119`.
Manifest: `build/linger-continuation/candidate.json`.
Archive: `build/linger-continuation/candidate.tar.gz`.
All portable source/test/docs hashes and modes, frozen baseline hash and exact
predecessor manifest hash are recorded. The bundle contains their immutable
provenance bytes and no task/auth/dependency state. Inspect the existing artifact
first; do not regenerate it to bless a mismatch:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python scripts/prepare_linger_candidate.py --check
```

The predecessor manifest SHA256 remains
`cb025a6fad66a0623dc5fbd3d981344f462d9b903a9a8ed8c1a83acd7f1da106`.
Its original archive and externally retained actual-Pi proof qualify only that
unchanged predecessor. This candidate does not make another real-Pi/provider
call, read native private authentication or activate its dependency link.

## Frozen host argv and remaining acceptance

The preceding regression worker could not open the external log in its sandbox;
the host then recorded 676 tests passed, zero failures/errors/skips. This delivery
worker checked the entry bundle before observing that receipt. Its exact receipt
and log hashes are retained in `delivery-stage-validation.json`; the receipt
itself has no source hash attestation. A subsequent delivery-only fix adds the
unchanged test workflow YAML to the inventory and one packaging behavior test.
That prior full regression is historical for the expanded test set. The original
host may rerun exactly this argv from the source root, using the verifier's
isolated temporary homes:

```json
["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","regression"]
```

The assigned delivery gate for this stage is exactly:

```json
["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","delivery"]
```

Local execution of this gate is recorded separately from the scheduler's own
acceptance. `build/linger-continuation/delivery-verification.json` identifies the
final bytes and actual outcomes; no local result claims overall acceptance.
Earlier Linger artifact/receipt bytes are retained under
`build/linger-continuation/history/<entry-manifest-sha256>/`, outside the portable
source inventory. The qualified predecessor originals are also retained.

For final overall qualification, the existing explicit contract also permits
only these original isolated frozen modes, without modifying their definitions:

```json
[
  ["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","accounting"],
  ["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","presentation"],
  ["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","capacity"],
  ["OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python","OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py","delivery"]
]
```

Run full regression once on the exact final candidate. Fix real failures only
in authorized source/new tests, preserve the 48 inherited protected files, and
repeat broad testing only after a relevant change or failure justifies it.
All gates require zero failures, errors and skips. Worker socket/nested sandbox
restrictions belong to the original host, not weakened tests. The scheduler owns
`.codex/tasks`; this worker does not modify it or launch another scheduler.

The authorization source is the user's 2026-11-19 “好的可以继续” following the
Linger assessment and bounded interaction-priority proposal. The explicit host
contract permits ONLY original `acceptance.py accounting/presentation/capacity/
regression/delivery` using isolated temporary configurations to overcome host
socket/nested sandbox restrictions. It grants no activation, installation,
real model/provider/Pi call, live hub/task mutation, publication, extra recovery,
permission expansion, new observer/supervisor, preemption or arbitrary resume.
`ready_for_checks` means those gates remain, not that they passed or the full
objective is accepted. The origin must verify every original criterion and keep
remaining conditions/history if any fail. Manual acceptance cannot be inferred
from a process exit, turn completion, silence or successful independent branch.

## Evidence and rollback

[validation.md](validation.md) distinguishes the selected worker run, external
case execution, previously observed host receipts and exact current host gates.
Source changes after packaging invalidate `--check` and require a new reviewed
artifact; do not overwrite predecessor proof. Preserve logs and attempts even
when a later candidate passes. Querying a task never accepts or resumes it.

No runtime switch has occurred, so retaining this separate directory/archive
retains the entire reversible delivery. [migration.md](migration.md) defines
legacy compatibility and a future separately authorized rollback boundary:
never overwrite newer ledgers/messages/capacity receipts with an older backup,
never feed v2 tasks to a legacy executor, and never free unknown occupancy by
TTL/dead PID or clear STOP. The host gates do not authorize such operations.
