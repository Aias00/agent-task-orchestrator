# Workflow responsibility stage

This stage qualifies workflow-specific responsibility in the inactive tracking
candidate. It preserves the complete five-fix objective. The frozen host
`responsibility` command remains scheduler-owned; worker fixture results do not
claim that command, total-task completion, actual-record proof or activation.

The internal stage already ported the read-only efficiency candidate's
`task_responsibility.py` and 43 regression analogues, plus one project/path-safety
case. This stage preserves those cases and adds 12 integration/negative tests.
Only responsibility, disposition and stable failure handling are reused. The
old efficiency runtime, manifest, qualification and full objective remain
independently pending and unchanged.

## Behavior verified

- Claims bind exact workflow, project, contract, generation, authenticated
  origin/hub actor, evidence and finite expiry. Repeating a claim preserves its
  original deadline. Unrelated or malformed claims and changed evidence fail
  closed. Session active/unknown supplies no handling proof.
- A failed workflow retains a responsibility record even with an alive PID.
  A verified task handler suppresses notices temporarily; an unverified PID
  remains a cleanup fence and grants no diagnostic execution or retry.
- A reply or queued/unknown diagnostic delivery leaves disposition pending.
  Outcomes require actual verified subsequent progress, a bounded external wait
  with a trigger, an exact necessary canonical question, or evidenced closure.
  Wait actor/trigger drift and automatic supervision alerts cannot hide failure.
- Two verified live observations compare bounded regular project evidence bytes
  and semantic task/criterion results. Metadata and diagnostic summaries alone
  cannot resolve a failure. Changed output with the same byte length can prove
  progress. Unsafe, missing or oversized output does not prove progress.
- A `completed` label with failed or unacknowledged acceptance cannot complete
  diagnostic/failure records. Existing completion and manual-acceptance gates
  still decide actual delivery.
- Stable failure episodes survive metadata drift, including temporary claim
  suppression and expiry. Once-only notice history, uncertain delivery barriers,
  project mute and the no-historical-backfill cutoff remain independent of
  responsibility. New substantive failed attempts create new obligations.
- Valid approved delivery projects only residual diagnostic criteria on both
  responsibility and operational records. When no residual remains, the exact
  diagnostic resolves without rewriting the old blocked workflow or creating a
  presentation, approval, attempt, repair or execution grant.

The implementation remains opt-in for new contracts through
`"responsibility_policy":{"version":1}`. It does not rewrite old immutable
contracts or reinterpret historical notice IDs. Public CLI usage is in
[the coordination guide](internal-coordination.md#workflow-specific-diagnostic-responsibility).
Queries read the existing responsibility ledger without creating a schema or
treating an inbox read as task ownership.

## Reproduction and evidence

Use the prescribed Python interpreter and private temporary `ATO_HOME` and
`CODEX_HOME`. All tests create synthetic identities and fixture state. Transport
is mocked or denied; no real model, native queue, provider or business call is
required. Positive kernel-identity fixtures supply a synthetic start identity;
they are not native execution qualification.

```sh
PYTHONPATH=.:tests python -m unittest test_tracking_responsibility
```

The final fixture results and exact implementation hashes are recorded in
`responsibility-stage-receipt.json`. Detailed logs and machine-readable results
are in `evidence/responsibility-*.{log,json}`. Compatibility batches cover the
internal inbox, explicit workspace binding, approved delivery, current
controller, failure handling, interaction, actor roles, notification preference,
completion-once and existing lineage/closure behavior.

All 49 protected inherited test/fixture files and all five external pins are
checked against the frozen baseline. The external baseline and acceptance
script are only read. Original host socket/native-sandbox assertions stay
unchanged; known worker environment failures are not rerun or weakened.

The scheduler must run the exact stage gate outside the worker sandbox:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python \
  OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/acceptance.py responsibility
```

Remaining total-task gates are physical-delivery completion deduplication,
full frozen regression, reproducible portable inactive packaging, migration and
rollback instructions, and the final origin-only bounded read-only actual-record
proof. This stage performs none of the origin-only actions or live activation.
