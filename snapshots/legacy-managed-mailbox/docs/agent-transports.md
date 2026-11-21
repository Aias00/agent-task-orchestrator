# Managed agent transport boundary

The contract is implemented in `transport.py`; native adapters are implemented in
`message_adapters.py`. Persistent storage lives in `message_bus.py`.
The public CLI routes interaction, recovery, controller and delegation delivery
through `mailbox_routes.py`, retaining the existing JSON permission ledgers.
No live runtime is activated. Native local-provider qualification is run by the
external host because the worker sandbox denies loopback socket binding. See
`message-adapter-validation.md` for independent host results. The complete
frozen scope remains in [`agent-neutral-mailbox-task.md`](agent-neutral-mailbox-task.md).

## Public Python interface

`transport.ManagedTransport` is a structural protocol:

```python
class ManagedTransport(Protocol):
    def send(self, endpoint: dict, message: dict) -> DeliveryResult: ...
```

`endpoint` includes the registered `id`, `kind`, native `session_id`, canonical
`project` and verification `source`. It omits credential hashes and owner
capabilities. `message` includes the durable ID, sender/target, `kind`,
`correlation_id`, `reply_to`, binding/task/contract provenance, body, expiry and
state. The host strips claim, ownership and dispatch-attempt capabilities before
calling the adapter. These are host inputs; adapters must select prompt fields
explicitly rather than serialize every field into model instructions.

An adapter returns `DeliveryResult(state, external_id=None)`:

| State | Required evidence | Consequence |
| --- | --- | --- |
| `delivered` | Exact native target transport positively received this message | Persist receipt, keep endpoint busy awaiting receiver acceptance/finish |
| `delivery_unknown` | Work might have reached the native CLI, including timeout after write | Persist ambiguity, hold endpoint slot, never automatically resend |
| `failed` | Positive evidence nothing reached the native session | Release slot; original message remains failed, no implicit retry |

`dispatch_one(bus, receiver_credential, owner_token, transports,
lease_seconds=30)` looks up the registered kind in an explicit adapter mapping.
If missing, it fails before claim/send. It claims at most one FIFO message,
commits the dispatch barrier and invokes `send`. It commits the receipt and
returns the stored message, or `None` when busy/empty. Exceptions become
`delivery_unknown` without storing exception text, which can contain credentials.
Process termination/`BaseException` leaves the barrier for recovery. It starts
no daemon, scheduler or detached process. The host must maintain ownership,
enforce canonical task validity and manage native process lifetime.

Adapters normalize native responses to `TransportEvent(kind, message_id,
endpoint_id, session_id, receipt_id, source, text='')`. `receive_event(bus,
receiver_credential, event)` checks the exact session/endpoint/source before
applying `accepted`, `reply` or `completed`. It calls authenticated bus receiver
APIs, so an unrelated credential is also rejected. Reply text is work material,
not user consent. Workflow lifecycle remains under the existing controller/recovery authority.
Normalized native replies can contain the explicit question protocol below;
`receive_event` itself never mutates a workflow or grants user permission.

## Managed native adapter API

`ClaudeAdapter` and `PiAdapter` take explicit `endpoint_id`, UUID `session_id`,
existing canonical `project`, private `runtime`, optional `executable`, `model`,
`provider_env`, `tools` and `timeout`. Construction starts nothing. `start()` or a
context manager starts one owned foreground child; `close()` closes input,
waits for orderly shutdown, escalates TERM/KILL if needed and joins the reader.
The adapter stays in the host process group so the existing worker STOP/timeout
group cleanup still applies. It starts no scheduler, daemon or detached job.

`runtime/owner.lock` uses an exclusive process-lifetime flock. `session.json`
records the immutable identity and tool policy, pending message, lifecycle and
native transcript location; writes use fsync and atomic replacement. Thread
serialization also protects callers sharing one adapter object. Message-bus
leases remain required: the native lock supplements them, not replaces them.

After `dispatch_one(...)`, call `adapter.events()` to wait for that turn's native
terminal event. Apply `accepted` and `reply` through `receive_event`; `completed`
is also emitted as an observation for the controller (the bus reply already
finishes the turn). `recorded_events()` replays fsynced normalized receipts after
host failure; bus receipt IDs deduplicate them. It ignores an incomplete final
JSONL line and never infers a new identity from unrelated history. A reply is
work material; it is not user authorization or proof of workflow execution.

Claude runs `--input-format stream-json --output-format stream-json` with
`--session-id`, `--replay-user-messages`, and native persistence. A submitted user
UUID and exact session match must be acknowledged before accepting its result.
There is only one outstanding turn. Restart uses `--resume EXACT_ID`, never
`--continue` or latest-session lookup. Pi runs `--mode rpc --session-id EXACT_ID`
in an explicit session directory. `get_state` verifies its session ID and file;
prompt responses match the mailbox ID. `agent_end` is insufficient: only a
successful assistant message followed by `agent_settled` finishes the turn.
LF framing preserves literal Unicode paragraph separators inside JSON strings.

An idle restart requires the recorded native transcript to exist. Missing
transcripts cannot silently become an empty session with the same ID. A crash,
timeout, wrong acknowledgement or protocol failure after the durable pending
barrier leaves `delivery_unknown` and prevents restart/replay. The host must
inspect native evidence, replay already recorded events, and explicitly
reconcile the bus with verified `not_running`/`finished` evidence. No automatic
resolution or budget reset is supplied. Pending runtime metadata remains
quarantined for explicit operator reconciliation; deleting it is not retry.

The environment is not inherited beyond PATH/LANG. The caller explicitly supplies
provider settings; Claude config, Pi config, cache, tmp and session paths
are fixed inside the private runtime. HOME and CODEX_HOME are never replaced,
and provider settings cannot override them. No native auth file is copied. `tools=none`
is the default and disables all model tools. `read-only`/`workspace-write` enable
only the existing file-tool allowlists, require macOS sandbox-exec, deny writes
outside the project/runtime (and project `.git`/`.codex`), and use Claude
restricted mode or Pi's realpath guard. Hooks, MCP, extensions, skills and context
file discovery are disabled; only Pi's explicit file path guard can be loaded.
The host checks model work against task scope; endpoint registration is not
permission to run Bash, sub-agents or provider configuration commands.

`CodexQueueAdapter(home=..., verifier=...)` packages queue invocation and strict
receipt parsing. `verifier(home, thread, project)` must verify the exact native
metadata/project binding before send. It preserves `CTASK_QUEUE_BIN` fixtures.
A missing executable is `failed`; timeout, wrong-thread receipt, nonzero status
or malformed output are `delivery_unknown`. Neither stderr nor credential-bearing
environment values are copied into mailbox delivery receipts. Existing
app-server session creation remains in `appserver.py`; core routing no longer
constructs native queue argv.

## Whole-goal integration requirements

Native identity, ownership, correlation, failure and transcript resume behavior
are covered with actual protocol fixture subprocesses. Real CLI qualification
must separately prove at least two native turns. A fresh empty session per
message is not continuation.

| Kind | Native channel to qualify | Scope |
| --- | --- | --- |
| `codex` | Existing queue/app-server behavior, preserving `CTASK_QUEUE_BIN` fixtures | Native command/receipt logic belongs in its adapter |
| `claude` | Installed Claude Code stream-json input/output or explicit native resume | Framework-managed CLI only, explicit session/project verification |
| `pi` | Installed Pi RPC or explicit native session | Framework-managed CLI only, explicit session/project verification |

Endpoint labels are agent-neutral and need not be Codex UUIDs. Transport kinds
are validated strings; the host mapping controls adapter availability.
Registration does not install or launch a program. Arbitrary existing TTYs are
outside this interface. Tool permissions must stay at or below existing file-only
worker guards: coordinating an endpoint does not permit Bash, MCP, sub-agents or
expanded filesystem access.

Integration must route a non-Codex origin's question to the verified hub, relay
only the user's actual sourced answer, and distinguish origin acceptance from
real execution. Recovery/controller must preserve old JSON fields, Codex
identities, budgets, failures and bounded takeover history. Stale routes must be
invalidated on STOP, contract/generation changes, expiry and user deferral.
Identity/permission cannot be guessed from complete history.

## Verification boundaries

`tests/test_message_bus.py` exercises SQLite lifecycle/concurrency. The adapter
tests in `tests/test_message_adapters.py` launch explicit protocol fixture
subprocesses; they never claim those fixtures are installed native CLIs.

`scripts/verify_native_mailbox.py --output PATH` resolves the installed Claude/Pi
executables, records their SHA256/version/help, creates isolated configs and
projects and starts a loopback-only Anthropic-compatible streaming fixture with
an invented key. Each CLI receives two mailbox messages in one process/session.
The second actual provider request must include the first assistant's unique
memory value, and both returned replies must correlate to their own mailbox IDs.
The probe records provider requests, normalized events and receipts beside the
JSON report, then cleans up processes/configs. It fails nonzero if any evidence
is missing; it does not skip a CLI, replace a native binary with a fixture, or
copy real credentials. The report uses the frozen schema_version=1 interface.

The external host previously passed Claude 2.1.325 and Pi 1.5.2 with two turns
per native session; retained executable hashes/receipts are in
`build/message-transport/native-host-report.json`. Bus/adapter integration inputs
changed afterward, so this is historical evidence and fresh host native
qualification is required. The restricted worker cannot bind the fixture socket
or nest sandbox-exec and does not repeat those known failures. No cloud-account
authentication is established. Current fixture/regression and delivery status is
in [message-delivery-validation.md](message-delivery-validation.md).

## Managed workflow hosts and commands

A trusted local host registers a fresh explicit native session using
`mailbox_routes.register_managed(kind, session_id, project, runtime, source, ...)`.
The runtime must be empty and isolated. Registration starts no process. The CLI
exposes `mailbox-register --kind claude|pi --session UUID --repo PATH --runtime PATH
--source TEXT`; it prints only the public endpoint ID. No provider key is stored
in the registry. The Python host can supply explicit isolated `provider_env` to
`pump(endpoint_id, provider_env=...)`; CLI `mailbox-pump --endpoint ID` uses the
adapter's clean environment and does one foreground turn, never a scheduler.
An explicit Codex endpoint pump drains one stored unsent enqueue after a busy
host releases ownership; it never replays unknown/dispatching envelopes.
These commands are implementation interfaces, not an activation of this machine's
running runtime. Tests use temporary Codex homes and protocol executables only.

`workflow-create --origin-endpoint ID` binds a managed origin; existing
`--origin-thread UUID` remains Codex-only. `delegate --origin-endpoint ID` uses an
explicitly pre-registered fresh managed coordinator; it does not discover or
inject into an existing terminal. Default delegate retains app-server creation
and native policy/readback checks. A delegation still requires acceptance before
the existing bridge may launch its bound workflow. No new scheduler is introduced.

`mailbox-state --endpoint ID --state paused|active|closed --source TEXT` records an
explicit endpoint lifecycle change. Paused origins do not receive native turns,
recovery attempts or controller reminders. Reactivation does not replay cancelled
work or reset budgets. STOP and changed generations/contracts also fence queued
messages and late replies. Empty/cancelled queues do not start a CLI.

Managed models have **no tools** in this integration host. A model may return:

```json
{"ctask":{"kind":"question","question":"Which compatibility option?","recommendation":"Keep compatibility","context":"Reason"}}
```

The host verifies the reply's endpoint/session/message/task/generation and uses its
private receiver capability to route `question`/`needs_input` through the existing
hub. For a current recovery request, `needs_input` and `blocked` follow the bounded
`takeover` transition instead. A model-provided answer/permission event is ignored.
A host replay deduplicates the question and cannot revive a closed recovery request.
No inherited hub conversation variable is treated as a live local user turn when
a native host generates a question.

Only a real user's sourced `answer` is sent back. The origin host must explicitly
record `accept-answer` after native delivery, either by its bound API/command or
by the model returning `{"ctask":{"kind":"accept_answer","question_id":"question-..."}}`
in the exact answer-triggered turn; receipt or a model result does not
imply acceptance or resumed execution. Trusted host APIs use
`actor_environment(endpoint_id)` for bound command invocation, or a validated
`EndpointCredential` for event routing. Capabilities stay in the local host and
are not passed to model prompts, native provider environments or CLI output.
The managed host remains responsible for presenting questions and relaying actual
human answers; no arbitrary TTY injection or automatic user consent exists.

`tests/test_agent_mailbox_integration.py` exercises both kinds with real protocol
subprocess fixtures, two resumed turns, generated questions, mixed managed hub/
origin identities, recovery and delegation acceptance, crash barriers, STOP races,
expiry, pause, changed identity/contract/generation and old JSON. These fixtures do
not replace native local-provider host qualification or cloud-account validation.

The adapter supplies the bounded event protocol inside managed workflow envelopes,
so models with tools disabled can request host actions without invoking Bash.
`accept_answer` is matched to the exact persisted answer delivery and actual user
source. `accept_delegation` likewise matches only the current delegation envelope.
Replayed acceptance events do not start workflows, reopen expired delegation or
reset recovery counters. The existing dispatcher remains responsible for the
already-authorized launch after acceptance.


## Managed coordinator operation and delivery

An endpoint registration is an explicit host identity, not proof that a native
process has started. `mailbox-pump` owns one foreground turn; nobody is promised
automatic model wake-up while the host is absent. A framework host must keep
pumping authorized pending work, present questions to the user, source actual
answers, and perform permitted workflow/recovery operations. The no-tool model
protocol supports questions, needs-input/blocked transitions for current recovery
requests, and exact answer/delegation acceptance; it is not a general protocol
for executing shell commands or automatically repairing code. File-tool stage
workers remain under `agents.py` and independent host checks.

The CLI pump scrubs ambient credentials and does not use an existing native login.
A trusted Python host supplies explicit provider settings via `provider_env`; it
must resolve its provider authentication outside model input/recorded artifacts.
This release does not include an interactive native-login bootstrap for the CLI
pump, or cloud-account qualification. The local native probe supplies only invented
credentials to a loopback fixture. Environment isolation must not replace HOME or
CODEX_HOME, and must preserve the existing file-only adapter limits.

Use [runtime-upgrade.md](runtime-upgrade.md) to assess/stage local source and rehearse
legacy-state restart/rollback. Installation does not copy accounts, activate an
existing hub, register endpoints or start a scheduler. New managed origins require
the new runtime; preserve transcripts, private capabilities, all current budgets
and uncertain deliveries before any manual rollback.
