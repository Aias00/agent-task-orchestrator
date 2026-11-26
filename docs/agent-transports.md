# Adapter and transport boundary

ATO owns workflow authority, attempts, STOP and independent acceptance. An
adapter owns the vendor-specific invocation or conversation protocol.
`agent_registry.py` declares code-owned capabilities; `ato_workflow/session_router.py`
resolves registered identities; `ato_workflow/message_bus.py` durably records bounded
messages and receipts. No title, directory name or model reply grants authority.

Legacy Codex identity requires exact native proof. Managed local CLI actors
have explicit project/contract/role scopes and private credentials. Pi managed
coordination requires its qualified profile and correlation receipts. A worker
adapter is not automatically a coordinator adapter. Unsupported capabilities
remain unsupported; ambiguous delivery is not retried as a new operation.

See [source map](source-map.md), [session capabilities](session-decoupling/capabilities.md)
and [neutral CLI usage](session-decoupling/neutral-cli.md). The local-only
scripted Pi verifier is an operator-invoked diagnostic, never part of the
default offline suite or an implicit real-agent admission.
