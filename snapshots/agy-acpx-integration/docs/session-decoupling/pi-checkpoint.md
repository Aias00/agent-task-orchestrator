# Managed Pi coordinator stage checkpoint — 2026-11-19

Only the assigned Pi coordinator lifecycle stage was implemented. The complete
six-stage goal, remaining neutral CLI human interaction, integrated fault/full
regression qualification, real-Pi local-only host smoke and reversible candidate
delivery remain in the unchanged `task.md`. This checkpoint is not total-task
completion and does not claim frozen host acceptance passed.

## Saved source

- `pi_session_coordinator.py`: explicit foreground lifecycle, persistent native
  identity and immutable index, fenced multi-turn host admission, private bounded
  event journal, authenticated control socket, owned process-group cancellation,
  exact workflow/STOP/owner checks and explicit checkpoint reconstruction.
- `pi_session_profile.py`, `pi_coordinator_exec.py`, `pi-coordinator-guard.mjs`:
  fixed Pi 1.5.2/pinned pi-acp launch construction, empty private configuration,
  scripted loopback model/dummy credential only, exact file tools and permissions,
  no shell/task-management tool, private-path/symlink/STOP/generation guards.
- `agents.py`: registers the separate `pi_acp` coordinator without starting it;
  existing `pi` and `acp_pi` workers remain separate.
- `session_router.py`: emits v2 references for the coordinator and validates
  additive checkpoint-successor mappings for future origin work. Original
  references and canonical historical mailbox endpoints remain unchanged.
- `tests/test_pi_session_coordinator.py` and the new
  `tests/fixtures/pi_coordinator_fixture.py`: **19** required behavior tests,
  including actual local ACP subprocess fixtures with Codex unavailable.
- `pi-coordinator.md`: lifecycle/operator/API and safety/recovery contract.

Native load is **unsupported**, even though pinned ACP advertises it. Recovery
uses a new native session explicitly labelled `checkpoint_reconstruction`,
retaining the full objective, verified current files, previous results, cumulative
history, old reference and old journal. Unknown prior delivery/effects cannot
be reconstructed or resent. Separate host and origin credentials cannot become
human decision authority. Agent end_turn never changes workflow acceptance.

The trusted foreground entry point and async host API are ready for the later
local-only actual-CLI harness. This stage does **not** call that harness or
prepare a fake real-Pi proof. The final delivery stage still owns
`scripts/verify_pi_session_local.py` plus its neutral-hub roundtrip and original
origin-host smoke contract.

## Worker evidence and host restriction

The source-ready offline diagnostic selection ran **18** Pi behavior tests,
zero failures/errors/skips. It deliberately reports the remaining host-required
socket case separately; it is not the frozen acceptance suite. Receipts:
`build/session-decoupling/pi-offline-selection.{json,log}`.

The unmodified complete Pi suite was attempted when it contained 15 tests:
14 passed and the private control-socket case failed at
`asyncio.start_unix_server -> sock.bind` with
`PermissionError: [Errno 1] Operation not permitted`. Three further tests and one
separate-role test were subsequently added/verified (final required count 19).
The known socket restriction was not retried, bypassed, skipped or converted to
a successful test. The required socket test is still in the frozen host suite.

Final-source neighboring compatibility selection: **168** tests, zero
failures/errors/skips, covering v2 contracts/transport/recovery, Codex adapter,
agent registry/transport/lifecycle and ACP outcomes. Receipt:
`build/session-decoupling/pi-compatibility.log`. An earlier combined selection
of these and the initial 13 Pi tests passed 181 tests; this is historical local
evidence, not a full-regression result. No inherited detached scheduler E2E or
native sandbox test was launched for this stage.

All **38** externally baselined inherited test/fixture hashes still match, and
the eight-core-module vendor-import AST boundary passes. A source fingerprint
and truthful qualification summary are retained in
`build/session-decoupling/pi-source-checks.json`.

No external checker/baseline, assigned `.codex/tasks`, other checkout, live
launcher/policy/runtime/service, real hub/origin, native auth, installed skill,
node_modules target, retry/supervision ceiling or historical directory was
modified. No package installation/download, real model/provider call, subagent,
new scheduler, detached implementation job, Git commit/push or runtime activation
was performed.

## Exact remaining stage check

Return `ready_for_checks` for the scheduler to execute the original command on
its host, outside this worker's socket restriction:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py pi
```

That command must run all 19 tests, including private socket/control identity,
with zero failures/errors/skips and recheck inherited hashes. This stage does
not return `needs_host`; the actual-Pi/local-model final delivery smoke remains
an origin-owned later-stage action under the frozen handoff contract.
