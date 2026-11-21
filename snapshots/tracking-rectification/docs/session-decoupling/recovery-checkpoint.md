# Durable recovery stage checkpoint — 2026-11-19

This checkpoint covers only the assigned recovery stage. The full frozen goal
and remaining Pi coordinator, neutral hub, integrated qualification and candidate
delivery obligations remain in `task.md`. No runtime was activated, no real
session was migrated, and no Git commit, package installation, new supervisor,
observer service, repair grant or scheduler was created.

## Implemented behavior

`session_router` now uses the versioned adapter observation contract for v2
recovery and origin replies. A concrete opaque cursor is obtained before send;
a v2-only adapter can implement `session_cursor_v2(identity)`. Recoverable v2
adapters must actually implement `observe_session_v2`; a legacy observer cannot
silently claim generation-fenced recovery. Existing v1 references and raw UUIDs
retain their established compatibility entry points beneath the router.

`transport.observe_managed` applies normalized native receipts to the existing
canonical mailbox. It verifies exact native session/project, canonical request
or envelope ID, correlation, execution revision and native turn, then applies
the envelope's immutable endpoint, route and dispatch-attempt fences. Wrong
identity/message/generation batches cannot advance the saved cursor. Duplicate
receipts survive reconstruction without another transition or send. Lost
endpoint ownership records late evidence without adopting it as acceptance;
observation never reacquires an expired endpoint owner. Tool/lifecycle events
are observations, not acceptance or workflow completion. Codex's typed event
bridge identifies the exact parsed canonical request as its message identity.

Recovery JSON and controller reply trackers remain projections. The mailbox
establishes accepted/responded/turn-completed evidence, while the recovery
request separately records working/repaired/needs_input/blocked disposition.
Takeover checks canonical delivery rather than trusting a mutable queued
projection. Unknown delivery is never replayed or treated as a claimable
request. STOP, closure, contract drift, stale generation, orphan child work,
live owners, another native origin turn and unknown v2 activity refuse takeover
or replacement execution. A request's own exact accepted turn may claim origin
host work. Host accounting revisions supersede old requests; only a durable new
workflow-start event is recorded as resumed execution. Agent reply/end-turn/EOF
still cannot settle workflow acceptance or a human decision.

The existing mailbox execution lease and repository flock cover foreground
workers, origin repair, authorized host rechecks and session-host native
load/resume. `advance_execution` increases the fence when the same foreground
owner advances generation; old renew/release calls cannot affect that execution.
Workflow start and stage revisions explicitly advance their owner, without
changing attempt or supervision accounting. Host recheck holds an origin lease,
renews through the existing guard, and keeps the original failed row, attempts,
limits, contract, prior evidence and manual criteria. Nested workers retain the
workflow's existing ownership/guard rather than becoming independent owners.

An expired token is not proof that native work stopped. Replacement execution
checks the exact prior task/project/contract and owner/child process evidence;
expired origin/session-host ownership additionally needs verified idle activity
of that prior workflow's origin. Session-host resume requires an actor for the
exact target reference and the workflow's exact bound origin. If its foreground
host exits while native activity is active or unknown, its lease stays
quarantined; expiry does not remove the idle-verification requirement. A
managed coordinator must keep its existing foreground lifecycle and guard over
owned native work. This is not permission to add a restart/repair loop. Native
reconstruction remains the later Pi stage's explicit new-session operation.

`handler_status` distinguishes stopped/closed, live worker, execution ownership,
active origin, unknown activity, a current bounded recovery request, unknown
delivery and unhandled failure. An expired/disabled/stale request cannot hide a
known idle unhandled failure indefinitely. Unknown activity does not prove no
handler. Recovery status exposes this classification and mailbox state. Existing
failure-attention policy creates one stable failure episode. Pending evidence
can refresh after accounting changes under that same ID; a notice or actual
presentation prevents another notice, including under the all-notifications
policy. A terminal recovery alert is attempted once per request. Ordinary quiet
controller updates remain recorded without delivery or presentation. The
missing `workflow_closure` router import was restored so its existing user-only
closure path remains usable; no closure rule was broadened.

## Verification retained in this checkout

- `tests/test_session_v2_recovery.py`: **33** independent behavior tests, zero
  failures/errors/skips. All identities, credentials, events, native effects,
  host checks and repositories are synthetic local fixtures. No real Pi or
  cloud model qualification is claimed.
- Combined contracts, Codex adapter, durable transport and recovery suites:
  **123 tests**, zero failures/errors/skips. Report/log:
  `build/session-decoupling/recovery-selected.{json,log}`.
- Selected inherited foreground compatibility checks: **26 tests**, zero
  failures/errors/skips, with fresh empty private roots per case. These include
  public takeover-status/host-recheck CLI, original failure/attempt preservation,
  recovery no-replay/STOP/grant behavior, once-only failure handling, and one
  foreground planner/worker fixture subprocess. Report/log:
  `build/session-decoupling/recovery-compatibility.{json,log}`. Detached inherited
  recovery E2E/start/watch tests were not part of this bounded selection; this
  selection is not full regression or host acceptance.
- All **38** externally baselined inherited test/fixture hashes are unchanged;
  the eight core-module vendor-import AST boundary passes. Report:
  `build/session-decoupling/recovery-preservation.json`.

The earlier transport checkpoint's exploratory full-regression failures remain
unqualified; this stage does not claim they all passed. Worker socket/nested
sandbox restrictions and the final actual-Pi local-only qualification remain
host responsibilities under the frozen task contract. No external acceptance
logs or baseline/checker files were written by this worker.

## Frozen scheduler handoff

Source and the assigned recovery fixtures are ready for the scheduler's exact
host command:

```
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py recovery
```

`ready_for_checks` means that this host command remains to run. It does not mean
host acceptance or the complete six-step workflow passed. The final real-Pi
local scripted-model smoke remains reserved for the final delivery stage and
its origin-owned bounded handoff; this stage does not request or run it.
