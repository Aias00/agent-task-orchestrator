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
