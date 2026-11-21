# Approved delivery observations (inactive candidate)

An accepted answer can authorize a **different contract**, such as a specific
production delivery, while the original workflow still has external source or
measurement gaps. `approved-delivery` records that the approved scope actually
executed under that independently completed contract. It does not complete the
original objective or make the two contracts a same-authority workflow lineage.
This is stage 2 of the five-fix maintenance candidate; no live activation,
business operation, original efficiency qualification, or overall maintenance
completion is claimed here.

## Public commands

Use the candidate's `runner.py` with the intended existing `ATO_HOME` and native
account context. Fixture commands must use private temporary homes and synthetic
identities. A legacy parent-workspace origin needs an explicit validated
workspace association for **each exact workflow** whose binding requires one.
There is no automatic association for every workflow in the repository.

```sh
python runner.py approved-delivery inspect /absolute/original-workflow \
  --question question-EXACT_ID --delivery /absolute/delivery-workflow
python runner.py approved-delivery register /absolute/original-workflow \
  --question question-EXACT_ID --delivery /absolute/delivery-workflow \
  --evidence /absolute/operator-proof.json --source 'Exact observation source'
python runner.py approved-delivery status /absolute/original-workflow
python runner.py status /absolute/original-workflow
python runner.py read-question question-EXACT_ID
python runner.py followups --all
```

Inspection and registration require the exact verified original origin or the
current configured approval hub. For a neutral session use `--actor-file` with
its existing workflow-scoped origin or hub credential. A user-role credential,
root provisioning credential, environment reference, unrelated workflow grant,
or actor from another origin is insufficient. The commands issue no credentials
or grants and dispatch no messages. Status is a read-only metadata query, like
existing workflow status. `read-question` retains its existing hub actor check.
The candidate does not relax native/v2 verification or the local adapter's
restrictions on authenticating actors for closed workflows.

Inspection returns `expected_evidence`, original criterion definitions and the
independent delivery's criterion definitions. Copy `expected_evidence` into an
operator-owned regular JSON file. Explicitly fill in only its two scope fields:

```json
{
  "approved_criteria": {"production": ["deploy", "public-checks"]},
  "deferred_criteria": ["external-source", "real-measurement"]
}
```

These are **fields in the full inspected proof**, not a complete proof file.
Every original criterion must appear exactly once in the approved keys or the
explicit deferred list. Every mapped value must name actual completed delivery
criteria. The operator must map only the actual accepted scope; mappings do not
assert that the original criterion passed. Definitions, raw results and all
original unresolved criteria remain visible, including the mapped criteria if
they still fail in the original workflow. Multiple observations must have
disjoint original scopes and distinct deliveries; their union determines the
remaining diagnostic scope. No text similarity or language parser decides what
the user authorized.

## Pinned proof and authority

The complete version-1 proof includes:

- The exact canonical question ID/file hash, actual answer, answer source and
  accepted timestamp. It requires the canonical question already be accepted by
  its origin with a recorded answer delivery. Queued, pending or answered alone
  cannot substitute for acceptance. Unknown transport is not a receipt; an
  independently recorded actual origin acceptance is still observable.
- The exact original/delivery paths, semantic and byte contract hashes, origin
  file hashes and execution generations. Both must have the same verified
  origin and actual canonical Git repository. The current hub configuration,
  native header/home/index identity (inside the trusted adapter) or opaque
  registered neutral identities, and Git/common-directory physical identity
  are pinned. New execution, changed native identity, hub or repository fails
  closed. Appending native conversation events does not change the header proof.
- A computed `authority_delta` listing exact before/after values and presence
  for every changed contract field except objective and acceptance. Scope, authorization, stage plans, limits, permissions, sandbox, agent and budget
  changes are included. `permission_expanded` conservatively means **any such
  authority-bearing contract difference**, not a natural-language determination
  of a grant. It must be true when the delta is nonempty. A supplied false
  no-expansion claim or omitted difference is rejected. This observation
  truthfully permits the record to show separately authorized production scope;
  it never grants permission to perform it.
- An independently completed delivery state hash and passed evidence for every
  delivery criterion. Every manual criterion needs its actual matching
  acknowledgement, contract, execution revision, note and underlying evidence
  checksum. All acknowledgement and underlying evidence bytes are pinned.
  Missing/failed evidence, active or unknown executor identity, a stopped
  delivery and unacknowledged manual results are refused.

The bounded, atomically saved ledger is
`ATO_HOME/ctask-interaction[/contexts/ACCOUNT_HASH]/approved-deliveries.json`.
The context suffix follows the existing account-isolation rule. It is ATO-owned
observation metadata. No workflow file, question, acceptance history, STOP,
attempt, retry ceiling or native metadata is written. Identical registration is
idempotent and retains the first source/actor/time. Conflicting assignments,
reused deliveries, overlapping scopes, cycles (including existing compatible
lineage edges), changed/missing evidence, symlinks and stale identity fail
closed. Records are never silently overwritten to fit new proof. Checksums are
integrity pins within the trusted local ATO state boundary, not signatures
that make an arbitrary writer trusted.

## Query and controller behavior

`status` adds `approved_delivery`; it leaves raw `status`, `remaining` and the
existing `delivery.effective_status` unchanged. `read-question` and inbox expose
`approved_delivery` alongside the unchanged accepted answer/history. A valid
observation allows a read-only hub inspection even when the original now has
STOP. That observation path refuses `--presented-source`; it cannot manufacture
presentation history or authorize a new answer/acceptance.

Each relation exposes `approved_scope_status=delivered`, the real delivery
workflow, completed evidence, actual authority delta, explicit deferred list,
current remaining deferred criteria and the original raw criterion results.
`relation_completes_original=false` and `execution_authority=false` are explicit.
`original_workflow_complete` reports only the independently observed original
raw completed status, never an inference from this relation.

The controller resolves only the exact accepted question's delivery obligation
when that action still tracks the original workflow. An explicitly linked
continuation continues to track the complete original goal and retains the
scoped delivery as evidence; it is not resolved by this observation.
It attaches the relation to original workflow/operational diagnostics and
restricts their diagnostic criteria to real remaining deferred work. If all
remaining scope has been explicitly observed, it stops creating that duplicate
operational obligation and retires its pending failure diagnostic, without
completing the original workflow. Existing terminal dispositions remain
historical. Other questions and residual failure/diagnostic obligations remain
visible. If pinned proof changes, automatic approved-delivery suppression is
withdrawn; the diagnostic obligation becomes visible again. Existing unknown
outbox claims are retained and never replayed, even during that reconciliation.

No completion notice is generated by this relation. Completion once, failure
once, quiet progress, project/Drover mute, no historical backfill, and unknown
outcome/no replay remain under their existing policies. A presentation is not
user read, and silence is not approval or acceptance.

## Validation and next stages

`tests/test_approved_delivery.py` exercises actual candidate CLI/API call paths
with private ATO/native homes, synthetic actors, local temporary repositories,
manual acknowledgement and fault fixtures. No real question/hub/native history,
model, queue or business call is used. Native answer transport is explicitly
fault-injected in the fixture; it records uncertainty without fabricating a
receipt. A separate neutral CLI fixture uses private passive endpoints and
existing scoped credentials.

The fixed host gate is:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python \
  OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/acceptance.py approved_delivery
```

The scheduler runs that gate outside the worker sandbox. Internal material
receipts, responsibility, physical-delivery completion deduplication, the final
portable candidate and bounded origin-only actual-record proof remain separate
stages. This stage neither edits the old efficiency manifest nor qualifies that
workflow. No real-record probe or activation is performed here.
