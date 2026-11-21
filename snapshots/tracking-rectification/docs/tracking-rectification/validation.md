# Unified inactive candidate validation

This is the complete five-fix source candidate, prepared in the isolated
tracking checkout. It is inactive. Stage success is not overall acceptance;
the fixed full regression and bundle gates belong to the scheduler, and the
actual-record proof belongs to the final dedicated origin stage.

The fresh packaging-stage directed runs used the prescribed Python interpreter,
private temporary `ATO_HOME` / `CODEX_HOME`, and synthetic identities:

| Directed module | Tests | Failures / errors / skips |
| --- | ---: | --- |
| `test_tracking_workspace_binding` | 21 | 0 / 0 / 0 |
| `test_approved_delivery` | 43 | 0 / 0 / 0 |
| `test_internal_coordination` | 25 | 0 / 0 / 0 |
| `test_tracking_responsibility` | 56 | 0 / 0 / 0 |
| `test_completion_lineage_dedup` | 31 | 0 / 0 / 0 |

Measured results and logs are `evidence/package-test_*.{json,log}`. These are
worker fixture results, not a substitute for independent acceptance. Earlier
stage receipts/logs are preserved with their original scope and timestamps.

`test_tracking_candidate` adds portable rebuild/extraction and fault checks,
plus synthetic end-to-end origin projections and audit-fence refusal tests.
Its final counts and the compatibility selection are recorded in
`evidence/package-qualification.json`. No original test/fixture was edited.

The full inherited and new regression must pass with **zero failures, errors
and skips** on the original host. It has not been claimed passed here. The
known worker restrictions on nested native sandboxes and local sockets are
documented in the inherited `docs/session-decoupling/validation.md`. The broader worker selection included these fixture groups: 499 ran, 488
passed, nine failed and two errored, zero skipped. Its unsuccessful result is
retained as `evidence/package-compatibility.{json,log}`. The failures are in
`test_agents.AdapterTests` and the socket-backed delegated-permission fixture;
no assertion or runtime sandbox was loosened. They will not be rerun here. The scheduler
must run the unchanged external commands:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/acceptance.py regression
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/acceptance.py bundle
```

## What is included

The packager includes source, all inherited and new tests/fixtures, docs,
synthetic test evidence, dependency lockfiles and immutable seed/acceptance
provenance. It excludes symlinks, runtime/task state, private histories,
credentials, dependency trees and old build archives. It neither invokes Git
nor reads a live home. `--check` compares every source byte, protected inherited
test, normalized file mode, manifest byte and deterministic archive byte.

`build/tracking-rectification/candidate.json` contains the complete SHA-256 file
mapping and `runtime_activated=false`. The detached
`build/tracking-rectification/hashes.json` records the **exact manifest SHA-256
and archive SHA-256**. Keep it beside the archive; its hashes are verified by
`--check`. The archive includes its own manifest. The three final delivery docs
contain the exact implementation identity below; their own bytes are separately
pinned in the full manifest. This avoids a circular document/manifest self-hash.

Implementation SHA-256: `0f2caac929606b375922ee9cd796ce335eb1032d1a44853674620d4da607a8e4`

Provenance is copied byte-for-byte from the fixed external `baseline.json` and
`acceptance.py` into `provenance/`. These copies are evidence, not a replacement
or modification of the scheduler's frozen gates. All 49 inherited files and
five external launcher/guard/skill/helper/efficiency-manifest pins must retain
their fixed hashes. Verification details are in `evidence/package-qualification.json`.

## Reproduction without a live home

```sh
python3 -B scripts/prepare_tracking_candidate.py prepare
python3 -B scripts/prepare_tracking_candidate.py --check
```

For review elsewhere, extract the archive into a fresh temporary directory
using a safe tar extractor, then run its copy of the checker:

```sh
python3 -B scripts/prepare_tracking_candidate.py --check --archive /absolute/original/candidate.tar.gz
python3 -B scripts/prepare_tracking_candidate.py prepare
python3 -B scripts/prepare_tracking_candidate.py --check
```

The rebuilt manifest/archive must match the originals exactly. No dependency
install is needed to prepare/check the source archive. Running the full tests
separately requires the original pinned Python dependencies and read-only Node
dependencies; the archive deliberately does not vendor installed environments.

The origin-only probe is prepared and fixture-tested, **not executed against
real records by this worker**. `operator-handoff.md` describes the exact plan,
120-second fence, immutable snapshots and final proof. No activation,
model/provider/queue request, business deployment or user notification occurred.

The original efficiency task retains its separate full objective and real-CLI
qualification. Its prior native-profile failure and claim remain historical
evidence; this candidate neither resamples it nor changes its manifest.
