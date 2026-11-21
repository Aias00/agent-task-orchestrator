# Qualification evidence and limits

The frozen checker and external baseline are immutable. It verifies all 48
protected inherited test/fixture hashes plus the global ctask launcher before
executing tests. The v2 harness isolates ATO_HOME/CODEX_HOME, strips ambient
actors/threads and refuses unmocked native model calls. This stage did not edit
those artifacts, assigned .codex/tasks, native configuration or another checkout.

## Observed runs

Evidence is local under `build/external-coordinator/qualification/`; raw logs
are deliberately excluded from the portable candidate. No real provider/model,
package fetch, dependency install, login, global entry activation or Git
publication was performed.

| Check | Observed outcome |
| --- | --- |
| Frozen CLI acceptance, final run | 13 tests passed, zero skips (`cli-final.log`) |
| Frozen lifecycle acceptance, final run | 13 tests passed, zero skips (`lifecycle-final.log`) |
| New directed parser/gateway/private-config/bundle/pinned-ACP checks plus production profile environment | 9 tests passed (`qualification-final.log`); includes actual pinned ACP adapter with offline native peer |
| First frozen full regression | 627 tests executed; 11 failures, 14 errors; not a pass (`regression-initial.log`) |
| Worker-feasible inherited/new regression after fixes | 601 tests passed, zero skips; 34 explicitly deferred host tests (`worker-feasible-regression.log`); not frozen full acceptance |
| Actual native sandbox and sockets | Host required; original assertions retained, no skips/fallback |
| Actual Codex ACP, optional fresh AGY, additive entry | Origin only; not executed/claimed by this stage |

The first full run's failures were the known nested sandbox / local socket
restrictions and one in-progress workflow correlation representation difference.
That difference was fixed: canonical parent workflow_dir now supplies both full
path and ID. The frozen lifecycle suite passed afterwards. Inherited native
adapter tests were not changed to accommodate worker Seatbelt restrictions.
The first full result remains recorded, with no rewritten failure history.

The selected follow-up enumerates every deferred test in its log. It avoids
repeated known-blocked nested native sandbox/socket tests and whole inherited
agent/delegation modules containing those tests. These are **not skips in the
frozen suite** and do not count as full acceptance. Host must run all tests,
including the new actual named-native-policy filesystem test.

## Review findings addressed

No unresolved P0 was found in the scoped CLI/lifecycle review. P1 corrections:
replace production outer+native nesting with one named native sandbox and a
bounded restricted app-server gateway; prevent legacy per-turn policy override;
keep control files private and denied to tools; strictly select a pinned final
message instead of concatenating commentary; reject scoped stage authority
expansion; require exact private host dependency paths with code-owned hashes.
P2 correction: populate canonical full workflow correlation. Same-user trust
and partial observation coverage remain explicit architectural limits.

Fixtures use fresh Git repositories, synthetic local_cli proofs, private homes,
real offline subprocess edits and independent command assertions. Actual public
acpx runtime fixtures cover success, failing independent checks, denied
permissions, malformed/ambiguous/schema/truncated/oversized outputs, EOF/timeout,
STOP/group cleanup, concurrency, idempotency, unknown delivery, scope/auth
rejection, manual criteria and privacy. New gateway tests observe real
subprocess translation of the native turn. A further test connects the
actual pinned Codex ACP adapter through public acpx and the gateway to an
offline native peer: named policy/schema survive, commentary is separated from
final text, and the optional goal view is explicitly unsupported. Candidate
tests check deterministic archives, source tamper and symlink rejection. No hardcoded success state or
empty command substitutes for a real check.

The new actual native filesystem test uses the same generated named profile,
checking authority read/write denial, symlink aliases, protected Git/ATO paths,
ordinary writes and read-only behavior. It contains no model prompt or login.
The worker does not rerun this known nested-sandbox operation. Host evidence is
required before the one remaining Codex ACP admission. Native metadata/policy
failure remains fail-closed and cannot be treated as a transport success.

## Candidate and independent host commands

```sh
"$PYTHON" scripts/prepare_external_candidate.py prepare
"$PYTHON" scripts/prepare_external_candidate.py --check
```

The deterministic candidate includes source, documentation, fixtures, exact
file hashes/modes, frozen baseline provenance and manifest; it excludes state,
credentials, node_modules, logs, symlinks, .git/.codex/.agents and old build
outputs. `--check --archive /absolute/path/candidate.tar.gz` works in an extracted portable source tree without the
original Git metadata, original external ops path or installed dependencies.
The archive stays outside its own contents; either provide `--archive` or place
the original archive beside the extracted manifest. It verifies source hashes/modes, protected baseline and every archive member
without extracting/executing it. The archive SHA256 is a separate sidecar to
avoid a manifest/archive hash cycle. Any source/doc/test edit requires prepare.

Frozen qualification commands (scheduler runs outside worker restrictions):

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-agy-acpx-integration-20261119/acceptance-v2.py regression
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-agy-acpx-integration-20261119/acceptance-v2.py bundle
```

After these pass, origin separately follows [operator-handoff.md](operator-handoff.md)
for remaining actual admissions, readback, additive entry and external host gate.
Stage readiness and transport end_turn never establish total-task delivery.

## Subsequent origin-delivery worker review

The scheduler's preserved qualification evidence records **636 full host tests
passed**, zero failures/errors/skips, plus the bundle gate, before this review:
`.codex/tasks/20261119-232116-33b86ef1/evidence/qualification-final-e91804-check-0.log`
and `qualification-final-e91804-check-1.log`. This worker only read those logs.
That pass does not qualify later source changes automatically.

The origin review found remaining metadata-read, tool PATH and smoke PID-reuse
issues; [origin-delivery-handoff.md](origin-delivery-handoff.md) documents the
source corrections and actual host sequence. On the revised source, 33 directed
worker-feasible transport/profile/parser/gateway/cleanup/bundle tests passed with
zero skips (`origin-review-fixtures-final.log`), and frozen CLI/lifecycle gates
each passed 13 tests (`origin-review-cli.log`, `origin-review-lifecycle.log`).
The first directed run's two helper-import errors remain in
`origin-review-fixtures.log`; module/CLI imports were corrected and the entire
33-test selection passed afterwards. `origin-review-cleanup-final.log` also
records the focused two-test cleanup pass. No original failure log was replaced.

Two native sandbox checks were explicitly deferred from that selected run,
never skipped/removed from frozen acceptance: the inherited outer fixture and
the revised actual native named-policy boundary test. Host must rerun all
regression tests (now two additional cleanup tests) and bundle, including native
Git/Node/authority checks, before using the one remaining real Codex admission.
This worker did not install/activate, run models, read native credentials, write
external origin-delivery proof or execute/claim the final host delivery gate.
