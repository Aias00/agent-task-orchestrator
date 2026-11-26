# Candidate migration and backward compatibility

This is a reviewable migration plan, not an executed migration. Authorization
covers isolated implementation, tests and candidate delivery only. The existing
runtime, launcher, queue guard, installed skill, global hub, real task directories,
policies and sessions remain in place. No task contract, frozen check, attempt,
supervision ceiling or historical directory is rewritten.

## State-by-state policy

| Existing state | Candidate handling and operational boundary |
| --- | --- |
| Frozen legacy task/origin UUID | Keep contract, origin UUID, native home and directory unchanged. Public compatibility wrappers route to the vendor adapter. Never infer the latest native session. |
| Existing raw UUID/index without project attestation | Keep v1 compatibility. Do not upgrade it to a v2 project identity unless exact header/cwd provenance is verified. |
| New neutral origin | Add an opaque v2 reference after exact registered-adapter verification. New reference is not a rewrite of a frozen old origin. |
| Unfinished legacy question | Keep original canonical question ID, target hub/origin and authority source. Do not rebind on a new default hub, seed a second mailbox, or manufacture a user response. |
| Legacy claimed outbox | Retain as already claimed. Read its diagnostic state; never import it as an unsent retry. |
| Canonical queued message | Dispatch only under current STOP/contract/endpoint/route/execution/owner checks and its existing message ID. |
| Dispatching or unknown delivery | Preserve uncertainty. Reconcile exact native receipts manually within authority; no blind replay, new message ID, adapter switch or fresh attempt to hide it. |
| Delivered/accepted/responded/end_turn | Preserve each distinct state. None authorizes workflow/manual completion. |
| Human answer | Keep sourced actual user input and the original route. Origin separately accepts. Old generation/contract or conflicting duplicate answer is rejected. |
| STOP or user closure | Preserve tombstone and history. Automatic recovery/reconstruction cannot clear it. Explicit existing resume/approved stop-adoption rules remain separate. |
| Completed workflow with altered evidence | Explicit foreground revalidation can invalidate evidence; recovery/session hosts cannot reopen terminal work. No counter reset. |
| Pi verified shutdown/reconstruction | Add a verified successor reference and full checkpoint/history; keep original origin reference and old native journal/questions. New native ID is explicit, native resume is unsupported. |
| User-accepted closure / successor lineage | Preserve closure, evidence and old raw status; use existing observational lineage. No unrelated “completed” task masks the original acceptance. |
| Ordinary updates / unhandled failure | Preserve quiet ordinary notification policy, no implied presentation, once-only failure notice and existing budgets. Stale question follow-ups are recorded without a new send. |

## Configuration selection

For the first compatibility rollout, leave ATO_HOME unset and keep the selected
CODEX_HOME exactly as before. That preserves the old `ctask-interaction` layout.
Native Codex files remain in CODEX_HOME and are never copied into the candidate.
A neutral-only configuration can instead use explicit ATO_HOME with no explicit
CODEX_HOME. Both variables are locators, not authorization.

When both are explicit, account context is
`ATO_HOME/ctask-interaction/contexts/SHA256(canonical CODEX_HOME)`. All participants
must agree on this pair. Changing the native account cannot pick up another
account's advisory policy, hub, endpoint tokens or synthetic reused UUID. An
operator who deliberately moves old ATO state must first quiesce its existing
owners, verify the source root and exact references, and reconcile pending or
unknown sends. This task does not move that state, discover identities from
history, or rebind an existing question. Empty state after changing locators is
not evidence that old state was migrated or completed.

## Schema/read boundary and rollback

MessageBus migrations are additive: generation/route/attempt/canonical identity,
execution ownership and receipt history extend existing ledgers. Original
legacy question/contract/native fields remain. Old binaries do not understand
new v2 authority and cannot safely service a new Pi/local endpoint or unknown
canonical message merely because SQLite opens. Restrict rollback to contexts
whose records the previous runtime understands, after all candidate owners are
quiescent and new v2 work is reconciled. Keep a new-only context parked for the
candidate reader if the old reader lacks those semantics.

Never restore an old state.sqlite or JSON snapshot over new writes. Backups are
for forensic recovery only; they are not a rollback operation. Candidate rollback
changes executable selection only and preserves current ledgers, questions,
receipts, STOP, attempts, accounting, lineage and user evidence. The exact planned
selection/rollback sequence and origin host qualification are in
[operator-handoff.md](operator-handoff.md).
