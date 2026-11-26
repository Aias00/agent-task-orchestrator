# Neutral CLI hub stage checkpoint — 2026-11-19

Only the assigned neutral CLI human question/answer and continuation stage is
implemented here. The complete six-step objective and final acceptance remain
in the unchanged [task.md](task.md). This stage passing does not complete the
total workflow. The current global hub, live runtime/launchers/policies, real
sessions, installed skill, external verifier/baseline, inherited tests/fixtures
and scheduler-owned `.codex/tasks` were not edited. No Git publication,
installation, real provider call, subagent or implementation scheduler was used.

## Reviewable stage delta

- `local_session_adapter.py`: built-in passive `local_cli` identities; exact
  project/native identity verification; separate immutable hub/user/origin keys;
  private bounded no-symlink actor-file reads; hashed stored secrets; explicit
  workflow/contract/revision/full-start-generation grants. Root keys grant only
  their fixed role, and scoped keys cannot renew or mint credentials.
- `agents.py`, `runner.py`: trusted built-in registration and actual public CLI
  provisioning/grant, authenticated hub/inbox/read/respond/accept commands,
  explicit presentation source, and bounded foreground `continue-task`.
- `interaction.py`: use existing actor authorization contracts and one canonical
  question/answer ID/mailbox; actual user text plus source and user proof; separate
  presentation/answer/origin-accept audit claims without tokens. Unknown sends
  retain their original barrier and cannot replay through duplicate commands.
- `workflow.py`: verify continuation under the existing repository execution
  lock before acquiring its existing durable ownership lease. Neutral manual
  acknowledgement requires a scoped user credential and evidence. No changed
  counters, ceilings, acceptance commands, STOP clearing or supervision policy.
- `session_router.py`: passive v2 endpoints can transport without advertising an
  unsupported native observer. Local activity stays unknown, and native
  observe/create/cancel/load-resume stay unsupported. Passive admission receipts
  are not observed as human/origin responses or workflow acceptance.
- `tests/test_neutral_hub.py` and `tests/fixtures/neutral_hub_cli.py`: new bounded
  synthetic behavior tests and trusted subprocess bootstrap. Existing tests and
  fixtures remain byte-identical. Older trusted `is_actor` APIs retain a public
  compatibility shim; actual local CLI and managed Pi reject ambient identity.
- `README.md` and [neutral-cli.md](../../session-decoupling/neutral-cli.md): isolated candidate usage,
  real-user source requirements, credential boundaries, CLI examples and managed
  Pi origin-proof integration. No global installation or binding is performed.

## Worker verification

The final new suite ran **22 tests, 0 failures, 0 errors, 0 skips** with the frozen
Python interpreter and temporary HOME/CODEX_HOME/ATO_HOME. Safe evidence:

- `build/session-decoupling/hub-stage/hub-tests.json`
- `build/session-decoupling/hub-stage/hub-tests.log`
- `build/session-decoupling/hub-stage/source-checkpoint.json`

It exercises actual candidate CLI subprocesses, including a complete roundtrip
through unwrapped `runner.py`, authenticated local endpoints, question display,
synthetic user answer, origin acceptance, foreground continuation and independent
artifact checks with Codex unavailable. A separate full workflow uses the bounded
synthetic worker/planner. A managed Pi **ACP subprocess fixture** also uses the
same CLI entry point from its trusted host, origin-only proof, distinct actual-
user fixture credential, a file-tool fixture event and independent checks. The
Pi fixture is explicitly named `fixture_pi_*`, not the actual `pi_acp` profile.
No socket, live native auth, actual Pi or cloud model is needed or claimed by
these checks. Passive/ACP turn completion leaves workflow criteria pending.

Coverage also includes wrong roles/projects/contracts/endpoints, forged tokens,
public/symlink credentials, missing actual user source, root versus scoped hub
binding, full generation fencing, STOP preservation, manual user-only evidence,
conflicting duplicate answers and unknown-delivery/no-replay. All 38 externally
baselined inherited test/fixture hashes were checked unchanged. Source parsing,
the frozen eight-core-module vendor-import boundary and `git diff --check` are
checked separately in the source checkpoint.

## Findings retained for the later regression/fault stage

The compatibility exploration is retained, not relabelled a passing full
regression. `compatibility-tests.json`/`.log` records 91 selected checks with
one failure: the inherited manual-evidence test re-runs an already-completed
workflow after its manual evidence changes; the existing
`recovery.execution_available` rejects completed workflow execution ownership.
This stage did not edit that recovery policy or weaken the inherited assertion.
The other 90 selected checks passed, including the existing interaction, neutral
E2E, v2 transport/contracts and registry suites.

An earlier mixed compatibility run exposed two additional existing full-suite
qualification issues: legacy fixtures change only CODEX_HOME while a parent
explicit ATO_HOME is shared (reused synthetic UUID endpoints collide across
fixtures), and the inherited budget-stop/controller case tries to send a
question follow-up after the workflow start-generation changed. The current
strict question-generation guard rejects it. These findings need qualification
in the already-planned regression stage, without changing protected tests,
weakening generation fences, resetting attempts or mutating the frozen plan.
They are not hidden by the new hub-suite success. The worker has not run or
claimed the complete frozen regression acceptance.

## Frozen host handoff

The implementation is ready for the scheduler-owned independent stage check:

```sh
python3.12 OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py hub
```

This external command was read but not executed by the worker because it writes
outside the worker-owned candidate. `ready_for_checks` asks the scheduler to run
that original frozen command; it does not claim the host result. This stage does
not request `needs_host`, actual-Pi/local-model smoke, activation or broader
authorization. Later fault/full-regression and final candidate packaging,
migration/rollback documentation and the origin-owned bounded actual-Pi smoke
remain part of the complete unchanged goal.
