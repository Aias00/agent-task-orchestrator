# Presentation stage checkpoint — inactive local candidate

This implements only stage 2 of [the complete task](task.md). The previous
attempt left implementation and passing local evidence but timed out before
returning a structured outcome. This retry reviewed those files, corrected the
Workflow wait projection and reran the relevant qualification. Nothing was
installed, activated, published or migrated into live task state.

## Query interface and interpretation

```sh
python runner.py status /path/to/task
python runner.py task-summary /path/to/task --format json
python runner.py task-summary /path/to/task --format text
```

`status` retains its existing fields and adds `task_view`. `task-summary` prints
that same projection (text by default). Queries read records and inspect kernel
process liveness/start identity; they never run checks or models, resume work,
acquire execution ownership, initialize a mailbox, send a question/notification,
mark anything presented/read, or change approval, STOP, recovery or completion.
WAL telemetry is read from a bounded private temporary copy so SQLite cannot
create or change sidecars in the original task directory. Temporary telemetry
copies are removed after the query. Unstable telemetry is unavailable, not proof
of acceptance.

The JSON projection includes:

- `objective`, `completion_scope`, `current_stage`, `stages` and `remaining`.
  A bound stage retains `stage_objective` and joins the complete parent objective
  under `overall` only after checking its parent/stage/repository/plan binding.
  Stage success and native turn success cannot accept total criteria.
- `raw_status`, `effective_status`, `process_status` and separate owner/worker
  observations. A live PID needs a matching recorded start identity before it
  counts as verified. Unavailable identity or a surviving unverified process
  group is unknown. No native activity or resumability capability is invented.
- `execution_accounting` separates invocations, failures, continuations, waits,
  yields, settled active/wait/yield seconds, and unsettled wait/yield projections.
  Workflow totals join every verified stage ledger plus planning/host ledgers
  once. Missing/invalid ledgers make totals unknown rather than fabricate zeros.
  Legacy `attempts` remain visible as history, never as failure counts; legacy
  failure/wait/active breakdowns are null. Invocation estimates state their basis
  and do not claim to recover historical counter resets.
- `blocker`, `next_step`, `needs_user`, `stop_requested`, `recovery`, `interaction`
  and `delivery`. Recovery request delivery is distinct from a verified handler.
  Query does not inspect or create a managed execution-lease database to infer
  native activity. Matching canonical question metadata is visible without
  accepting answers or recording presentation. It remains subject to existing
  actor/contract/session/generation validation during actual acceptance.

Example interpretations (exact IDs/times/evidence depend on the queried task):

| Recorded situation | Truthful projection |
| --- | --- |
| New legacy stage | `raw_status=created`, `completion_scope=stage_only`, failures and active/wait breakdown unknown |
| Running record, dead owner and child | raw running retained; effective interrupted; no verified executor; no wake or resume |
| Live PID with unavailable/different start identity | effective process_unverified; native activity and resumable capability unknown |
| Finite external wait with reason and future deadline | effective waiting; deadline/reason visible; settled active time unchanged; independent running branches remain visible |
| Workflow planner waiting | Uses the actual Workflow-level deadline and reason for both legacy and v2; expired/missing deadlines become wait_unverified |
| Stage passed, manual total criterion remains | completed stage retained; overall needs_input and remaining manual criterion visible; needs_user true |
| Frozen host handoff | needs_host with original checks next step; needs_user false unless a separate actual human question remains |
| Blocked workflow without verified execution | failed_no_verified_handler (verification absence, not a claim about native idleness); blocker and recovery history retained |
| Original blocked task with evidence-pinned completed successor | raw blocked and original remaining criteria/STOP retained; effective completed only while existing strict lineage validation still succeeds |
| Unknown delivery or STOP | Query retains the no-replay/STOP barrier and never grants retry/acceptance authority |

Completed successor delivery is an observation of independently validated
acceptance, not a rewrite of the original task. A changed successor state,
contract or pinned evidence invalidates that completion projection. Existing
manual acceptance and user closure paths retain their own authority checks.

## Quiet policy and compatibility

No notification policy, canonical ask implementation or failure attention dedup
was changed. Ordinary progress/completion, host handoff and operational updates
remain silent. Actual user decisions/permission/information/manual acceptance
use the existing canonical ask. Existing once-only unhandled-failure attention
retains its presented state across queries; querying does not rearm it.

`task_presentation.py` holds the projection; Runner's status/CLI call it.
Workflow summary retains legacy stage_status while handling an unavailable
active stage truthfully. Existing lifecycle accounting/execution semantics are
unchanged by this stage. No plan/state migration or new policy opt-in is needed
to read old tasks. Rolling back this presentation change requires no task data
rewrite; preserve all newer v2 data under the accounting rollback boundary.

## Verification and original host handoff

Worker evidence uses the existing frozen Python, temporary repositories, private
empty ATO_HOME/CODEX_HOME roots, and synthetic identities/local fixture executables
only. The local combined suite includes all tests in `tests/test_task_summary.py`
and the unchanged external `host_behaviors.Presentation` test. It covers actual
JSON/text CLI snapshots, full filesystem side-effect guards, real local Runner
continuations and Workflow/manual acceptance, stale/reused PIDs, live WAL reads,
finite/expired waits, distinct counters/legacy unknowns, exact stage linkage,
verified successor history, canonical questions and quiet failure dedup.

`presentation-validation.json` records final counts, source identities and
evidence paths. Direct frozen `preserved()` checks verify all 48 inherited
tests/fixtures, installed launcher/runtime and linked dependency remain intact.
Known socket/nested sandbox restrictions are not retried as full regression in
this worker. Direct local external-test success is not the original host gate.
The scheduler/original host must run exactly:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-linger-continuation-20261119/acceptance.py presentation
```

The frozen command writes its own receipts outside this worker's writable scope.
This stage returns ready_for_checks and does not claim that command passed.
The host contract authorizes only the original isolated frozen gates, never
activation, installation, publication, real model/provider/Pi calls, or live
task/hub changes. Scheduler-owned `.codex/tasks`, frozen task/check definitions,
external verifier/baseline files and protected tests are not edited.

## Complete remaining objective

Stage 1 already has an external accounting receipt; it remains part of final
integrated qualification. Passing presentation is still stage-only evidence.
Remaining work under [task.md](task.md), with its full acceptance retained:

1. Original frozen presentation gate for this exact candidate.
2. Stage 3: trusted versioned static endpoint limits and interactive reservations;
   explicit opt-in/Workflow inheritance; durable deduplicated queue, atomic claim,
   exact token/owner/generation fencing; interactive priority/background FIFO and
   starvation visibility; STOP cancellation and cleanup-confirmed release;
   unknown cleanup/delivery retains its slot. No TTL reclamation, preemption,
   arbitrary resume, permission/ownership expansion, adaptive concurrency or new
   supervisor. Integrate actual Runner/Workflow/CLI and at least 12 behavioral
   capacity tests; run the original frozen capacity gate.
3. Original integrated accounting/presentation/capacity/regression gates: at least
   all inherited 563 tests plus new suites, zero failures/errors/skips, preserved
   hashes, APIs/schema/count/history, STOP/no-replay/manual/quiet-policy guards,
   qualified session decoupling and untouched live runtime/dependencies.
4. Reproducible inactive local delivery: prepare_linger_candidate.py and --check;
   complete portable source/test/docs hashes and modes, baseline and predecessor
   manifest identities; reproducible archive; architecture, migration, validation,
   operator-handoff, README and repository-skill updates; original delivery gate.
   Document old task compatibility and rollback without overwriting v2 data.

No deployment, Git commit/push, runtime activation, live migration or real model
qualification belongs to this authorized candidate task. No overall completion
is asserted by this checkpoint or independent branch success.
