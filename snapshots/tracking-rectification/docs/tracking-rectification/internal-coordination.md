# Private material inbox and workflow responsibility

This stage adds inactive candidate source only. It neither activates this checkout
nor completes the five-fix total goal or the original efficiency qualification.
All original task files, STOP, attempts, grants, user decisions and old notice
history remain authoritative.

`internal_coordination` admits explicitly requested UTF-8 report artifacts into
the existing neutral `MessageBus`. The canonical report, bounded artifact snapshot
and queued envelope commit atomically in the same FULL/WAL store. A stable ID
covers exact workflow, contract, origin/hub, origin-record hash, execution
generation and caller request ID. Reusing that ID with different material,
summary or source fails. A queued/dispatching/unknown envelope is never replayed.
No adapter is registered to dispatch `ato_private_inbox` to a native chat queue.

The report is a private durable pending inbox. Automatic wakeup is explicitly
`unsupported`: there is no qualified private background consumer. A Codex chat
queue is user-visible and is not an invisible internal channel. Existing
controller diagnostic transports keep their established guard and correlation;
they do not become private transports or establish receipt from mere admission.
No polling, daemon, new supervisor, arbitrary prefix bypass or historical scan
is added. Earlier `recorded_only` materials are left unchanged.

The separate states are:

- `submission_state=queued`: one durable mailbox admission, no receiver read.
- `inbox_state=available`: the authorized maintainer can explicitly retrieve it.
- `inbox_state=read`: the exact recipient called the read operation, with a
  stable, sealed local receipt and actual bounded artifact retrieval. This is
  not a user presentation, model response, user read, approval or acceptance.
- `assignment_state=assigned`: the recipient explicitly accepted a bounded
  diagnostic action for at most 900 seconds. Exact duplicate calls retain the
  original expiry; expiry cannot be renewed under the same report ID.
- `handling_progress=unverified`: report read/assignment does not prove execution.
  Actual workflow handling and verified progress are queried through the
  responsibility ledger and its independent kernel/lease/disposition checks.

Inbox listing authenticates origin/hub role and exact task scope, returns at
most 256 newest records for that workflow, and creates no read or presentation
receipt. Submission requires the verified origin; read and self-assignment
require the verified recipient hub. V2 uses explicit scoped actor credentials,
never environment identity alone. Local CLI actor authentication can prove the
same immutable scope after STOP/completion for inspection. Mutating/routing
operations still enforce their existing open-task and STOP checks. No actor
identity, project, role, contract or generation proof is relaxed.

Only explicit bounded artifacts under the exact project are admitted. Parent
traversal, symlink ancestors, nonregular files, binary payloads and conventional
private configuration paths fail. Artifact reads use descriptor-relative
`O_NOFOLLOW` traversal. The text limit is 64 KiB; summaries are at most 4 KiB;
request ID and source are bounded. The private snapshot and envelope hashes are
checked before receipts. Original artifact hash drift blocks new read/assignment
receipts. Store contents are private ATO state and are not included in source
bundles. Credential challenges/tokens never enter report output.

## Neutral CLI

Use the candidate interpreter (Python 3.11+) and explicit scoped actor files:

```sh
python runner.py internal-report submit "$WORKFLOW" \
  --request-id requested-diagnosis-v1 --artifact "$PROJECT/report.txt" \
  --summary 'Requested diagnostic material' --source 'Exact instruction source' \
  --actor-file "$ORIGIN_ACTOR"
python runner.py internal-report inbox "$WORKFLOW" --actor-file "$HUB_ACTOR"
python runner.py internal-report read "$WORKFLOW" --id "$REPORT_ID" \
  --source 'Actual maintainer inbox retrieval' --actor-file "$HUB_ACTOR"
python runner.py internal-report assign "$WORKFLOW" --id "$REPORT_ID" \
  --next-action 'Inspect the exact failed check evidence' --seconds 60 \
  --source 'Actual bounded diagnostic assignment' --actor-file "$HUB_ACTOR"
```

A runnable, fully synthetic example provisions private temporary local CLI actors
and uses the actual public CLI, then removes its fixture:

```sh
python scripts/demo_internal_coordination.py
```

No workflow execution, model request or notification is sent by the example.
Necessary real user questions still use canonical `ask`, `read-question`,
`respond` and `accept-answer`, with their existing distinct actor requirements.

## Workflow-specific diagnostic responsibility

The read-only efficiency source's responsibility implementation and 43 directed
tests were ported, retaining the additional path-safety regression, as
`task_responsibility.py` and `tests/test_tracking_responsibility.py`. The separate
responsibility stage adds controller integration and negative tests; see
[its validation](responsibility-validation.md) for the current counts. Only the kernel identity and exact
parent/stage binding helpers were extracted into `responsibility_process.py`.
Its warm runtime, accounting, candidate manifest and qualification were not
imported or changed.

New contracts opt in explicitly with `"responsibility_policy":{"version":1}`.
Existing immutable contracts retain legacy semantics. The existing controller
creates obligations; no scheduler or independent observer is added. Session-wide
active/unknown never proves that an opted-in workflow is being handled. Exact
workflow/contract/generation execution leases, verified task processes, finite
workflow claims, canonical questions and bounded external waits are classified
separately. Unknown delivery remains a no-replay barrier. Reading internal
material or listing an inbox cannot create such a workflow claim.

```sh
python runner.py responsibility-status "$WORKFLOW"
python runner.py responsibility-claim "$WORKFLOW" --role origin \
  --next-action 'Inspect this workflow failure only' --evidence "$PROJECT/diagnosis.txt" \
  --seconds 60 --source 'Actual diagnostic claim' --actor-file "$ORIGIN_ACTOR"
python runner.py responsibility-disposition "$WORKFLOW" --role origin \
  --outcome external_wait --next-action 'Recheck the recorded external condition' \
  --condition 'Named external proof becomes available' --recheck-at "$FINITE_DEADLINE" \
  --evidence "$PROJECT/diagnosis.txt" --source 'Actual diagnosis' --actor-file "$ORIGIN_ACTOR"
```

A diagnostic must leave an explicit verified authorized action, bounded external
wait with trigger, exact canonical human question, or evidenced closure. A reply,
queue receipt or heartbeat is not recovery. Claims expire and cannot be renewed
for the same failure episode. Metadata accounting drift does not mint another
failure alert. A genuinely different failed attempt/check produces a new episode.
Notice suppression, delivery/presentation and actual resolution remain distinct.
An unverified live PID preserves the obligation and cleanup fence. Verified
processes can temporarily suppress a notice without resolving the obligation.
Subsequent progress compares bounded project evidence bytes and semantic results,
so timestamps, heartbeat, elapsed accounting or diagnostic summaries alone do
not resolve it. A bare completed label still requires independent acceptance.
Wait dispositions revalidate their actor and trigger, and an automatic
supervision alert cannot stand in for a necessary canonical human question.
User notices remain quiet for progress/internal reports/stages, failure-once and
completion-once retain their original guards, and Drover mute still takes priority.
Approved-delivery observation resolves only its exact diagnostic scope; old
residual criteria stay visible on both responsibility and operational records
and in the bounded origin diagnostic. No business retry, repair or attempt reset is
performed by any of these APIs.

Queries never initialize the responsibility database. `responsibility-status`
and `task-summary --format json` expose unresolved follow-ups even when the
ordinary user-question inbox is empty. This stage adds no activation or rollback
command: final candidate packaging and the origin-only real-record qualification
remain later total-task gates. Keep pending internal artifacts when switching
candidate readers; do not downgrade an upgraded mailbox schema or restore an old
snapshot over newer receipts.
