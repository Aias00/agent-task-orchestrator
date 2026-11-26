# Qualification evidence and limits

This document describes the isolated session-decoupling candidate, not the live
runtime. The immutable full task is [task.md](task.md); external baseline and
acceptance.py remain unchanged. Historical stage checkpoints retain their own
scope and are not final acceptance. The scheduler independently runs the frozen
host checks; no worker result is substituted for them.

## Current worker verification

The first complete exploratory run executed 550 inherited and earlier-stage
checks with isolated HOME, ATO_HOME and CODEX_HOME and zero skips. It exposed
shared legacy account context pollution, explicit terminal revalidation/resume
compatibility, a stale controller notification path, and worker OS restrictions.
That failing log is retained as `build/session-decoupling/regression-worker.log`;
it is not presented as successful qualification.

The final integration repairs keep existing tests/fixtures byte-identical:
account-specific configuration and mailbox isolation under explicit paired roots;
foreground terminal revalidation after the existing lock/STOP checks without
allowing automatic terminal recovery; and a stale follow-up recorded without
minting a new-generation envelope. No retry ceilings, attempts, frozen contracts,
old directory names or supervision budgets change.

A directed 104-check exploration after the context fix left just the explicit
STOP-resume and stale notification findings. Both were then fixed. A targeted
14-check run covering those inherited regressions plus 12 new qualification
fault/package/protocol tests passed with zero skips. The integrated full result
is retained in `build/session-decoupling/regression-result.json` with its complete
log `regression-final-worker.log`. The final checkpoint lists exact observed
counts and unresolved host-verifiable restrictions; it must be read before
claiming a pass. Directed session suite results are retained separately.

New delivery tests exercise configuration isolation, authority-preserving
foreground revalidation, STOP/closure and role refusal, stale notification
no-send/deduplication, protected-test tampering, new-source inventory, symlink and
private-state exclusion, bounded six-response local-model wire output and
sanitization of retained native evidence. They run no actual Pi/provider model.

## Retry qualification checkpoint

The resumed worker inspected the prior attempt and retained every earlier log.
The prior `regression-result.json` records 562 tests, 11 failures, 17 errors and
zero skips; it is a failing result, not acceptance. The final attempt ended with
a model transport disconnect before returning a structured outcome.

The retry repairs the legacy once-only terminal failure notice compatibility
path beneath the session router. Exact native binding is still required. Its
notice explicitly says origin activity can be unknown; it never grants recovery
or treats that uncertainty as idle. Neutral v2 activity-unknown behavior remains
unchanged and suppresses false unhandled-failure classification. Dispatching and
unknown recovery deliveries suppress this legacy notice too. The implicit origin
record now explicitly describes the current runtime value as only a locator,
requiring native binding before use. Existing protected tests are unchanged.
A new behavioral test verifies uncertain legacy notices, deduplication after
presentation, and the unknown-delivery barrier. The targeted compatibility,
qualification and v2 recovery run passed 70 tests, zero skips
(`retry-targeted.log`).

Directed retry reports are `retry-{contracts,codex,transport,recovery,pi,hub}.json`
and matching logs under `build/session-decoupling/`. The complete retry report is
`retry-full.json`; the checkpoint JSON contains exact counts and classifies the
remaining restrictions. The OS-denial fixture receipt in
`retry-sandbox-denial.log` shows `sandbox-exec: sandbox_apply: Operation not
permitted` before the synthetic agent executes. The Pi control-socket test also
gets `PermissionError` at bind. No test was skipped or sandbox protection removed.
The complete retry ran 563 tests: 540 passed, 9 failed, 14 errored, zero
skipped. All 23 unsuccessful checks belong to the nested sandbox worker tests
or local socket fixtures; all disposition/notification regressions now pass.
This is not a passing regression. The full frozen regression must still run on
the independent host.

The external verifier's read-only `preserved()` and `boundary()` checks passed
in the worker: live launcher/runtime/guard, inherited tests/fixtures and pinned
dependency hashes match; core modules pass the vendor import boundary. Python
source AST checks passed. These do not substitute for behavioral acceptance.

## Evidence levels and remaining host gate

The final delivery worker adds `tests/test_session_delivery.py`: 14 offline tests
for native file-result/call-ID checks, failed-tool refusal, exact native identity,
project/canonical question/generation/turn correlation, missing/duplicate terminal
evidence, Codex static-alias/database/app-server fail probes, actual candidate CLI
child probe loading, deterministic archives and content/mode/source drift.
`delivery-offline.json` and `delivery-offline.log` record the combined 49-test run
of delivery, existing qualification and neutral-hub suites, with zero failures,
errors or skips. No actual Pi CLI/model was run. The final receipt check additionally
applies typed native observations to the canonical MessageBus for all three turns.
The full regression must include the new suite on the host; previous full-suite
counts above remain historical, not a result for the final candidate.

The repository skill now distinguishes Pi worker from the managed `pi_acp`
coordinator and documents neutral human roles, reconstruction and the origin-only
handoff. The skill-creator validator could not run because the existing Python
environment has no PyYAML; no package was installed. Standard-library checks verify
the simple required frontmatter and repository documentation links instead.

Earlier/new session protocol tests use actual local subprocess fixtures and
explicitly synthetic adapters/users. The final smoke's HTTP protocol is unit
qualified offline. Preparing this script does not establish actual CLI success.
Worker checks cannot claim the origin-only actual-Pi/local-model gate, native
load, real cloud model behavior or user acceptance.

Origin must run the exact frozen external regression, the directed modes required
by the full task, `real_pi` and `delivery` as described in operator-handoff.md.
Only the existing host handoff authorizes that one <=240-second local smoke.
It uses actual installed Pi and the pinned adapter with a scripted loopback
model, empty private native configuration, dummy credential, synthetic user
answers and Codex fail probes. It retains safe per-step/event/tool/artifact proof.
No external provider, real hub/origin/config, auth extraction or runtime activation
is involved. `end_turn` never satisfies the synthetic workflow acceptance.

## Candidate identity and preservation

`scripts/prepare_session_candidate.py` records the external actual-live baseline
SHA256, all portable source/tests/docs/skill/script hashes and modes, actual-live-to-candidate
delta, seeded Git head, the original seeded dirty diff hash and qualification-stage
delta against `stage-start.json`. `--check` independently reconstructs the exact
inventory and validates protected test hashes. It rejects changed/missing files
or symlinked source. Build evidence, node_modules/dependency link targets, private
configuration, native auth/rollouts, task state, SQLite and Git internals are not
part of the portable file set. `--archive` writes a deterministic tar.gz containing
only that file set and the manifest. `--check` verifies the current inventory and
existing archive without extraction. New archive tests prove byte reproducibility
and reject changed source, altered content and executable-mode mismatches.
The read-only pinned dependency hash is checked
by the frozen host verifier and trusted Pi launch profile.

`runtime_activated=false` is explicit. The existing launcher, runtime files,
queue guard, baseline/test hashes and dependency are checked read-only. Candidate
preparation/checking does not install anything, change the frozen workflow state,
or publish Git. Reversible executable selection and the old-reader v2-state
boundary are documented; rollback never overwrites newly written state.

The delivery checkpoint is `build/session-decoupling/delivery-checkpoint.json`;
`delivery-stage-start.json` preserves the incoming manifest hash and inventory.
The final checkpoint retains the original full goal, offline results, exact
candidate/archive hashes and precise origin argv. Its `needs_host` status requests
the already authorized bounded local smoke and frozen checks; it is not a claim
that actual Pi, cloud inference or the full task passed.
