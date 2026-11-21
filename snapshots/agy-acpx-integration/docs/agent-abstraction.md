# Local agent abstraction candidate

This is an isolated candidate, not an installed runtime. `agent_registry.py` is a trusted in-process registry: plans choose a registered kind, never an import path, executable, or plugin. Version-1 execution/session contracts remain compatible; additive version-2 identity, capability, activity, event, and actor contracts form the neutral session boundary. `agents.py` supplies the built-in execution adapters. `runner.py` and `workflow.py` negotiate worker and planner capabilities before creating their subprocess commands; independent acceptance checks, attempts, cancellation and frozen contract hashes remain scheduler responsibilities.

## Coupling audit and current boundaries

| Area | Implementation and remaining boundary |
| --- | --- |
| Worker / planner | `agents.command` routes to a registered adapter. Codex retains its native execution flags; Claude and Pi use `agents.py`'s macOS sandbox bridge and the existing file-tool allowlists. A read-only planner receives no worker grants. |
| Progress | New process records carry adapter kind and checkpoint label. `control.py` uses registered progress-path and decoder methods; `agent_native_events.py` owns native parsing. The combined decoder in `progress.py` is only a compatibility path for old process records without kind metadata. |
| Origin / hub | `session_router.py` persists explicitly registered references after adapter verification of exact identity and project. `workflow.py` stores `origin_session_ref` separately from old `origin_thread_id`; `interaction.py` preserves the existing question ledger, notification policy and actual user-answer requirements. |
| Transport / correlation | A registered session transport is invoked after the existing durable `dispatching` record. Errors after that barrier are unknown and are not retried. Optional versioned events require exact identity, project, correlation and turn, and controller reply observation starts at the pre-send cursor. `message_bus.py` and `transport.py` additionally provide a durable managed mailbox API with explicit credentials, claims and a dispatch barrier; this independent API does not automatically consume or migrate the legacy interaction ledger. |
| Recovery | `recovery.py` checks origin and contract, requires explicit authorization and adapter observation/transport, and observes correlated events before accepting progress. A queue receipt is not a resumed process. Limits, evidence, STOP and attempt history remain intact. |
| Delegation | `delegation.py` asks the selected trusted adapter to create and prove a session with the requested project, sandbox and approval policy. It persists the creation barrier first and requires explicit acceptance before the existing dispatcher starts the exact workflow. `codex_legacy.py` contains the legacy app-server protocol, UUID conversion, exact index/rollout lookup, queue parsing and rollout decoding. The legacy fields and paths remain readable. |

`interaction.py`, `control.py`, and `recovery.py` still retain narrow compatibility branches for old Codex state and its rollout offsets. ATO-owned ledgers use `ATO_HOME` when set; without it, they keep the historical `CODEX_HOME` location. `CODEX_HOME` continues to locate Codex-native indexes and rollouts. Both are path locators, not actor proof or session authorization. `appserver.py` remains the Codex-specific protocol implementation. No arbitrary native session ID is converted to a Codex UUID.

## Capability matrix

| Adapter | Worker | Planner | Native coordinator / hub | Delegation / recovery | Progress |
| --- | --- | --- | --- | --- | --- |
| Codex | Native CLI | Native read-only CLI | Existing exact UUID/index and native queue compatibility | Existing app-server creation and rollout-correlated recovery | Codex JSON stream |
| Claude | Restricted file tools, macOS sandbox required | Restricted read-only | Not implemented | Not implemented; explicit request fails | Claude stream JSON |
| Pi | Restricted file tools, macOS sandbox required | Restricted read-only | Not implemented | Not implemented; explicit request fails | Pi stream JSON |
| Fourth test fixture | Offline fake subprocess | Offline fake subprocess | Explicit fixture identity, actor and transport | Offline fixture creation, acceptance and correlated recovery; no real model | Fixture events |

All capabilities describe implementations, not guarantees about installed CLI versions, authentication or cloud providers. External workers do not inherit additional writable roots, arbitrary shell/MCP tools or full access. Explicit unsupported capabilities fail before dispatch. A transport result of `delivered` means a transport receipt, not model acceptance or user approval. No untrusted plan can register an adapter; no arbitrary TTY injection or native session discovery is implemented.

The v2 registry reports Codex session creation as supported, its current load/resume API as unsupported, and actor authentication as unknown. Claude and Pi are explicitly unsupported for session creation, transport, observation, activity, cancellation, load/resume, and actor authentication; their worker/planner registrations do not imply coordinator support. These code declarations remain separate from CLI installation and provider readiness.

The candidate does not start a new polling service or migrate active state. The old notification mute/once behavior, advisory timing and host-recheck paths continue to apply. The fixture tests exercise isolated data and do not prove native provider/session compatibility; see `build/agent-abstraction/validation-report.md` for actual checks and the host limitations.
