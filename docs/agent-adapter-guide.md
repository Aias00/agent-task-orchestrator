# Adding a trusted adapter

Registration is a code-owned bootstrap operation, not a task-plan feature. The complete runnable example is `tests/fixtures/fixture_adapter.py`; `tests/fixtures/fixture_ctask.py` explicitly registers it before entering the ordinary CLI and calls `set_runtime_entrypoint` so child runtime processes load the same trusted registrations. This bootstrap is fixed by trusted code, never taken from a plan or environment variable. Run the offline exercise with `python3.12 -m unittest discover -s tests -p test_agent_neutral_e2e.py -v` from the candidate checkout. The fixture creates temporary repositories, never calls a provider, and launches its planner and worker as actual child processes.

An adapter declares an immutable `frozenset` of capabilities and `kind`, then implements only those capabilities. For example, a worker and planner implement `command(request)` and return argv for their trusted executable. The request identifies the output schema, destination, sandbox and planner role. A planner must preserve read-only restrictions. A worker must enforce at least the existing restrictions; merely declaring a capability does not provide a sandbox. Plans supply only a registered `agent.kind` and optional model, not commands or import paths.

For explicitly supported sessions, `verify_session(identity, project)` verifies the exact native identity and project from trustworthy evidence. `is_actor` must authenticate the current session; a bare environment value is not a security proof. `send(endpoint, message)` returns a `transport.DeliveryResult`: `failed` is permitted only when the adapter knows no effect occurred; ambiguous outcomes are `delivery_unknown`. The same send interface serves the session ledger and managed mailbox. The host persists its dispatch barrier before calling it. If supporting observation, `session_cursor` captures a bounded serializable cursor before sending and `observe_session` returns `(events, next_cursor)` for the exact correlation. Events are `agent_registry.AgentEvent` objects with version-1 `SessionRef`, source, turn ID and correlation ID. The core validates the session and correlation again, and uses a matched received/completed turn before relaying text. Text is task material, never a user decision.

For explicit delegation, `create_session` must establish and independently read back the exact identity, project, sandbox and `approval='never'`, then return `CreatedSession` with evidence source. Delegation also requires session verification, actor, cursor, observation and transport capabilities. Creation can have an ambiguous outcome; do not retry it blindly. A real implementation must enforce and attest its native policy, avoid arbitrary TTY injection, and never silently replace missing restrictions. The test adapter simulates these properties only inside its temporary fixture and is not a production adapter.

An opt-in host can call `session_router.register(kind, identity, project, source)` or `ctask session-register --kind KIND --identity EXACT_ID --project PROJECT --source EVIDENCE` after loading trusted code. It can bind that returned opaque reference with `hub --session-ref REF` or `workflow-create --origin-ref REF`. The CLI alone cannot load external code or magically enable Claude/Pi lifecycle capabilities. Session references are local to their configured ledger and should not be copied between machines. For a new coordinator, `delegate --session-kind KIND` requires the full creation capability set and the original explicit delegation authorization; the existing default is the Codex legacy adapter.

Use `progress_path` and `normalize_progress` only for native events whose semantics are known. Arguments are hashed before persistent watchdog state. Missing capabilities must raise before effects, and credentials must never enter messages, plans or candidate artifacts. Add integration tests covering actual subprocesses, identity mismatch, policy refusal, unknown delivery and STOP. Do not ship the fixture bootstrap as a production plugin mechanism.

## Version-2 session adapters in this candidate

The version-1 interface above remains a compatibility API. New adapters declare
`session_reference_version=2`, use `SessionIdentity` / `SessionReference`, and
report each capability as supported, unsupported or unknown with evidence.
`session_router.register_v2` verifies exact native identity/project/provenance;
registration never launches an arbitrary command. `authenticate_actor` binds a
private credential to role, project, workflow, contract and execution revision.
The workflow-start generation is separately fenced. Environment values locate
credentials but cannot authenticate actors. Origin cannot act as user; lifecycle
host cannot approve business decisions.

`observe_session_v2` consumes a JSON-serializable bounded cursor and returns
`SessionEvent` records for the exact identity, correlation, generation, message
and turn. The canonical MessageBus owns dispatch attempts and receipts;
adapter-native journals are evidence, not a second outbox. `transport.observe_managed`
validates every correlation before applying observations. Delivery uncertainty
must remain unknown across process restart; never retry under a new message ID.
A completed tool event may describe a failed tool, so qualification must inspect
its status and independently verify artifacts. Reply/end_turn is transport
evidence and cannot satisfy workflow criteria.

Built-in examples are `ato_adapters/codex_session_adapter.py` (vendor implementation beneath
the original public shim), `ato_adapters/local_session_adapter.py` (passive CLI human roles)
and `ato_pi/pi_session_coordinator.py` (owned foreground ACP lifecycle). The real Pi
profile is `LocalPiProfile`, distinct from synthetic fixture profiles and from
the existing Pi worker. Its configuration accepts only explicit local model
data; plans cannot supply commands, modules, endpoints or credentials. Native
load is unsupported. Explicit checkpoint reconstruction verifies old shutdown,
no uncertain effects, current authority and files, then records a new identity
and additive successor mapping without rewriting historical questions.

See [capabilities](session-decoupling/capabilities.md),
[Pi lifecycle](session-decoupling/pi-coordinator.md) and
[CLI human interaction](session-decoupling/neutral-cli.md) for supported paths.
The origin-only [host handoff](history/session-decoupling/operator-handoff.md) qualifies
actual CLI plus a scripted loopback model separately from offline fixtures and
cloud inference; preparing or registering an adapter proves neither.
