# Source compatibility and migration boundary

Implementation SHA-256: `0f2caac929606b375922ee9cd796ce335eb1032d1a44853674620d4da607a8e4`

Exact manifest/archive hashes are in the detached, checked
`build/tracking-rectification/hashes.json`. This source candidate is inactive;
preparing, extracting, checking or qualifying it never selects a live runtime.

## Actual changes and interfaces

| Area | Candidate behavior | Authority boundary |
| --- | --- | --- |
| Workspace association | `workspace-association inspect/register/revoke` pins exact workflow, native home/ID/header, canonical direct-child Git root, origin, hub, contract and generation. Binding/read-question/controller use it at the original mismatch. | Trusted adapter owns native formats. Strict default, v2 proof and transport authorization stay intact. No ancestor exception or header edit. |
| Approved scope | `approved-delivery inspect/register/status` joins the actual accepted answer, independent delivery authority/contract and complete command/manual evidence. Controller retires only that delivered scope. | Observation-only; original blocked status, criteria, STOP, attempts and full residual objective survive. Actual authority deltas are recorded. |
| Internal material | `internal-report` CLI and `internal_coordination` route bounded artifacts through durable canonical IDs/hashes and existing neutral actor/message boundaries. | Available/queued, actual read, assignment and verified handling remain distinct. Unsupported wakeup stays unsupported; user-visible queue is never an invisible consumer. |
| Responsibility | `task_responsibility` and `responsibility_process` bind finite claims/dispositions to exact workflow, contract and generation; stable failure identity is separate from suppression and notices. | General session activity is advisory. A reply/claim does not authorize execution or establish resolution. Original repair/STOP/unknown-delivery barriers survive. |
| Completion | `completion-delivery inspect/register/status` or existing validated full compatible lineage identifies the physical operation before controller claim/dedup. | Equal titles, commands or artifact hashes alone are insufficient. Unknown old dispatch blocks successor replay; duplicate projection creates no presentation. |

Detailed API/CLI examples and negative cases are in `workspace-binding.md`,
`approved-delivery.md`, `internal-coordination.md` and `completion-lineage.md`.
The unchanged source interfaces continue to support legacy callers. New
observation arguments are optional, old ledger/action/outbox histories remain
readable, and existing completion/trusted dispatch validation still applies.
Agent-neutral routing/actor APIs retain their shapes. Codex-specific native
index/header proof stays in `codex_session_adapter.py`; the packager/probe does
not add another actor-verification implementation or credential format.

Responsibility enforcement remains opt-in through an existing new-contract
`responsibility_policy: {"version": 1}`. Existing immutable contracts are not
rewritten. The reused efficiency work covers responsibility/disposition and
stable failure handling only; unrelated efficiency/warm-runtime changes were
not imported. Stage provenance records the read-only reuse source and hashes.

## Observation state

New records live under the existing account-scoped ATO interaction directory,
including `workspace-associations/`, `approved-deliveries.json`,
`completion-deliveries.json`, and internal/responsibility ledgers. Existing
native home, question history, workflow contracts/state/checks/attempts and old
outbox records are not migrated or rewritten. Every use revalidates pinned
proof. Revocation preserves association history; conflicts and drift fail
closed instead of replacing historical evidence.

Setting an explicit `ATO_HOME` chooses another ledger root; it does not copy a
hub, move history, change native home or issue a credential. Fixtures and the
final probe always use a fresh isolated ATO root. No backfill is run. Historical
`recorded_only` remains recorded-only; old claims and uncertain deliveries
remain replay barriers. Completion-once, unhandled-failure-once, quiet routine
progress/stages/handoffs, zero reminders, Drover mute and the original completion
cutoff are retained.

## Upgrade and rollback

No installation/launcher change, database migration or live activation is part
of this task. If live selection is separately authorized later, first verify
the exact manifest/archive hash and all frozen qualification including the
origin proof. Inventory and preserve active workflows, their pinned source and
process owners, account-specific policy, native metadata and complete existing
ledgers. Do not point an active old workflow at a new candidate merely by
replacing a global launcher. Select the reviewed immutable candidate only
through the already qualified deployment mechanism and its explicit scope.

For the present task rollback means leave this candidate unselected and retain
its review artifacts. A later authorized source rollback must preserve all
new and old observation/outbox/notice history and task-pinned runtimes. Never
restore an old database snapshot, delete uncertain dispatch claims, erase STOP,
reset attempts or turn an accepted-scope projection into business replay.
Old code lacks the new cross-successor dedup protection; keep new completion
dispatch disabled until that compatibility risk is resolved under the actual
notification preference. Rollback itself never grants resend authority.

The independent efficiency manifest remains
`4ff7510adf12c684a920717c470304c68f235847340344e6fb6538adf1a1414a`.
Its complete objective and qualification are not discharged by any tracking
stage or probe. The recorded foreground sample failed after 26.64 seconds with
`native profile changed during turn`; original claim/journal remain intact.
No automatic replay or root-cause claim is made here.
