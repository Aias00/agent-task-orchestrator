# Task-owned native worker turns (inactive candidate)

This stage adds an explicit policy to Runner plans and Workflow contracts:

```json
"worker_session_policy": {"version": 1, "mode": "task_bound_native"}
```

Existing tasks without the policy retain their original adapter command, result
schema and retry behavior. Workflow copies the frozen policy into its stages;
planners cannot choose a native identity or adapter implementation. Stage plans
cannot override the parent policy. A workflow stage is executed under its real
parent Runner owner; it cannot be launched independently to borrow that owner's
authority. This feature is a local inactive candidate, not a live installation.

`worker_sessions.py` is the neutral Runner integration. It uses the existing
registered adapter, private atomic receipt writer, process cleanup and STOP
guards, `SessionIdentity`/`SessionEvent` and `DeliveryResult` contracts. There is
no additional observer, mailbox pump, scheduler, service or repair process.
Private receipts are created by task creation under ATO_HOME/worker-sessions-v1,
outside the model-writable project. Missing receipts, changed ownership/modes,
changed task bindings or uncertain claims cannot be silently reconstructed.

The default Codex adapter delegates native details to `codex_worker_session.py`.
A cold turn creates a new task-specific native source, obtains the exact UUID
from the exec JSON stream, and reads back only that new task-owned native file
through the exact native index lookup. A subsequent turn invokes `exec resume`
with that verified UUID. It never uses `--last`, session names, arbitrary TTY
input, origin/hub sessions or model-selected native IDs. The unique message and
native turn must agree with the appended native lifecycle and effective model,
project, approval and sandbox context. Source mismatch, missing capabilities or
unsupported native record formats fail closed.

The recognized installed Codex queue launcher is resolved to its immutable
native payload before execution. The payload, launcher, actual task source
manifest, native-home locator, hashed configuration inputs, selected model and
permission boundary are pinned in the task receipt. Configuration contents and
authentication material are never copied into the receipt. No authentication
file is read. Native configuration files are only hashed and never modified.
The same sandbox/approval/explicit roots are supplied on both turns. The default
workspace-write network grant is explicitly false and additional roots default
to an empty list; an explicit authorized grant is preserved. The effective
native context is independently checked again after each turn.

A claim is durable before dispatch. An EOF, malformed response, wrong ID,
missing native lifecycle, timeout, STOP during delivery or unconfirmed cleanup
keeps the delivery unknown. Retry, host-checks and host-recheck cannot supply the
missing native receipt or start another session. Eligibility refusals before
dispatch are recorded separately and do not mint an invocation/failure fee.
Successful continue uses the existing v2 accounting and source/checkpoint
progress mechanisms. A native end of turn is never independent acceptance.

Native session reuse is distinct from process reuse: this adapter starts one
bounded CLI process per turn and reuses the exact native context. Persistent
processes and checkpoint-request/steer are unsupported. Existing file-worker
adapters have no worker-native continuation implementation. The worker-specific
capability declaration does not upgrade generic origin-session capabilities or
authentication. Installed-CLI native qualification is still origin-owned;
protocol fixtures cannot certify a provider, deployment or speedup.

Warm prompts preserve the complete authorized objective and assigned stage,
carry bounded latest feedback, and omit the repeated memory/predecessor dump.
An unchanged native source prefix and exact input generation are prerequisites
for the next turn. Parent workflow generation changes fail closed; no implicit
owner transfer, adoption or checkpoint reconstruction is implemented here.
The full incremental checkpoint, verification-cache, metrics, benchmark and
portable delivery stages remain separate requirements in task.md.
