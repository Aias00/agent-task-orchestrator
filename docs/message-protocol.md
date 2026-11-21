# Independent mailbox and transport primitives

`message_bus.MessageBus(path)` is an independent local SQLite/WAL API. The caller
chooses its explicit private database path, registers exact endpoints and routes,
and manages authenticated endpoint credentials and exclusive ownership. Credentials
must not enter model inputs, task records or Git. Endpoint/source validation, task
and contract binding, idempotency keys, leases, cancellation and receiver receipts
are separate from the existing workflow JSON interaction ledger.

`transport.dispatch_one` claims FIFO work and commits a dispatch barrier before
invoking a trusted adapter. `failed` means no native effect is known to have
occurred; a timeout/crash/ambiguous write becomes `delivery_unknown`. Recovery does
not resend unknown work under a new ID. Native delivery is not model acceptance,
a completed workflow or a user decision. Receiver events require exact endpoint,
session, message identity and evidence source.

This API does not install managed Claude/Pi transports, expose mailbox-pump CLI
commands, inject arbitrary existing terminal sessions or migrate legacy queues.
The Agent registry/session router is a separate opt-in extension boundary; see
[adapter guide](agent-adapter-guide.md). Default Codex origins remain supported by
their exact legacy identity and queue paths. Original workflow authority, STOP,
independent host checks, quiet routing and actual user-answer requirements remain.

## Managed session v2 integration

The session decoupling candidate now uses this same mailbox for v2 canonical
questions, answers, recovery and controller follow-ups. Their older JSON/SQLite
outbox rows project `mailbox_id` and never independently send the same envelope.
`mailbox_state` gives the exact durable state; compatibility `state=queued` can
mean a known native submission. Exact execution/route/endpoint generations,
dispatch attempts, private endpoint credentials and native turn IDs fence every
v2 receiver event. Generation and owner loss preserve late evidence without
settling new work. `reconcile_correlated` is an explicit current-host native
lifecycle check, never a resend operation or model approval.

V1 submission-only references keep their public compatibility authority unless
they have verified native queue serialization. They are not silently promoted
to managed coordinators. Existing claimed/unknown outboxes are not replayed or
rebound during migration. Additive mailbox schema v2 preserves v1 IDs, bodies,
fingerprints and history, but an old schema reader must not be pointed at a new
store or a restored snapshot used to erase new writes. The detailed stage
contract, compatibility limitations and local evidence are recorded in
[the transport checkpoint](session-decoupling/transport-checkpoint.md).
