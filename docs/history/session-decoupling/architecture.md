# Agent-neutral session coordination candidate

The full goal and frozen acceptance remain in [task.md](task.md). This checkout
is an inert candidate seeded from the actual live implementation. It does not
replace the global runtime, controller, hub, policies, installed skill or any
real origin. The stage checkpoints describe historical stage results; current
qualification and remaining host work are in [validation.md](validation.md).

## Dependency and authority boundaries

`agent_registry.py` defines versioned identities, capability states, actors and
serializable observations. `agents.py` performs trusted built-in registration.
`session_router.py` verifies opaque references and dispatches through those
registered adapters. Task plans and environment variables cannot import an
adapter, choose a coordinator command or authenticate a v2 actor.

The workflow, controller, recovery, interaction, delegation and transport use
that router. The eight frozen core modules do not import Codex native code.
`codex_session_adapter.py` owns exact UUID/index lookup, read-only native SQLite,
bounded rollout decoding, queue receipts, app-server create/readback and native
queue serialization. `codex_legacy.py` forwards the original public API; the
compatibility shim preserves old fixtures and persisted UUID contracts. See the
concrete [coupling audit](coupling-audit.md).

`ato_config.py` centralizes state locators. With no ATO_HOME the exact historical
CODEX_HOME layout remains. A neutral-only client with ATO_HOME and no explicit
CODEX_HOME uses `ATO_HOME/ctask-interaction`. If both are explicit, ATO-owned
state is scoped to `ATO_HOME/ctask-interaction/contexts/SHA256(canonical CODEX_HOME)`.
This separates account-specific hub bindings, policies, UUID endpoints and
credentials under one ATO root, without reading native state. Different native
account locators never share those authorities. All cooperating processes must
use the same locators. Changing them does not move data or grant permissions.

## Canonical durable delivery

The existing SQLite MessageBus is the sole new-message authority. Question JSON
and controller outboxes hold pointers to its immutable canonical message IDs;
they are projections, never independently pumpable outboxes. Existing legacy
outboxes remain claimed historical records rather than being re-enqueued.

Messages persist queued/dispatching/delivered or delivery_unknown, then independent
accepted/responded/turn_completed receipts. Endpoint and route generations,
dispatch attempts, exact project/native identity, contract, workflow execution
generation, message, correlation and turn must match. A crash before possible
effect can recover queued work; a crash after the sending barrier quarantines
unknown delivery. No automatic replay, fresh ID retry or counter reset resolves
that uncertainty. Duplicate receipts are idempotent; stale receipts remain
history and cannot settle a later generation.

Repository flock plus a durable execution lease coordinates worker, origin host
work and session host. Lease expiry fences a token but cannot prove native
process termination. STOP, user closure, unknown native activity, stale authority
or a live competing owner refuse execution. Explicit foreground run/resume can
revalidate a terminal result only after its normal lock and STOP checks; recovery
and session hosts retain terminal refusal. Revalidation preserves attempts,
accounting and manual evidence and can invalidate a changed manual acceptance.
The CLI resume remains the explicit path that adopts an authorized old stop.

## Pi lifecycle and human interaction

`pi_acp` is a session adapter; `acp_pi` and `pi` remain distinct worker routes.
`PiCoordinator` owns one foreground ACP child process group and a private bounded
identity/event journal. `LocalPiProfile` requires installed Pi 1.5.2 and pinned
pi-acp 0.0.34, empty native configuration and a scripted loopback model with a
dummy key. The derived adapter attests native identity and attaches a host turn
nonce to events. Native error/EOF cannot be converted into successful work.

The coordinator exposes bounded file tools, with a trusted extension checking
workspace paths, metadata exclusions, current turn and exact workflow authority
immediately before tools. No model shell, arbitrary ACP permission callbacks,
ctask administration, credential access or arbitrary terminal injection is
provided. Cancellation stops only the native process group it owns. The private
host is a transport lifecycle, not a new repair supervisor or observer service.

Native load is unsupported until separately qualified. Explicit reconstruction
requires verified shutdown and no uncertain turns, exact actor/workflow binding,
the full objective, current file hashes, previous results and cumulative history.
It creates a new native identity and an additive successor link; original
references and previously sent questions stay immutable. It never replays work.

`local_cli` supplies a passive non-Codex human hub. Distinct root hub, user and
origin credentials produce private workflow-scoped proofs. The actual CLI
inbox/read/respond/accept-answer/continue-task path records actual user text and
source, then separate origin receipt and independent workflow checks. A model
reply has no user credential. Silence and listing do not imply presentation;
end_turn and acceptance of an answer do not satisfy workflow criteria. Ordinary
notifications stay quiet; an unhandled failure has at most its existing once-only
notice. See [neutral-cli.md](../../session-decoupling/neutral-cli.md) for runnable isolated examples.
