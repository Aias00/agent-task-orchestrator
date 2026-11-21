# Completion identity and successor deduplication

This inactive candidate extends the existing completion-once controller. It
does not change the policy, completion cutoff, native queue gate, business
execution or any existing action/outbox history. This is the dedup stage of
the five-fix task, not completion or activation of the entire candidate.

`completion_delivery.identity(workflow)` resolves a bounded, revalidated
component through either of these explicit sources:

- Existing compatible `workflow-link` records. The original authority and
  mapping validator runs again, pinned successor state and evidence must still
  match, and the mapping must cover both entire acceptance sets. A successor
  with additional criteria remains a separate completion for notification
  purposes. No link is inferred from objective, path, check command or output.
- A separately registered shared physical-operation receipt. Both results
  must already be independently completed, idle, without STOP, with their
  complete command/manual evidence and actual acknowledgements. The same
  canonical receipt must occur in both results' evidence. Its exact operation
  ID, workflow/contract/origin/generation bindings, repository identity,
  authority digest and full verification payload are checked. Uniquely mapped
  full acceptance and every non-objective/non-acceptance contract field must
  agree. A common artifact hash on its own never creates an association.

The explicit receipt route uses `completion-delivery inspect/register/status`
and an append-only bounded `completion-deliveries.json` in the ATO interaction
context. Registration requires the verified bound origin or hub. Neutral
clients supply their existing workflow-scoped `--actor-file`; a role or bare
session identity is insufficient. Vendor identity/header parsing remains in
the trusted session adapter. No credential is saved in the observation.

For two exact absolute workflow paths and an **existing** qualified shared
operation receipt:

```text
python runner.py completion-delivery inspect /exact/first --successor /exact/final --receipt /exact/operation.json
python runner.py completion-delivery register /exact/first --successor /exact/final --receipt /exact/operation.json --evidence /exact/reviewed-proof.json --source 'Exact sourced same-physical-operation verification'
python runner.py completion-delivery status /exact/final
```

The reviewed proof must equal `inspect.expected_evidence`; inspect only returns
it and never writes workflow files. `operation-template --operation-id ...`
returns the strict shared-receipt schema for prospective fixture or qualified
operation producers. A template is not a record of production execution.
Never manufacture a retrospective operation receipt or append it to completed
historical workflow state to make registration pass. Existing legacy results
must use their original immutable proof or already verified compatible lineage.

The receipt route conservatively refuses overlapping/reassigned pairs,
ambiguous criteria, changed proof, wrong home/actor/repository, stale generation,
symlink paths, missing acknowledgements, active processes or incomplete work.
Every projection revalidates original evidence. Invalid connected evidence
blocks new completion sends pending reconciliation; it never permits a fresh
successor replay. Registration grants no retry, deployment, publication,
execution or user-acceptance authority. Approved-delivery observations remain
separate: independently approved scope being delivered does not alias its
incomplete predecessor's entire objective.

Before dispatch, the controller checks existing canonical result actions and
all durable outbox claims in this component. Existing notice/presentation or
queued/dispatching/unknown claims remain barriers under successor IDs, including
pre-upgrade outbox-only claims. The check repeats inside the existing SQLite
`begin immediate` outbox transaction. It creates no second transport or daemon.
The trusted adapter still validates the exact action, body, destination,
generation, policy and dispatch claim before any native submission.

`completion_delivery.duplicate_of` is observation metadata. Ordinary duplicate
projection adds no notice, presented/read timestamp or user acknowledgement.
If two sends were already projected before the first claim, the transaction
records the second as `recorded_only`, `authority=none`, with the original
claim reference and no transport attempt. It does not replace the old claim.
Historical physical completions remain behind the original policy cutoff even
when their successor is newly watched. Drover mute and zero reminders retain
priority, and distinct physical operations retain independent once-only sends.

No migration/backfill is required: old action and outbox schemas are read
directly. Do not clear claims or restore an old database snapshot on rollback.
Rolling source back loses this dedup protection for new successor IDs; it does
not revoke old notices. Candidate packaging, frozen full regression and the
bounded actual-record origin qualification remain later task stages. The old
efficiency workflow and its pending qualification remain independent.
