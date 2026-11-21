# Managed Pi ACP coordinator

This is the Pi lifecycle stage of the frozen six-step task in `task.md`. The
`acp_pi` worker is retained unchanged. The distinct code-owned `pi_acp` session
adapter uses a foreground `PiCoordinator`, private control endpoint and a
persistent native identity/event journal. Importing `agents` only registers the
adapter; it starts no CLI, provider, supervisor, scheduler or observer.

## Lifecycle and delivery authority

`PiSessionAdapter.create_managed_session(profile, project, ...)` creates a fresh
owned runtime and native session and returns `(host, SessionReference)`. The
caller must keep the host's event loop running and close the host in `finally`.
The separate foreground entry point supports an explicit operator-owned process:

```text
PYTHON pi_session_coordinator.py --project /temporary/project \
  --local-config /private/local-loopback-config.json --sandbox workspace-write --create
```

The configuration is a private 0600 JSON file containing `model` and `models`.
`models.providers` must explicitly use an HTTP loopback endpoint with a port,
`apiKey: "ato-local-dummy"`, a supported protocol (`openai-completions`,
`openai-responses` or `anthropic-messages`), and an explicit list of models.
Model entries cannot override endpoints, keys, headers or protocols. The profile
is for the scripted local model qualification; it does not use real providers.
It never reads/copies the user's native auth or inherits provider credentials.
The actual CLI qualification belongs to the later final delivery stage.

Without `--create`, the foreground host advertises only a prepared provisioning
locator. The existing synchronous `create_session(repo, contract, name, id,
persist)` contract can then create once through this already-running host. It
never starts a detached helper. Concurrent or repeated creation fails closed;
creation ambiguity is not retried. The host rejects contract permission
widening and requires its exact project and sandbox. `created_reference` emits
a version-2 opaque reference for this adapter while retaining older adapters'
public compatibility behavior.

The foreground host's private runtime lives under the explicit ATO state root,
outside the model workspace. It holds a live flock, a random control credential,
a unique process-instance nonce and one native child process group. Native
creation persists a possible-effect barrier before native IO. Only the exact
native ID attested by the pinned adapter is registered; the upstream random-ID
fallback is rejected. The immutable native index is reserved with exclusive
creation, so another host cannot overwrite/adopt the same identity. Persisted
PIDs are diagnostic only. Cancellation/shutdown signals only the child group
owned by the live subprocess handle, after checking its group leader. No PID
scan, PID adoption or cross-session kill exists. If the process/stream disappears
without verified shutdown, activity remains unknown.

The Unix control socket has a short per-runtime hash path under a private
0700 temporary directory, avoiding macOS's Unix path-length limit. Every control
request needs the private host token and exact host ID. Only send, new, status,
bind and cancel are implemented. It exposes no shell, terminal, arbitrary argv,
filesystem callback, dynamic imports or user approval operation. Socket requests
and ACP frames are bounded to 1 MiB and operations have finite deadlines.

`transport.prepare_managed` and `dispatch_managed` retain the canonical outbox
and its endpoint/route/attempt/execution fences. Pi accepts only a complete v2
mailbox envelope, with exact project/session and canonical message/correlation
IDs. Host admission is a transport receipt only. The native admission barrier
is saved before writing stdin. At most one turn is active. Duplicate message IDs
return their prior receipt/unknown disposition without another prompt. A lost
control response, native timeout, protocol error or EOF cannot become a safe
retry. No replay/restart loop is added.

Each active-turn native update carries the exact host nonce captured when the
adapter emits it. Startup, historical, wrong-session and wrong-turn updates do
not settle requests. Native `stop` evidence and ACP `end_turn` must both agree;
native errors are sticky and cannot be repaired by a subsequent success chunk.
A model reply or EOF never completes a workflow. The journal projects only
correlated message/response/turn evidence into the existing mailbox, whose
independent acceptance and user-only/manual decisions remain unchanged.

The private 0600 journal is append/fsync persisted, capped at 8 MiB, with a
monotonic serializable cursor. The host admits at most 256 turns; hitting a bound
requires explicit host review, not silent rotation/history loss. Tool evidence
contains bounded tool IDs/status, not raw tool arguments/results. Reply text is
bounded to 6000 characters. Raw native stderr, exception text, prompts and
control credentials are not copied into the journal. A fresh adapter process
can observe the same exact native identity and cursor without Codex.

## File policy and existing execution ownership

The real profile pins Pi 1.5.2, Node's fixed executable and pi-acp 0.0.34 at SHA256
`324aeb8bba1228937e16b1326fb3e014e2a625b2dea549dbcd49b006cd5df6a2`.
It derives an owned copy of the pinned dependency without modifying node_modules.
The version probe uses an empty private HOME. The launch records binary and
adapter hashes and explicitly does not claim actual-CLI/cloud qualification.
Pi is launched offline with auto-discovered extensions, skills, MCP, context,
prompt templates and themes disabled. The only explicit extension is the
candidate's trusted file guard. Read-only offers `read`; workspace-write offers
`read,edit,write`. No glob/tree search, shell or task-management command is
available to the coordinator model. ACP client permission requests are denied,
and unknown client callbacks are rejected. Trusted CLI/schema host integration
for human interaction belongs to the next neutral-hub stage; model text cannot
supply a user approval.

The OS sandbox limits writes to the owned runtime and permitted project files,
excludes internal directories, and restricts the model client to the explicitly
configured loopback ports. The mandatory file extension rejects symlink escapes,
directory enumeration, internal `.git/.codex/.ato/.pi/.aws/.ssh/.env` components,
unknown tools and read-only writes. It also checks the live owned turn and its
private tool-authority record on every file call. For actual workflow bindings
it checks STOP, user closure, contract/origin bytes, execution revision and the
workflow event snapshot. A changed workflow event stream fails closed rather
than letting a stale generation continue tools. A normal prompt cannot invoke
the adapter's slash commands because the host wraps work as literal text.

`bind_workflow(identity, directory)` is a trusted host operation after native
creation. It validates the exact frozen origin/session/project/contract and
current generation and saves an immutable binding. For workflow messages,
dispatch repeats STOP/closure/expiry/contract/generation/exact-origin checks and
joins the existing repository flock and durable `session_host` execution lease.
A competing worker/origin lease prevents native execution. The host holds this
ownership over its prompt; unknown native activity retains the existing lease
quarantine. This does not create business supervision, repair allowance or new
attempt accounting.

The `pi-host-token` actor proof authenticates only `session_host`, with exact
workflow, contract and generation bindings. Ambient environment locators and
legacy `is_actor` never authenticate this adapter. This actor can operate its
owned lifecycle, but cannot answer/approve/accept human decisions. Trusted host
code can separately call `origin_actor_proof` after validating the assigned
frozen workflow origin. Its independent `pi-origin-token` grants only the
existing origin role (ask/report/accept a routed answer), never lifecycle or user
decision authority. This grant is not exposed to model/ACP/control-socket calls.
The next hub stage must use this origin proof and a separate actual user endpoint.

## Honest recovery

The pinned ACP adapter advertises native load. This candidate deliberately
reports `session.load_resume=unsupported`: advertisement and worker capability
are not an independent native-load qualification. The normal router therefore
rejects native load/resume before invoking anything. No fallback is labelled
native resume. The `pi` and `acp_pi` worker declarations remain unsupported as
coordinator paths; the separate `pi_acp` adapter has actual lifecycle methods.

`reconstruct_session(previous_reference, checkpoint, profile, actor=...,
workflow_id=..., contract_hash=..., generation=...)` is an explicit async operation.
It requires the exact authenticated previous session-host actor, released old
flock, recorded owned-process shutdown, no unfinished/unknown prior turn, and
current workflow authority. Checkpoint fields are exact: `full_goal`,
`current_files` (path/hash evidence), `previous_results`, `cumulative_history`.
The full frozen objective and current file hashes are verified. Nothing is sent
to the new model until a new explicit message arrives; that message receives the
reviewed checkpoint as context, never as instructions to replay prior effects.
The operation returns a new native ID and opaque reference, preserving the old
reference, complete checkpoint, native lineage and prior journal.

For an actual workflow, reconstruction appends a verified
`session_reconstructions` link to origin metadata. The original opaque origin
reference is retained. The neutral router validates the exact adapter's
successor proof and current workflow fence before future origin work follows
that link. Historical mailbox endpoints/canonical IDs are never rebound or
resent; direct resolution of an old reference still returns its old native
identity. Contract, attempts, criteria, budgets and workflow state are unchanged.
Invalid, stopped, stale or forged successor links fail closed. The chain is
bounded to 16 explicitly reviewed reconstructions, not an automatic retry budget.

## Qualification boundary

`tests/test_pi_session_coordinator.py` uses a real local ACP subprocess fixture
and separate Node-only file-policy fixtures with Codex unavailable. These are
synthetic process/protocol tests, not actual Pi/model qualification. The socket
case remains a required test even where the worker sandbox denies `bind()`.
The scheduler's original host `acceptance.py pi` command must run the entire
suite with zero failures/errors/skips. See `pi-checkpoint.md` for current evidence.
No real Pi/provider invocation, native auth reading, live activation, real
hub/origin binding, installation or Git publication was performed in this stage.
