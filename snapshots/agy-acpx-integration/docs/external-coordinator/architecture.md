# AGY → ATO → acpx/ACP → Codex

This is an opt-in source candidate. AGY is a replaceable external actor. ATO is
the only task, workflow, authority, STOP, attempt-history, execution-lock and
independent-acceptance ledger. acpx is a replaceable transport library. Its
protocol session and subprocess lifetime carry no task-management authority.
Nothing attaches to an existing AGY/Codex terminal, session, hub or task.

## Actors and actual boundaries

| Actor/component | Actual capability | Cannot establish |
| --- | --- | --- |
| Human operator, `external_operator.py` | Provision one private local_cli origin capability bound to exact Git root and normalized reviewed contract | New business authority, approval through model text |
| AGY, `external_coordinator.py` / `ato-control` | init, submit, status, events, result, stop, conservative continue within that capability | Human role, answer/acknowledge, grants, hub rebinding, new scope, implicit planning |
| Canonical `workflow.Workflow` / `runner.Runner` | Frozen stages, checks, repository flock, mailbox lease/fence, STOP, attempt ceilings, final criteria | Acceptance from EOF/end_turn/assistant text |
| `acpx_worker.py` | Validate frozen attempt and schema; publish a checkpoint only after validated transport/result | Native/business success from transport completion |
| `acpx_bridge.mjs` | Public acpx runtime, one fresh in-memory session and one prompt; bounded observation and shutdown | CLI queue, daemon, prompt replay, native resume |
| Pinned Codex ACP 1.1.5 | ACP/native protocol translation with acknowledged mode catalog | Override ATO policy, execute arbitrary plan-supplied binaries |
| `acpx_native_proxy.mjs` | Restricted app-server policy gateway; exactly one thread and turn | Login/logout, config writes, resume, review, goal operations, permission approval |
| Pinned native Codex 0.144.6 | Trusted model connection and tool execution under native named sandbox | Read/write ATO authority through model tools; independent task acceptance |

Private actor proof is verified through existing local_cli token machinery and
the current canonical execution revision. Environment variables locate state;
they do not prove actor identity. Synthetic local_cli origin means an external
CLI endpoint, not evidence that a native AGY session is active. Native activity
is reported unknown and native continuation unsupported.

This is a same-OS-user local trust boundary. The CLI is not a hostile-process
security boundary against programs able to read all that user's files. The host
must expose only the restricted entry and scoped actor to AGY. Human bootstrap,
root credentials, arbitrary Python execution and unrestricted native tools must
not become AGY tools. Native tools receive an actual filesystem sandbox; the
trusted native server itself necessarily consumes the separately provisioned
in-place native login. No implementation code opens or copies login content.

## Canonical lifecycle and failure handling

Submit hashes the exact repository and normalized frozen contract. A private
ATO request receipt is a creation barrier and locator only; it holds no task
status. The barrier is durable before canonical workflow creation. A crash with
no reconciled workflow stays unknown and never creates a replacement. Repeated
submit returns the original workflow; even repeated `submit --run` does not run
again. A changed contract, different repository, workflow or origin is rejected.

Continue is foreground and serialized before canonical repository/execution
locks. It passes `retry=False`, never erases STOP or resets limits/attempts, and
rejects interrupted, active, unknown or unreconciled execution. A consumed
incomplete stage requires trusted operator review. Only explicitly reviewed
stages can execute. Once exhausted, unmet criteria produce blocked, with no
implicit native planner. A completed workflow may explicitly recheck criteria.
Status/events/result do not record human read, approval, answer or acceptance.

The worker validates run/task/attempt/output/schema paths and frozen plan hash.
Correlation contains workflow ID AND full `workflow_dir`, child stage/run ID,
task, attempt sequence and fresh turn nonce. Runner launches the bridge inside
its owned process group. acpx, the gateway and native server inherit it; no
transport component detaches. STOP/cancellation/hard limits and canonical group
cleanup fence all descendants. Unknown delivery remains blocked without replay.
A known response containing invalid JSON remains a distinct result failure.
Only a matching validated acpx proof can allow host checks to promote a task.

## Native policy correction and remaining host proof

Origin's preserved model-free probes proved that an outer sandbox-exec wrapper
cannot nest the native sandbox on this host. The old outer policy function/test
is retained as a regression utility; production no longer launches it. Native
`:workspace` protects `.git/.codex/.agents` writes, and an inline named-profile
filesystem map can deny authority reads. Sources: external ops
`nested-sandbox-prerequisite.json`, `native-protected-path-prerequisite.json`,
`native-authority-read-deny-prerequisite-2.json`. Those receipts are historical
evidence; the current gateway still requires its own host qualification.

Production `acpx_profile.py` constructs `ato-worker`, extending `:workspace` or
`:read-only`, with network disabled and explicit filesystem `none` entries for
ATO/native authority, the runtime, dependencies, private host config, control
files and protected repository directories. It rejects overlapping roots,
unclean native homes, inherited project/known system Codex config, extra grants,
planner/full-access and unsupported platforms. Native config disables web,
MCP/apps/multi-agent; ACP filesystem/terminal/MCP callbacks are also disabled.
Permissions and elicitation are never approved. Network restrictions apply to
model tools; the trusted native model connection still needs account/network.

The pinned adapter supplies legacy `sandboxPolicy` every turn. The gateway
removes that field, selects the fixed named profile on thread AND turn, freezes
config/cwd/workspace roots/approval policy and supplies ATO's output schema. It
requires the native thread response to acknowledge the exact named profile and
rejects unknown methods or a second thread/turn. No fallback weakens isolation.
The adapter's optional goal-view query returns unsupported locally; no native
goal operation is performed and no healthy worker is killed by that query.
Control files live inside a unique private scratch directory and are explicitly
denied to native tools, including symlink aliases. No shared `/tmp` policy file
is used. The model cannot modify bridge input, checkpoints or authority files.

The zero-model native schema generation confirmed that thread `permissions`
and legacy `sandbox`, and turn `permissions` and `sandboxPolicy`, are mutually
exclusive. Actual native filesystem tests use the same generated policy;
subprocess fixtures observe the actual gateway rewriting and EOF cleanup. An
additional fixture connects the actual pinned ACP adapter through public acpx
and the gateway to an offline native-protocol peer, including commentary/final
metadata and the optional unsupported capability query. Full
native turn behavior, native login and provider readiness remain origin gates.
The worker never executes a real model or claims these gates passed.

## Results and replaceable transport

The bridge uses public `createAcpRuntime`, `ensureSession`, mode acknowledgment,
`startTurn`, `turn.result`, `turn.assurance`, observation and shutdown. Session
storage is bounded/in-memory. One exact process launch is allowed; version
probes/reconnect/package-exec/alternate executables are rejected before spawn.
No acpx public interface is modified or made dependent on ATO.

The pinned Codex parser uses public `onAcpMessage` to retain `messageId` and
`_meta.codex.phase`. It accepts commentary followed by exactly one final_answer
message, retaining chunk boundaries per identity. Missing/changing phase,
multiple finals, late commentary, foreign sessions, truncation or oversized
streams fail closed. No regex, JSON fence or prose extraction is permitted.
This parser is specific to the qualified pin; unsupported metadata is a real
prerequisite, not a reason to treat the last assistant text as a final result.

A completed/end_turn + matching finished/response receipt proves protocol
completion only. Strict JSON rejects duplicate keys, nonfinite values, trailing
objects and invalid schema. Only bounded safe counters/hashed native identity
and correlation are stored in receipts; no raw wire/tool body/native stderr or
credential is persisted. Backend outcome and business validation stay unknown
at this boundary. Runner executes the original command checks independently;
workflow completion requires all original overall criteria. Coverage of tool
observation is explicitly partial, never proof of all native behavior.
