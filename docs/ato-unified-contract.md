# ATO unified contract v1

T1 specifies the interface for new tasks. T2–T7 implement and qualify it in the
order in the [approved plan](ato-unification-task-plan.md). The
[implementation baseline](ato-unified-baseline.md) includes the four execution
fixes that this interface relies on. The protected eight modules and their
baseline fixtures remain byte-identical. Workflow owns execution and technical
checks, and the unified service binds those facts and authenticated human
decisions to one public Task ID. The existing runtime and data stay in their
original namespaces. Source qualification and runtime activation have separate
authorization and evidence.

## Public CLI

`ato` has eight daily commands. `ato --help` lists them and the three
management groups. `ato project --help`, `ato agent --help` and
`ato system --help` list their own operations. Help exits zero and works without
state, credentials, Git or adapters. There are no aliases, hidden runtime
commands, directory-form Task IDs or public Run handles to pass as arguments.
Every command other than help writes one JSON object and a newline to stdout.
There is no human/JSON mode switch. Shell scripts consume named fields.

| Command | Result data and effect |
| --- | --- |
| `ato run --repo PATH --spec FILE [--foreground]` | Validate and freeze the specification, allocate a new Task and its first Run, and admit it through the same launcher used by resume. Returns the task status record and only reports a live launch once the exact owner and startup receipt are verified. Default is background. Foreground waits for the same Run to settle, including at an external or human boundary with no further active work. |
| `ato list [--repo PATH]` | `{tasks: [TaskSummary], warnings: [code]}` sorted by Task ID. Omitting the repo lists visible Tasks across repositories. Filtering is exact by normalized Git identity. |
| `ato status TASK_ID` | Returns `TaskStatus` with overall state, actual execution, Run history, stage, checks, review, product, acceptance, pending items, delivery, and next step. |
| `ato stop TASK_ID` | Durably fences the observed Run, then returns `TaskStatus` with the actual stop or cleanup progress. Repetition returns the existing disposition. |
| `ato resume TASK_ID` | Explicitly admits execution under the Task's current controller authority and recovery checks. When repository contention left an untouched initial reservation, resumes the same initial Run. A proved retryable prior execution requires a successor Run. Returns `TaskStatus` with the same background admission and startup proof as run. |
| `ato inbox` | `{items: [InboxItem]}` containing authorized pending items, in creation order with ID as tie breaker. A read has no presentation or delivery effect. |
| `ato answer REQUEST_ID --answer TEXT` | Records the actual user's answer to the exact pending request. Returns `{request_id, task_id, state, delivery, recorded_at}`, where `delivery` reports the canonical disposition. The execution owner consumes a valid decision separately. |
| `ato accept TASK_ID [--evidence FILE]` | Optionally records explicit human acceptance separately from task completion. A scoped actor supplies exact product evidence with `--evidence`. Returns `TaskStatus` with the receipt reference. |

The following is the v1 management surface. Maintenance, transport and
release machinery outside this table is host code.

| Group | Exact operations |
| --- | --- |
| `project` | `validate --repo PATH --spec FILE` validates and previews the normalized repository, base commit and structural digest without creating state. `trace TASK_ID` returns bounded, redacted execution and delivery summaries. `recheck TASK_ID [--checks-only]` performs a foreground technical recheck of the already bound product under existing execution fences. `cleanup TASK_ID --confirm` removes an eligible clean worktree and retains its branch and evidence. |
| `agent` | `doctor` returns installed/registered Worker, Reviewer and session capabilities with version/probe evidence and separate readiness. `binding TASK_ID` returns the scoped controller, interaction, Reviewer and actual Run owner summary. `handoff TASK_ID --to SESSION_REF [--interaction SESSION_REF] --evidence FILE` performs the binding transition below. `reviewer TASK_ID --kind KIND [--model MODEL] [--effort EFFORT] --evidence FILE` selects a permitted Reviewer for subsequent technical review. `ask TASK_ID --kind KIND --question TEXT --reason REASON [--continuation OP] [--purpose-ref REF]` records a canonical request under the current controller's scoped grant. |
| `system` | `status` reports schema/release, observer identity and health, and mailbox summary in the selected namespace. `notification-policy [--repo PATH]` reports effective policy and its disposition for the optional repository. Both are read-only and redact private locators. |

`project recheck` invokes only the authorized frozen host checks, and by
default the configured Reviewer when present. `--checks-only` omits the
Reviewer. Neither form launches a Worker, planner or repair, clears STOP, or
satisfies a manual criterion. When a frozen `host_handoff` is pending and
actual host work is recorded, checks-only may advance its existing technical
gates under the same lease. Preserve an already-passed review only when its
product, spec, check set and review revision still match. A changed product
or Reviewer requires fresh qualifying evidence. An explicit recheck can refresh
the checks and review of a completed product. Cleanup requires
controller or scoped operator authority, proven idle execution and terminal
completion or STOP. Apply the
existing H1 checks for branch/common-dir/links and dirty tree. Record removal
once, preserve the branch, accepted receipt and retained product/check
evidence. For accepted cleanup, verify the retained clean Git tree matches
the accepted product before removal. A stopped Task whose worktree was
removed is sealed against resume.

### Parsing and command results

Use Python 3.12 standard-library `argparse` and JSON. Dispatch typed inputs to
services after parsing. Disable long-option abbreviation on every parser,
reject repeated scalar/boolean options and extra operands, and let `--` end
options. Pass a value starting with `-` as `--option=value`. There is no
config/response file expansion, implicit stdin input,
shell parsing, plugin loading or environment interpolation. Reject bare
legacy flags, including `--plan`, `--retry-failed`, `--accepted-answer`,
`--actor-file`, `--source`, `--worker-codex-home` and `--proxy`. Trusted
host configuration supplies authorities and credentials as described below.

Task IDs are `task-` plus 32 lowercase hexadecimal digits from a random
UUID. Internal Run and request IDs are `run-` and `request-` with the same
suffix format. Match the complete token before storage lookup. A legacy
workflow directory, H1 ID, slug, symlink or shortened ID is invalid. Request IDs
identify human interactions. They confer no task access. Criterion, stage and
step IDs use `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` in their respective scopes.
Validate, then preserve the exact UTF-8 answer bytes, including internal
whitespace. Reject blank text, NUL, invalid Unicode and answers over 8 KiB.
No normalization of a human's answer participates in authorization.

CLI paths may be absolute or relative to the caller's working directory.
For repositories, resolve to the actual Git worktree root using the exact
supplied directory. A subdirectory of a worktree, bare repository or non-Git
directory fails validation. Resolve an alias to its canonical path before
comparing. The repository key is the canonical absolute shared Git common
directory, so linked worktrees match and distinct clones do not. The
repository display/provisioning root is the canonical primary worktree from
Git's worktree list. Verify that this root, Git common directory and the
selected worktree agree. Freeze the key and root, and reject a moved or
replaced binding when using it for execution. A repo inside the ATO store,
a provisioned task directory as a worktree parent, or an overlapping native
home is invalid. Sanitize Git overrides and use trusted Git with argv and
local-only commands. Validate paths before passing pathspecs or refs to Git.

For spec and evidence files, open a single existing regular, non-symlink file.
Reject unsafe path components or redirected final opens, hard-link aliases
and special files. Validate the opened file, bound reads to 1 MiB and detect
replacement or concurrent alteration. Hash and parse the same bytes.
Relative paths **inside** a spec are either the `repo` path relative to the
spec file's parent, or repository-relative design-doc paths. Paths inside
acceptance evidence are never dereferenced. For state, credentials and
seals, require private/canonical boundaries below. Parser diagnostics never
reflect supplied argument strings, paths or file contents.

The response envelope is
`{"schema_version":1,"ok":true,"data":{...},"error":null}` or
`{"schema_version":1,"ok":false,"data":null,"error":{"code":"...","message":"...","task_id":null}}`.
Use code-owned text for `message`. Once a visible Task has been committed,
a failed startup or foreground operation includes its Task ID. Unauthenticated
and cross-scope lookups do not return identifiers or distinguish existence.
Internal services raise a typed error with a stable code and optional
authorized Task ID. Map unrecognized exceptions to `INTERNAL_ERROR` and keep
diagnostic material private. stdout never carries worker output, captured check
output, tokens, native identities, raw adapter errors or tracebacks. stderr is
reserved for sanitized operator diagnostics. The following exit classes are
stable. Successful status/list/inbox calls return 0 for blocked or failed Tasks
as well.

| Exit | Meaning and v1 error codes |
| --- | --- |
| 0 | Operation succeeded or repeated an identical authorized decision. For foreground `run`, settled successfully in `completed`. A recorded STOP or human answer can return 0 with unresolved cleanup or delivery explicitly represented in its result. |
| 2 | Invalid invocation or data. `USAGE`, `INVALID_ID`, `INVALID_REPO`, `INVALID_JSON`, `UNSUPPORTED_VERSION`, `INVALID_SPEC`, `INVALID_EVIDENCE`, `UNSUPPORTED_CAPABILITY`. |
| 3 | `AUTH_REQUIRED` for missing/invalid credentials, `NOT_FOUND` for absent or out-of-scope IDs, `FORBIDDEN` for a visible object and an unauthorized action. |
| 4 | Valid operation cannot proceed or its foreground work reached a non-success boundary. `BUSY`, `NOT_RESUMABLE`, `STOPPED`, `STALE_REQUEST`, `BINDING_CHANGED`, `DECISION_CONFLICT`, `PRODUCT_CHANGED`, `CHECKS_REQUIRED`, `LAUNCH_FAILED`, `CLEANUP_BLOCKED`, `DELIVERY_UNKNOWN`, `RUN_UNSETTLED`. |
| 5 | `STATE_CORRUPT`, `SPEC_CHANGED`, `RUNTIME_MISMATCH`, `INTERNAL_ERROR`. Reconciliation requires reliable evidence. |

Use a stable action/reason code alongside any more specific kernel status.
For a foreground wait interrupted by the controlling terminal, request the
same durable STOP and check the owned cleanup result. Return `STOPPED` or
`CLEANUP_BLOCKED` with the Task ID. A background parent whose child startup
result is indeterminate returns `RUN_UNSETTLED`. The retained launch record
is reconciled by identity and never blindly retried.

## Frozen task specification

The input is a single strict UTF-8 JSON object without BOM. Reject duplicate
keys at all depths, nonfinite numbers (including overflow), invalid Unicode,
trailing data, and nesting deeper than 32. Check exact allowed keys recursively.
Integers are integers, including positive limits, and booleans are booleans.
Use the existing Workflow/Runner and code-owned adapter/permission validators
on a private copy, including their defaults and narrower capability checks.
Neither JSON nor its values are evaluated during parsing. A frozen check argv
is later executed by the trusted host only under the validated authorization.
No added parser or schema dependencies.

| Field | Input and normalization |
| --- | --- |
| `schema_version` | Required integer `1`. |
| `repo` | Required path, matched by canonical Git identity to `--repo`. Freeze the primary worktree path and Git common directory. |
| `base_ref` | Required nonblank Git ref/commit selector, at most 255 bytes, without leading option characters, control characters or shell expansion. Resolve locally to exactly one full Git commit at admission. Use the full commit for every later base comparison and worktree creation. |
| `worker_contract` | Required Workflow object. Required nonblank `objective`, `scope`, `authorization` (bounded to 8 KiB each) and `acceptance`. `authorization` is descriptive. The trusted submission separately binds verified authorization for the proposed operation. Optional `agent`, `sandbox`, `permissions`, `execution_policy`, `host_handoff`, `budget`, `stages`, `limits`, `memory` use the existing schemas. `memory` defaults to false and must be false in the isolated lane. Default Worker is the code-owned Codex adapter. Select the model in `agent.model`. |
| `reviewer` | Omitted or null disables technical review. Otherwise require `agent: {kind, model?, effort?}`. Optional `allowed_kinds` defaults to that kind alone and must include it. `session_mode` accepts `fresh_only` (default), `resume_if_supported` or `require_resume`. `max_review_rounds` defaults to 1 (1–20), `timeout_seconds` to 300 (1–3600), `checklist` and `design_docs` to empty lists (each at most 128). Use H1 agent/model/effort and text validation. Freeze each permitted design-doc path and digest from the base, with no symlink or outside reference. |
| `worktree` | Optional object with only `shared_env_links` (default empty, unique names). Worktree parent is host-selected, defaulting to the primary repo's parent. A generated branch under `ato/TASK_ID` and the generated external worktree path are reserved for that Task. Callers cannot select a branch, reuse a worktree or choose an executable via the spec. |

`acceptance` requires one or more independent `command` criteria. A
`manual` criterion is optional and describes a possible human check. Each uses the existing `id`,
`description`, `kind` and (command only) `checks` schema. `checks` is a
nonempty list of argv arrays. Each array contains nonempty strings, no NUL, at
most 8 KiB per argument. Preserve order and contents. The host admits the exact
frozen commands as part of task authorization. Generated Worker stages can
select declared check IDs but cannot invent host commands. For explicit
`stages`, use Workflow's `{id,covers,plan}` and Runner's step DAG. Stage
checks must be selected from the command argv in the covered criteria, and
at least one command criterion is covered per stage. A stage inherits Worker,
sandbox, memory, actions, grants and ceilings from the task. Reject attempts
to override them and duplicate/unknown dependencies. Only the first
implementation stage must run regardless of green baseline checks. As in T0,
if no stage was supplied, the frozen objective and independent command checks
generate one initial stage during normalization. Freeze the derived plan,
its stable ID and check selection. Once the first implementation is recorded,
the existing Workflow selects subsequent steps and applies cumulative limits.

Example input beside the repository's source files:

```json
{
  "schema_version": 1,
  "repo": ".",
  "base_ref": "HEAD",
  "worker_contract": {
    "objective": "Update the command output",
    "scope": "Local worktree edits and the listed independent checks",
    "authorization": "Approved local implementation and verification",
    "agent": {"kind": "codex"},
    "acceptance": [
      {"id": "tests", "description": "Public tests pass", "kind": "command", "checks": [["python3.12", "-m", "unittest"]]},
      {"id": "delivery", "description": "User accepts this exact output", "kind": "manual"}
    ]
  }
}
```

Freeze canonical spec JSON plus its digest before launch. Replace the base
selector with the resolved commit in the frozen object. Keep the original
input hash separately for audit. Canonical JSON throughout the unified layer
is UTF-8 using sorted keys, compact separators, `ensure_ascii=False` and
`allow_nan=False`, with no trailing newline in hashed bytes. Hash with
SHA-256, lowercase hex. Distinguish the unified `spec_sha256` from the
kernel's own semantic `contract_hash`. Bind and recheck both. Copy any
normalized/defaulted data used to create the kernel contract separately, and
preserve original spec, counts and acceptance across Runs. Never patch a
frozen contract to recover or extend it. Scope, commands and authority changes
require a separately authorized new Task. Any old/new lineage is observation
metadata only.

## Authority, credentials and roles

The owner of the private ATO state can use the sealed local CLI to submit and
manage local Tasks without a proof file. This applies to `run`, `list`,
`status`, `stop`, `resume`, `accept`, `project trace`, `project recheck`,
`project cleanup` and `agent binding`. Local control follows the Task's
current controller binding. Sessions under the same OS account share this
local controller identity. Each Task records the submitting local account,
canonical repository and frozen spec digest. A caller with an explicit
`ATO_ACTOR_FILE` uses that actor's scope.

For local submissions, the host can record the submitting Codex session
after checking its exact native index entry and session header. This private
record selects the original conversation for completion notices.
The session working directory and the submitted repository are independent.
This routing metadata grants no actor authority and changes no controller
or acceptance binding. See [host setup](ato-host-setup.md#completion-notices-for-local-tasks).

An authenticated actor is a verified local CLI or trusted adapter principal.
For scoped operations, the CLI selects a private actor file through
`ATO_ACTOR_FILE` or receives proof directly from the trusted host integration.
A proof is always validated against the identity registry and
current bindings. Process environment, working directory, a native home,
a thread/session ID, a repository match and a string citing a source convey
no authority by themselves. No ambient Codex or legacy `CTASK_*` identity
fallback applies to v1. Enrollment and grant issuance stay with the trusted
host using the existing scoped actor/adapter machinery. A scoped key cannot
mint or refresh itself. Bootstrap uses an explicitly enrolled principal
and authority, with separate user, controller and interaction roles.
Scoped task creation selects a verified initial controller and interaction
binding from the repo's trusted host configuration and records their IDs and
binding revision. There is no auto-selection by session recency.

A repo-scoped submit grant covers the allowed objective and scope, actions,
paths, capabilities and limits. The host establishes that grant from real user
authorization and binds it to the frozen digest at admission. Scoped Task-level
grants pin canonical repo, Task ID, spec digest, role and applicable binding
revision, plus Run/kernel generation for execution-scoped operations. The
Workflow contract's `authorization`, optional `permissions.source` and
`execution_policy.source` preserve attribution. The actor proof and verified
authorization reference supply authority. Canonical project comparison may
map an exact Task-owned worktree to its frozen repo identity for the unified
grant. At the kernel API boundary use the actual native worktree project,
Workflow ID, contract hash and generation. Validate the link in both directions.
A project key is never substituted for a verified native/endpoint identity.

| Principal | Authorized operations with the matching scope |
| --- | --- |
| Local state owner | Submit, observe and control local Tasks under the current controller binding. Explicitly accept a Task with the local controller and interaction binding. |
| Scoped user | Submit/observe or stop/resume when granted. Answer and accept as the authenticated `user` actor of the bound interaction endpoint, with actual decision provenance. Authorize a binding transfer or changes to eligible review configuration through the audited host. |
| Scoped controller / origin | Coordinate and observe its Tasks. Submit, stop, resume or request checks within an existing grant. Create a real user request or report a pending host handoff. Propose/record an already authorized handoff. Never infer or register a user's answer or acceptance under this role. |
| Scoped interaction endpoint | Receive/read scoped pending human items and record actual presentation. Route the separately authenticated user's action to the service. It cannot self-elect as controller, create execution rights, issue itself credentials or speak as the user. |
| Actual Run owner / host | Under the current kernel lease and exact generation, launch in-scope work, record technical evidence, consume the matched authorized answer and execute authorized host checks. Only the owning host renews, signals or releases its process/lease. This role carries no authority to accept a product. |
| Worker / planner | Perform frozen work or propose a stage/report a need within the adapter sandbox. Planner is read-only. Neither gets actor/lease tokens, writes trusted evidence, selects its own adapter executable or invokes business actions beyond the existing frozen execution policy. |
| Reviewer | Read-only OS-fenced review of a bound product, under the same execution admission. Can emit technical verdicts only. It cannot edit source, mint privileges, change scope/Worker, drive user routing or settle acceptance. |
| Trusted operator/release host | Code-owned local enrollment, policy and adapter configuration, scoped diagnostics, authorized worktree cleanup and explicitly authorized release transactions. Operational access confers no user decision authority. |

`list` and `status` expose only Tasks the actor can observe. `inbox` also
requires the exact current interaction route and item scope. Pending
requests with a displaced or stale grant are never shown to a previous
recipient. Immediate STOP is available to a current Task-scoped actor holding
stop authority independently of lease ownership. Any origin takeover, answer
consumption or review switch rechecks the grants and ownership for its own
action.

Credential files are bounded, owner-checked, regular, single-link, mode
`0600` in a private `0700` directory, outside every repo/worktree and
model-readable area. Use the existing verified scoped credential and
endpoint machinery with hashed stored secrets and no public proof fields.
Native homes and executables are resolved by code-owned host profiles, with
absolute checked paths and adapter-specific admission. Start with isolated
private native homes and a minimal allowlisted environment for Worker,
Reviewer and checks. Scrub environment proxies, loader and Python import
paths, inherited actor and session variables, provider keys and runtime
overrides. No vendor-home inheritance, raw secrets in JSON, keys in argv or
copying into the worktree. The trusted host alone may supply an explicit
verified native home or a credential-free numeric-loopback proxy according
to applicable consent and adapter policy. No remote proxy, userinfo, path
or query in that proxy location. Home/profile selection must not overlap
ATO state or any repo or grant cross-role credential access. Review or worker
qualification and real provider permission remain separate from a binary's
availability. The offline harness injects fake adapters by trusted code and
uses temporary ATO and native homes throughout. See the
[H1 offline boundaries](h1-offline-implementation.md),
[worker grants](worker-permissions-and-handoffs.md),
[action ownership](worker-action-policy.md) and
[neutral actor boundary](session-decoupling/neutral-cli.md).

## Storage and service seam

Use `ATO_HOME` when explicitly set, otherwise
`${HOME}/.local/share/ato`. The selected home is a locator only. New
authority, task records, policy and kernel-support state are rooted in
`ATO_HOME/unified-v1/`. No fallback or auto-import from
`CODEX_HOME`, legacy ctask paths or H1 hubs. Keep vendor native roots
separate. Verify private owner/modes and canonical paths before mutation.
Directories are `0700`, regular state/evidence files are `0600`. Reject
symlinks, hard links on trusted files, path escape, wrong owner, unsafe
modes and schema drift. Open private records by verified directory and
filename, and publish fsynced atomic replacements or exclusive creations
in the same filesystem. Start the trusted runtime with umask `077` for
kernel-created records as well. A private store can be absent without a read
creating it. Registry, grants, locks, outbox and kernel-support state live
under this versioned root. Bind the existing sealed core's config/storage
lookups to `unified-v1/kernel/` via a code-owned initialization in the
unprotected config seam, consistently across all v1 source processes.
Native-home selection cannot select a different mailbox, lease or policy
namespace. Reuse the existing managed mailbox database/leases in that
kernel namespace, including its WAL and existing queue barriers. T2 adds
no database, package or background indexer.

| Location relative to the versioned store | Authority |
| --- | --- |
| `tasks/TASK_ID/spec.json` | Canonical immutable normalized task specification. |
| `tasks/TASK_ID/task.json` | Schema version, Task ID, spec/input digests, creation time, repo identity, authorization reference, initial/current binding revision reference, worktree binding, last published Run ID, revision and creation facts. No writable copy of a kernel stage state. |
| `tasks/TASK_ID/runs/RUN_ID.json` | Run ID, predecessor Run, kind (`initial`, `resume`, `recheck`), ordinal, start reason, runtime seal, exact Workflow directory/ID, worktree, spec and contract digests, kernel generation at admission, launch and settled/recovery receipt references. A Run may link bounded repair Workflow records if the H1 driver creates them. Every such link pins its predecessor and prior history. |
| `tasks/TASK_ID/evidence/SHA256` | Immutable-by-reference blobs and manifests, verified by digest before use. Receipts refer to content identities, not user-supplied mutable paths. |
| `tasks/TASK_ID/STOP.json` and task lock | Versioned STOP request for the observed Run/epoch and audit revision. The existing Workflow/Runner STOP marker in the worktree is also set for the active kernel record. The internal driver guards against either fence. |
| `tasks/TASK_ID/bindings/`, `tasks/TASK_ID/interaction/` | T5 binding and T4 decision records respectively. See their ownership below. Only committed revisions project into status or inbox. |
| Bound worktree's `.codex/tasks/workflows/WORKFLOW_ID/` and child stage directories | `workflow.create(worktree, ...)` writes authoritative contracts, acceptance command results, stages, attempts, time, leases/process facts and kernel events. The unified ledger records exact back-references. It does not assume the kernel ID equals the public Task or Run ID. |

One isolated linked worktree is provisioned per Task at its frozen commit.
A Run always resolves to that worktree and its bound Workflow chain.
Persist primary repo path, canonical common directory, worktree path,
generated branch and full base commit. Verify the exact Git registry
entry, directory identity, common dir, branch and base ancestry before
admission. Never claim an existing worktree or branch by finding a matching
name alone. Use H1's external-worktree placement and shared-link checks.
The opt-in shared names are `.venv`, `venv`, `node_modules` and
`integrations/pi-acp/node_modules`. Sources must exist, be ignored and
untracked and have no symlink alias. Refuse target collisions or changes
and give the adapters no additional write grant through those links.
Reuse H1's checks that `.codex/tasks` is untracked and that neither it nor
its parent is redirected. Fence kernel records and the unified store from
Worker/Reviewer writes, with stage output admitted through the existing
host-owned protocol. Refuse an adapter profile that cannot preserve this
boundary. Reuse the reviewer integrity checks across both product and
ignored scratch/runtime entries, including link edges.
[H1 worktree implementation](../hub_worktree.py)

Reserve IDs with exclusive filesystem creation. Stage the Task in
`preparing` with a private creation intent before any Git or kernel
provisioning. Commit links only after validating both sides. Persist the
expected repo, branch, paths and digests before issuing each side effect.
On interruption, identify the phase from durable intent and owned evidence.
Finish only an exact matching publication or record that reconciliation is
needed. Never duplicate a Workflow, delete an ambiguous worktree/branch,
or turn a half-written reservation into a new task. A second independent
`run` creates a new Task even if the normalized input is identical. A
concurrent retry of the same internal intent reconciles its reserved ID.
A Run's identity, prior links and published receipts never change. Advance
mutable projections by shifting references to new records after writing and
verifying them. A per-Task short mutation lock and expected revision fence
serialize publication. Preserve old receipts after interruption or conflict.

A Run progresses from `reserved` to `launching` to `active` on a verified
startup receipt, then `settled` with its actual execution outcome. A proved
pre-effect failure is `launch_failed`. If admission or a terminal outcome
cannot be verified, record `unknown` and retain the Run. Only exact
same-Run reconciliation under the current fence can settle it. Late receipts
from superseded generations remain evidence and cannot change the active Run.

The canonical directory scan is sufficient for v1 list, and a verified
explicit request index may map a Request ID to its Task without becoming
authority. Revalidate indexed pointers from the canonical records before
exposing any result. A rebuilt index only reads committed facts and
replaces an index cache. It cannot create Runs/locks or backfill authority,
change visibility, move a Task or repair its state. Queries use existing
state snapshots and a bounded revision retry or a typed corruption/busy
result. Do not open SQLite for a write-capable initialization on read paths,
or call legacy helpers that create lock files. An empty store returns empty
lists. List omits unverifiable entries and uses code-only warnings for
visible corruption. Direct reads of a visible damaged Task return the
applicable error after checking visibility.

### Logical service APIs

Modules may use internal typed records or mappings with the following
semantics. Pass an authenticated actor context and the configured store to
public-facing services. Runtime callbacks are trusted constructor inputs.

| Stage and API | Boundary |
| --- | --- |
| T2 `normalize_spec(repo, spec_file)`, `create_task(spec, authority, binding)` | Offline structural normalization, followed by host authority check and durable reservation. Returns Task ID and frozen digest. Creation is read-only until explicit durable reservation. |
| T2 `load_task(task_id)`, `load_run(task_id, run_id)`, `bind_worktree(task_id, binding, expected_revision)`, `append_run(task_id, run, expected_revision)`, `record_run_receipt(task_id, run_id, receipt, expected_revision)` | Internal typed record reads/writes with hash/back-reference checks. The two writes for run intent and receipt are monotonic. Unknown launch cannot be represented as a new successful Run. |
| T2/T3 `list_tasks(actor, repo=None)`, `task_status(actor, task_id)` | One redacted projection, taking versioned binding and decision snapshots from T4/T5 when composed. No execution state writes. |
| T3 `start(actor, task_id, foreground=False)`, `request_stop(actor, task_id)`, `resume(actor, task_id)`, `recheck(actor, task_id, checks_only=False)` | Admission to one driver with persistent launch/STOP/lease receipts. Initial and recovery launch share the Run admission path, with distinct reason/kind. Recheck receives a Run ID and the existing Workflow under the same constraints. |
| T4 `inbox(actor)`, `record_answer(actor, request_id, text, source_event)`, `accept_product(actor, task_id, decision)` | Human-only decisions are stored once with authenticated provenance. Supplies `pending(task_snapshot, binding_snapshot)`, `acceptance(task_snapshot, product)` and execution decision lookup/consumption to the composed services. |
| T5 `binding(actor, task_id)`, `handoff(actor, task_id, target, interaction, decision)`, `select_reviewer(actor, task_id, reviewer, decision)` | Synchronous compare-and-swap of authority-checked binding revisions and read-only view of the actual Run owner. Publishes verified routing and review bindings for T3 and T4. |

The exact shared read boundary for T4 and T5 is an immutable Task snapshot
`{task_id, repo_key, spec_sha256, revision, stop_epoch, run_id, workflow_id,
contract_hash, generation, execution, product}` and a binding snapshot
`{task_id, revision, controller_ref, interaction_ref, reviewer,
review_revision}`. Generation is the kernel's existing full start/revision
fence, never just a timestamp. Mutation requests specify the expected Task
and binding revisions from those snapshots. Readers either see the previous
committed binding or the new one. Neither parallel leaf imports the other.
T3 provides transaction/read adapters and the product/check projection. T6
composes the two leaves and their status contributions. Per-task locks are
short-lived. No code holding the task mutation lock waits on a Git, execution
or mailbox operation. Revalidate expected revisions after releasing any
lock needed for slow evidence or launch preparation.

### Task status and execution states

`TaskSummary` has `task_id`, `repo`, `status`, `reason` and
`updated_at`. `TaskStatus` adds `spec_sha256`, `execution`, `run_history`,
`stage`, `checks`, `review`, `product`, `acceptance`, `pending`, `delivery`,
`worktree_state` and `next_step`. Unknown optional scalar fields are null.
Empty collections are arrays. `reason` and `next_step` are code-owned, and
the latter identifies the action or current wait. `execution` contains
`state`, `run_id`, `pid` when verified, `owner_role`, `verified`,
`stop_requested` and a redacted lease disposition. `state` is one of `idle`, `launching`, `active`,
`stopping`, `cleaning`, `unknown`. Run history lists Run IDs, kinds,
verified outcomes and times, without raw local file or native identity
details. `stage` shows criterion/step IDs, technical state and accumulated
attempts and elapsed time from Workflow. `checks` lists criterion IDs,
states and receipt IDs. `review` gives the technical verdict and its
receipt. `product` gives `product_id`, base, branch, authorized worktree
location and `checks_sha256`. `acceptance` gives disposition
(`pending`, `recorded`, `invalidated`), bound criteria and receipt ID.
`pending` contains authorized request summaries. `delivery` uses canonical
mailbox dispositions (including `recorded_only` when no transport is
configured and `delivery_unknown` when admission is unconfirmed).
`InboxItem` contains request ID, Task ID, kind, a bounded question or human
action, relevant product ID, creation time and delivery disposition, without
private prompts or transport transcripts.

Overall status is derived from verified facts. In this table earlier safety
conditions take priority over later progress signals. An ordinary unknown
notification stays in `delivery` after valid acceptance and cannot undo it.
Unresolved execution or decision delivery that could affect a new Run fences
that Run.

| Observable status | Required facts or transition |
| --- | --- |
| `cleanup_blocked` | Group, owner, native activity or cleanup identity cannot be reconciled safely. Keep STOP and retained references when present. Return no claim that execution is gone. |
| `delivery_unknown` | A material Run/recovery/answer delivery lacks a trustworthy outcome, independently of whether a local process is active. Retain its receipt, block replay/admission and require exact correlation or authoritative reconciliation. |
| `stopping` → `stopped` | STOP recorded. Transition only after the owner, workers, reviewer, checks and relevant native children have verified cleanup or absence. A blocked cleanup returns `cleanup_blocked` instead. |
| `preparing` | A creation intent exists, possibly with partial links. A verified full creation may proceed to launch. Interrupted ambiguous provisioning stays fenced. |
| `launching` → `running` | Durable launch intent, then exact live owner and startup receipt. A proven pre-effect failure becomes `blocked` with `LAUNCH_FAILED`. An unverified outcome remains fenced. |
| `checking` → `reviewing` | Independent checks hold execution ownership. Required review starts only once the checks pass for the same product. Blockers can use bounded repair under the frozen checks and cumulative history. |
| `waiting` / `needs_input` / `needs_host` | Bounded external wait, an actual pending human request, or an authorized frozen host handoff respectively. Display current Run activity separately. A response, process exit or handoff read does not mean execution resumed. |
| `blocked` | Verified failure, policy/limit barrier, unavailable required checks/review, or invalidated product/evidence. Keep existing counts and report specific reason. No automatic resume from an unknown state or a Reviewer mutation. |
| `completed` | Execution and cleanup settled, current product and independent checks verified, and any required technical review passed. Earlier successful Run receipts labelled `needs_acceptance` use the same projection. An optional human receipt is tracked separately. |

Explicit worktree cleanup is a lifecycle field
`worktree_state: "present" | "removed"` in status, separate from acceptance
and STOP. Intentional completed cleanup keeps the verified sealed product
as the retained current delivery. A workspace disappearing outside that
record is unavailable. The engine's `completed`, stage worker
`ready_for_checks`, reviewer verdict, mailbox/adapter turn completion and
controller delivery are facts in separate fields. None implicitly creates
a user receipt.

## Execution, STOP and recovery

T3 uses the H1 driver techniques around the original Workflow and Runner. The
same driver admits initial work, recovery, authorized host checks, review and
bounded repair. Background and foreground differ only in whether the caller
waits after admission. Publish an intent and verify an exact startup receipt
containing Task, Run, source seal, PID with birth identity and generation
fence. If the child already settled, use its verified terminal receipt for
that same Run. Acquire the canonical repo gate for orchestration across linked
worktrees, then the existing worktree flock and durable kernel execution
lease as appropriate. The outer repo gate is serialization, never a second
lease. Nested stage workers use their parent's ownership and guard. When
a lease is released between kernel steps, keep orchestration serialized and
recheck ownership and activity before the next step. An active or unverified
native owner bars new execution even when a flock is free.
[Baseline execution fixes](ato-unified-baseline.md) ·
[Workflow](../workflow.py) · [H1 execution composition](../hub_loop.py)

Reuse the first-stage preseed discipline, then preserve the existing
technical acceptance/plan flow. Preflight adapter capability, path and
authorization limits before starting an attempt. Independent host checks
and review are admitted under the same repository gate and lease. Original
check argv, caps and execution policy stay frozen. Review follows successful
checks only. Reuse the H1 read-only per-adapter profile, bounded structured
verdict, before/after integrity snapshot and repeat-blocker fingerprint. A
valid `request_changes` with actionable blockers can trigger only bounded
repair and rechecking within the same Task. Repeated same-product blockers,
round exhaustion and reviewer mutation are hard barriers. Use the current
verified Reviewer binding when the next review starts, then fence it for that
turn. A captured verdict may be used only with the existing required product
and review revision. With unknown resume support, `resume_if_supported` starts fresh and
`require_resume` returns `UNSUPPORTED_CAPABILITY`. Resolve native session IDs
only through their recorded adapter, role and workspace.

Rechecks, repairs, wait handling and cleanup preserve cumulative time,
attempts, stages, planning limits, supervision counts and history. In the
new namespace elapsed time is advisory by default. Pin the verified time
policy revision at Task admission. The trusted host can select an enforced
policy with explicit authorization before admission. Refuse policy drift
that would change the authority or limits of an existing Task. Retain the
existing per-attempt/check timeouts, bounded waits, resource limits and any explicit
per-action grants in either mode. A prior Run or Workflow receipt records
spent attempts/time before another is linked. A repair Workflow must inherit
remaining authority and limits and link the previous receipts. A new
directory or Run carries the same history. Adopt the current no-progress and
repeated-failure protections.
[Adaptive budgets](adaptive-budgets.md) · [Read-only review boundaries](h1-offline-implementation.md)

STOP is a short Task-level mutation. An already completed Task returns its
existing status. On other Tasks with an admissible stop transition, it
publishes the fence immediately even while the worker holds execution locks,
then mirrors it into the bound Workflow STOP path. It never waits on a busy controller or Git operation.
The driver and startup checks observe its epoch, the outer guard connects it
to Workflow/Runner, and review/check loops poll it. Missing the kernel
mirror after a crash leaves the outer STOP effective. A driver signals only
its verified owned process groups, checking birth/group identity before
TERM, bounded wait, escalation to KILL and reap. Never signal a reused PID,
ambiguous group or a process from a previous owner. Cleanup ambiguity leaves
`cleanup_blocked` and durable identity receipts. A launch racing STOP
either proves it never admitted work or observes STOP and performs owned
cleanup before reporting idle.

Resume is explicit and task-scoped. Under the admission fences, revalidate
the frozen spec/authorization, repo and worktree binding, runtime, prior
Run/Workflow generation, process identities, lease, STOP epoch, remaining
limits and latest applicable actor decisions. For an expired lease, verify
that exact old Workflow and native session are idle and all process groups
absent. Expiry, PID reuse, EOF and missing observations are insufficient.
Use correlated receipts to settle known outcomes. Unknown attempted work,
unresolved check/Reviewer mutation, material unknown delivery, an unverified
native owner, missing worktree, an unanswered blocking request or exhausted
limits refuses admission. Successful settlement is terminal for Worker resume.
Revalidating an invalidated
product uses explicit `project recheck` first. A verified reversible failure
or completed STOP cleanup can admit a new Run only after the underlying
barrier is resolved. Retain the same Task ID, prior Run and cumulative
history. Reserve the next Run and retire only the expected old STOP epoch
inside a fenced transition. If publishing that transition is interrupted,
the STOP stays effective until exact reconciliation. A later STOP always
wins and no code unconditionally unlinks STOP, adopts an old PID or retries
unknown work. Bound host-only actions require a frozen `host_handoff` and
authentic provenance, and return to the same independent checks after work.
[Recovery evidence](recovery-escalation.md) ·
[Execution authority and host handoffs](worker-permissions-and-handoffs.md)

### Private sealed invocation

A source-selected Python entry script `ato_runtime.py` is private to the
selected snapshot. Invoke it with an argv list of the absolute pinned Python,
absolute script in that same snapshot, code-owned internal verb and typed
IDs. The private verbs are `run-child TASK_ID RUN_ID`, `observe`,
`supervise TASK_ID RUN_ID`, `resume-run TASK_ID` and
`read-handoff HANDOFF_ID`. The handoff locator is `handoff-` plus 32 lowercase
hexadecimal digits mapped to the canonical action. A private operation requires
an admitted intent and verified parent/host context. Child startup receives
a one-use capability over an inherited private pipe and checks the intent,
lease/generation as applicable,
snapshot seal, state root and STOP before side effects. Reader calls instead
reverify the current scoped actor, recipient, handoff generation, retained
payload hash, STOP and completion. An expired or invalid compact wake-up
returns a short code-owned silent disposition and never acts on stale
material. The handoff ID is only a locator. Run, observer and supervisor
claims are fenced by their own exact verified process/runtime identity.
Shell rendering for a private wake-up quotes the fixed interpreter/script
and the validated ID. Runtime selection never trusts PATH, repo code,
a conversation's text, model-supplied import names, launcher overrides or
an ambient actor. Do not put capabilities, challenges, native tokens or
decision text in child argv.

The public parser cannot dispatch runtime verbs. Use the code-owned
`agent_registry.set_runtime_entrypoint` / `runtime_command` and unprotected
`ato_config`/reader seams to bind remaining cross-process callers. Translate
a protected caller's allowed legacy internal argv before dispatch using its
exact validated Task/Run back-reference. Do not install or expose a second
parser accepting legacy commands. Protected legacy paths and unsafe native
delegation remain historical. Retain intentional regression coverage
through direct private source fixtures. See the caller map below.
[Existing runtime registry](../agent_registry.py) ·
[Compact host handoff](compact-origin-handoff.md) ·
[Compact failure wake-up](compact-failure-notices.md)

## Product, checks and human decisions

### Immutable product identity

The host takes a product snapshot of the bound worktree under execution
admission after work has stopped mutating it. Make a canonical manifest
containing Task ID, spec digest, frozen base commit, current HEAD commit
and tree, index digest, and sorted repository-relative entries for tracked
and non-ignored untracked product paths. Each entry records kind and mode
and the SHA-256 of bytes for regular files, exact link text for symlinks
without traversal, or the explicit missing marker for a tracked deletion.
An unsupported file type, unrepresentable path or unresolved submodule
fails closed. Use Git paths with NUL delimiters, a canonical UTF-8 encoding,
and bounded traversal. Exclude the ATO execution/evidence namespace and
verified shared dependency links themselves from the product digest. A
tracked source directory named like an eligible dependency remains product.
Record ignored runtime/scratch files and link integrity separately for the
reviewer tamper check. This detects index, mode, content and uncommitted
work changes that a HEAD-only hash misses. `product_id` is
`product-` followed by SHA-256 of the canonical manifest. Retain that
manifest and bound evidence outside the worktree.

The trusted host measures the same product before and after independent
checks and review, and at acceptance. A frozen check can produce ignored
scratch output. A changed product during checks or review cannot receive
a passed check set. Each command check receipt pins Task, spec, Run, exact
Workflow generation, criterion and argv digest, product ID, verified
outcome, and content digests of retained logs. Only the owning independent
host creates technical receipts. Capture authoritative evidence before
allowing further worktree mutations. The current `checks_sha256` hashes
the canonical ordered receipt references for **all** original command
criteria. For required review, retain its structured verdict, product
and review revision, Run provenance and the check-set digest reviewed.
If a recheck produces a different check-set digest, establish a matching
required review before accepting. No Worker-written status, stdout
claim, arbitrary path or manual off-record waiver substitutes for a passed
check set. Capture canonical bytes once, publish as private immutable
content-addressed blobs, and verify existence and digest every time a
current receipt is consumed.

A general edit, worktree substitution, changed index, missing evidence,
or new check set invalidates a previous acceptance for the current
product. A valid intentional cleanup preserves the sealed product as the
current delivery. Queries diagnose a changed or unavailable current
product without writing a replacement or repairing it. Independent
recheck and a new matching user decision are required for the new
version. Prior receipts remain auditable.

### Requests, answers and acceptance

T4 reuses the existing scoped actor checks, typed interaction semantics,
managed mailbox/deduplication and quiet policy. A canonical request
contains its ID, Task/repo and spec identity, kind, trusted creating
actor/reason, creation time, current recipient/binding revision and the
exact relevant Run/full generation or product/check-set version. Typically
a work decision is Run scoped and final acceptance is product scoped.
Record expiry when the creating workflow defines one and otherwise fence
by applicable Run, binding and STOP/closure. A request for only a factual
answer cannot become a permission or budget grant. Grant decisions
require the distinct actual authorizer, the exact scope/purpose and
all existing grant checks. New authority beyond the frozen task requires
a new authorized Task. Bind an executed decision to its request and the
one intended continuation. A resumed Run cannot reuse a previously
consumed authorization in a later generation.

Use current actor verification and the bound user principal for answers.
On the CLI that proof accompanies the actual human's input, with provenance
recorded from the trusted invocation context. With a conversation adapter
it additionally binds the actual user turn/event, not assistant output or
a generic claimed source. Commit a single decision to the request before
any external effect. Identical decision by the same current authorized user
returns the receipt. A conflicting answer returns `DECISION_CONFLICT`.
Stale run/spec/route/endpoint/STOP checks return `STALE_REQUEST`. An
answer is separate from origin consumption and from starting or stopping
work. The execution host records consumption under the existing generation
fence and checks the specific answer and its canonical delivery before
advancing a Run. A pending answer whose relevant effect is unknown remains
a recovery barrier.

The local state owner records explicit acceptance with `ato accept TASK_ID`.
An optional `--evidence FILE` retains the supplied bytes as a private
attachment. The receipt records `acceptance.method=local_owner` and applies
to the Task, including one pinned to an earlier runtime. Repeated acceptance
returns the original receipt. Checks, review, Run history, execution and
completion continue to report their measured results. Existing owner receipts
that explicitly closed historical Tasks retain their effect. Without a human
receipt, `acceptance.disposition` is `not_recorded`.

Scoped acceptance requires `--evidence FILE` with a strict JSON object
with exactly the fields below. `criteria` contains all frozen manual criterion IDs, once each,
or an empty list for a command-only specification.
`source` identifies the actual user instruction/event and `note` is
their decision text. Both are nonblank and bounded to 8 KiB. When a trusted
event record exists, verify that evidence against it. The user actor must
authorize the decision independently of the strings in the file.

```json
{
  "schema_version": 1,
  "task_id": "task-11111111111111111111111111111111",
  "spec_sha256": "<64 lowercase hex digits>",
  "product_id": "product-<64 lowercase hex digits>",
  "checks_sha256": "<64 lowercase hex digits>",
  "criteria": ["delivery"],
  "decision": "accept",
  "source": "Actual user decision provenance",
  "note": "I accept this product version"
}
```

Require current scope, settled execution, matched product, passed complete independent
checks and any required matching review. Match and pin the review receipt
when needed. Record source bytes' digest and
the verified, non-secret actor audit reference. Normalize criterion order
for comparison. The decision key is Task, spec, product, check set, the
normalized decision fields and the verified user principal. Repeating the
same key after revalidation returns the original receipt. On that product
and check set a different decision conflicts. Commit with a compare-and-swap
after recomputing product/evidence under the repo admission gate and
rechecking STOP, binding and Run revisions. The product pointer projects
that optional receipt.

Inbox queries do not update presentation. A scoped interaction host can
record that the item was actually displayed, once, separately from user
receipt or approval. Track notification and decision delivery via the
one canonical mailbox with stable request or Run settlement correlation IDs,
exact endpoints and generation checks. Commit a dispatch reservation before
adapter entry and retain `delivery_unknown` for ambiguity. An identical
record/lookup, restart, new recipient, receipt read or new Run does not
create another send. Correlated reconciliation may settle the old receipt.
The default is `recorded_only` without configured eligible transport.
Use existing project mutes, quiet progress, real user-input notices and
once-only total completion policy. Final completion notices use the first
successful verified Run settlement for the Task. Historical tasks keep
their delivery records without replay. Keep unknown notices and
all actual delivery evidence. See the [message protocol](message-protocol.md),
[notification policy](notification-policy.md) and
[completion policy](completion-notifications.md).

## Controller binding and handoff

The Task binding has a controller conversation, a user interaction endpoint,
and an active Reviewer selection with its own revision. The immutable
origin and the actual current Run owner are execution facts from the kernel.
The controller conversation may change while the old Run continues. A
Reviewer is a separately scoped tool for technical review, even if it uses
the same vendor as one of the conversations. Matching vendors, local paths
or repo identity do not establish actor equivalence.

A handoff evidence file uses the same strict JSON loader. It contains
exactly `schema_version`, `task_id`, `spec_sha256`, `binding_revision`,
`from`, `to`, `interaction` and `source`. `from` is the current
controller reference, `to` is the target reference, and `interaction`
is the exact resulting interaction reference (the existing one if the CLI
option is omitted). The file and command must agree. Use only opaque
verified reference tokens. Check the genuine authorizing user event and
source, current actor and role grants, both old and new endpoints,
destination eligibility, canonical repository, spec digest and binding
revision. The destination's authority is verified by its enrolled
credentials and scoped grants, never by a supplied conversation string.
A controller who records an authorized change cannot bypass the
authorizing user's decision. The synchronous commit uses expected Task,
STOP and binding revisions. Conflicting handoffs, unrelated sessions even
in the same project, and handoff/STOP races fail or serialize against the
winning transition. No old grant is automatically valid for the new
binding. A role that kept its endpoint must still satisfy the current
binding and generation checks.

Retain immutable before/after bindings and a scrubbed handoff receipt with
current stage, used attempts and elapsed history, unresolved request IDs,
current product/check evidence and actual Run owner. The new controller sees
that owner and its state, including a live old owner. A successful binding
transfer creates no launch, does not cancel a Run, mutate lease credentials,
replace the kernel's origin record, clear STOP or change the Worker.
Execution transfer uses an explicit, separately scoped resume after exact
release or verified exit, including native cleanup. The new Run acquires
the existing execution fence according to the same admission rules.
Fail closed if the original owner is still active or its outcome is unknown.

Use the updated binding snapshot to present a still-current pending item to
a verified new interaction endpoint. Keep its canonical request ID and
decision history. Supersede only a provably unsubmitted old route under the
existing mailbox barrier. For an already claimed/unknown route, retain the
canonical receipt. Neither the switch nor later inbox reads replace it
with an automatic resend. Prefer pull visibility in the new endpoint for
current, undelivered items. Reject answers made with old route grants and
remove visibility immediately after the binding commit. A handoff cannot
resurrect a stale Run or product-scoped request. Late events are evidence
only until their exact valid correlation is proved.

The Reviewer evidence file has exactly `schema_version`, `task_id`,
`spec_sha256`, `binding_revision`, `reviewer` and `source`. The chosen
kind/model/effort matches the CLI and frozen allowed kinds, existing scope
and adapter capability. Commit only when no review turn is active or
ambiguous and the Task can still qualify a product. Increment only the
binding/review revisions, preserve the Worker and user route, and park the
old Reviewer session metadata. Apply the new selection to future review
only. Old technical evidence does not qualify the new selection. Rebuild
bounded, redacted context from the verified handoff instead of passing a
foreign native session ID. H1 session rollover and attach mechanics can
remain trusted adapter features, with qualification and idle checks.
[H1 design](hub-orchestrator-design.md) ·
[Neutral session boundaries](session-decoupling/operator-handoff.md)

### Independent implementation ownership

| Stage | Exclusive source and test ownership | Shared integration required |
| --- | --- | --- |
| T2 | `ato_task_store.py` and `tests/test_ato_unified_task_store.py` | Typed normalization, IDs, Task/Run persistence, immutable blob helpers, compare-and-swap and query snapshot APIs. T2 leaves execution and actor-specific policy to consumers. |
| T3 | `ato_execution.py`, private execution bootstrap and `tests/test_ato_unified_execution.py` | Driver and launch/STOP/recovery integration, independent check and product projection, trusted host callbacks and the shared snapshot/transaction adapters. |
| T4 | `ato_acceptance.py` and `tests/test_ato_unified_acceptance.py` | Owns requests, answers, presentation, product acceptance, their receipts and notification eligibility under `interaction/`. Uses read-only binding values injected by the composition root, plus T2/T3 transaction, actor and evidence helpers. No writes to bindings. |
| T5 | `ato_binding.py` and `tests/test_ato_unified_handoff.py` | Owns controller/interaction/Reviewer binding revisions, verified handoff evidence and binding projection under `bindings/`. Receives current unresolved request/evidence references through a callback. Does not edit questions, answers, acceptance or delivery receipts. |
| T6 | Public parser/dispatch, sealed runtime wiring, install/release and single Skill integration plus `tests/test_ato_unified_cli.py` | Compose T4/T5 through the T3 seam, install the routing and status callbacks, and compose route supersession/reconciliation via the shared mailbox. Integrate user CLI, private notification rendering and worker bindings into a sealed source. |
| T7 | `tests/test_ato_unification_e2e.py` and final qualification evidence | Exercise all composed paths across subprocess/API boundaries using temporary offline fixtures. |

T4 and T5 share service contracts only. They do not edit the other leaf,
shared CLI, config, packaging, docs index, or the protected modules. Each
provides its own test setup through constructor interfaces, with no shared
fixture edits. Their stage handoffs name the final integration points for T6.
The composition root obtains the current verified route before decision
admission, before actual presentation and at each dispatch barrier. It
rechecks epochs and authority before publishing or claiming an action. T5
can snapshot request IDs via callback, while T4 performs any resulting
interaction reconciliation using the new binding. Neither path adds an
independent queue or writer lease.

## Legacy capability and caller migration

The following inventory uses the exact frozen
[55 parser names and aliases](../tests/fixtures/cli-surface.json)
and their [historical handler fixtures](../tests/fixtures/legacy_cli/__init__.py), plus
[the H1 parser](../hub_cli.py). A public replacement uses the new Task ID.
An internal disposition describes the remaining trusted caller and seam.
A retired operation remains usable only by its original sealed version
against its own prior namespace, where that version still supports it.
No path-based argument, old command or alias is accepted by the new public
parser. This is a source and caller migration. There is no data conversion.

For a new submission from a legacy standalone plan, put the step DAG in
`worker_contract.stages[].plan` and declare the independent command criteria
in `worker_contract.acceptance`. H1 callers map
`review_policy` into `reviewer`, using null when disabled, and
`worktree_policy.shared_env_links` into `worktree.shared_env_links`. The
trusted host selects any nondefault worktree parent. Keep the original
scope, review bounds, base and checks. Generate new Task and Run IDs only
on authorized submission in the new namespace.

### ctask inventory

| Legacy name | Disposition and migration |
| --- | --- |
| `_controller` | Internal. Observer boot moves from `control.start` and `ctask-notify` to the snapshot-bound private `observe` operation. Operator inspection uses `ato system status`. |
| `_dispatcher` | Retired for new tasks. `delegation.delegate/watch` and the delegation child chain remain confined to their historical source/namespace. New execution enters the Worker's typed Run driver. |
| `_supervisor` | Internal. Preserve only explicitly authorized, bounded observation/recovery through a sealed supervisor caller. `recovery.start`'s internal launch is translated to the bound Run. Queries use `ato status`. |
| `_worker` | Internal. Legacy `run_cmd` spawn becomes the sealed `run-child` operation with intent, ownership and receipt checks. |
| `accept-answer` | Internal. Origin/Run host consumes the exact current answer through the T4 decision seam. The answer wake-up carries a compact private read reference. It no longer suggests a path-based shell command. |
| `acknowledge` | Replaced by `ato accept` for the complete manual delivery on an immutable product/check set. Keep existing per-criterion historical acknowledgements in old records. |
| `agent-doctor` | Replaced by `ato agent doctor`. Probe code-owned adapters for versions/capabilities and redact. Do not turn diagnostics into login, turn submission or provider validation. |
| `answer` | Retained as `ato answer REQUEST_ID --answer TEXT` with actual user proof/provenance and a versioned request. Old `--source` and `--actor-file` callers use the trusted actor/decision context. |
| `ask` | Replaced by `ato agent ask TASK_ID`. A scoped controller creates one verified request with canonical correlation. The user sees it through `ato inbox` or optional authorized notification. |
| `budget-charge` | Internal. Trusted host records verified prior time/evidence on the existing accounting ledger using a stable charge identity. Status displays cumulative time. No manual counter rewrite or transfer of old authority. |
| `budget-configure` | Internal. Freeze the initially authorized budget at submission and validate any narrower time policy in the trusted host. The legacy public retrofit of an existing independently created Workflow stays historical. |
| `budget-estimate` | Internal. Estimation helpers inform the host preparing the original specification. There is no independent calculator subcommand or new budget grant. |
| `budget-policy` | Internal. The trusted operator pins the configured advisory/enforced policy before admission, with actual authority. Inspect policy and effective accounting in `ato system status` and `ato status`. Legacy `status/set` flags do not apply. |
| `budget-reforecast` | Internal. Trusted host may use existing evidence and authorized remaining budget to reforecast within the original envelope, without minting time or retry authority. No public reforecast verb. |
| `budget-run` | Internal, narrowed. Frozen host checks and explicitly approved host actions use the existing metered lease path. No arbitrary argv forwarded from the public interface. |
| `continue-task` | Replaced by `ato resume`. The same Task uses its authenticated stored decision and safe recovery checks. The caller cannot force a specific old answer or continue without admission. |
| `control` | Split. `status` becomes `ato system status`. The release journal controls trusted `start/stop` functions, and the owned observer runs `tick` privately. `ctask-notify` callers use the new status/notification projections or private observer, depending on function. |
| `create` | Replaced by `ato run --repo PATH --spec FILE`. A one-stage plan becomes a frozen Workflow with independent final command checks. No standalone stage-directory creation API. |
| `delegate` | Retired. Arbitrary business/session delegation and native conversation creation sit outside v1. Submit a bounded local Task or hand off an existing one to a verified already enrolled session. No implicit scheduler or new conversation. |
| `delegation-accept` | Retired. Old delegated conversation prompts and requests remain historical. Use authenticated `ato agent handoff` for controller transfer of an existing Task. |
| `delegation-status` | Retired dedicated ledger/status. Observe a new Task with `ato status` or `ato agent binding`. No reassignment of old delegation IDs. |
| `failure-alerts` | Internal. Preserve opt-in once-only unattended-failure routing and project mute checks, using trusted policy and one current verified action. Public inspection is `ato system notification-policy` and `ato status`. |
| `followup-link` | Internal metadata only. Observe retained attempts and conversation/Run associations on the same Task. A separately authorized successor can carry verified observation linkage. Never link by directory guesses or turn a link into acceptance or a fresh budget. |
| `followups` | Split. Use `ato inbox` for user-actionable items. Routine operational and historic evidence uses `ato status` / `ato project trace`. Migrate `ctask-notify` and controller Skill callers accordingly. |
| `host-checks` | Replaced by `ato project recheck TASK_ID --checks-only` where the trusted host has recorded the frozen handoff's actual authorized action. It runs the original checks only under the same accounting/lease rules. |
| `host-recheck` | Replaced by `ato project recheck TASK_ID --checks-only`. Targeted check routines can still report diagnostic evidence internally, and product qualification needs the entire independent command set. |
| `hub` | Replaced by `ato agent binding` and verified `ato agent handoff` for a Task. Initial endpoint configuration is trusted host enrollment. No machine-global implicit thread switch or raw UUID grant. |
| `inbox` | Retained as `ato inbox` scoped to the verified actor, current binding and item generation with a read-only projection. |
| `list` | Retained as `ato list [--repo PATH]`. Callers consume Task IDs and normalized repository results, never scan legacy state directories. |
| `local-session-create` | Internal. Trusted setup enrolls passive CLI endpoints and separately provisions actor/user proofs using existing session code. Never expose issuance as a Worker/model tool. |
| `local-session-grant` | Internal. Trusted root issuer gives a verified role an exact Task, spec and binding/generation-scoped proof outside the worktree. No public output of bearer material. |
| `notification-policy` | Inspection becomes `ato system notification-policy`. Authorized `set/mute/unmute` are trusted host settings in the v1 namespace. Migrate `ctask-notify` and Skills without moving old policy/state. |
| `operational-disposition` | Internal. A verified origin/controller attaches bounded diagnosed user-need, provenance and evidence to a canonical request. Read the real decision in `ato inbox`. No public manual override of recovery. |
| `permission-check` | Internal capability probe before launch. `ato project validate` handles structural validation and `ato agent doctor` reports supported capabilities. A named sandbox probe remains host evidence, never a real-run or authorization claim. |
| `presented` | Internal. Interaction host records actual display once with current item/binding and real event. `ato inbox` and `ato status` do not mark presentation. Notification and Skill callers use the presentation callback. |
| `read` | Retired alias of `read-question`. Use scoped `ato inbox` for pending items or the private host reader for addressed material. |
| `read-question` | Split. User sees authorized pending material via `ato inbox`. Private exact-request readers can verify material without updating it. Display uses a separate presentation record. |
| `respond` | Retired alias of `answer`. Use `ato answer` with the verified user decision. |
| `resume` | Retained as `ato resume TASK_ID`. Fenced admission replaces unconditional legacy STOP unlink and request-specific flags. It can start an untouched initial reservation or admit an eligible successor. The private legacy recovery subprocess must pass the same admission checks. |
| `run` | Retained as task submission and start with `--repo`, `--spec`, optional `--foreground`. Previously created Task IDs continue via `ato resume`. Directory inputs are invalid. |
| `session-register` | Internal. Code-owned trusted registry verifies kind, endpoint identity, exact project and capabilities. Status/binding report redacted results. No public session proof assertion. |
| `start` | Replaced by background default `ato run` on submission and `ato resume` for a current Task. Legacy delegation's helper spawn remains historical. |
| `status` | Retained as `ato status TASK_ID` with overall/technical/actual execution, current product, evidence, decision and next-step fields. |
| `stop` | Retained as `ato stop TASK_ID`. Preserve STOP fencing and explicit verified cleanup progress. |
| `supervise` | Internal. Auto-recovery policy and limits require explicit trusted authorization and a verified origin. Launch the sealed supervisor through the current Run scope. It observes or routes an eligible request. An uncertain outcome fences replay or a new Run. |
| `takeover` | Internal. A scoped origin reports progress, actual repair, blocking state or user need via recovery/interaction callbacks. Binding handoff and execution lease transfer remain separate. Retain receipt/expiry/history checks. |
| `takeover-status` | Replaced by the read-only recovery summary in `ato status` and `ato project trace`. The host repeats current eligibility checks at mutation. |
| `trace` | Replaced by `ato project trace TASK_ID` with scoped, bounded and redacted summaries. Eliminate directory/task/JSON-format options. |
| `unsupervise` | Internal. An authorized host disables recovery policy and invalidates claims. Already attempted or unknown deliveries remain recorded and stale actions are rejected. |
| `validate` | Replaced by `ato project validate --repo PATH --spec FILE`. A standalone input maps to an explicit stage in the new versioned spec. |
| `watch` | Internal. Register observation at successful new Task admission with verified binding, or record registration failure. No new shell command to adopt a legacy path. |
| `workflow-close` | History stays in the Workflow ledger. Use `ato accept` to record a local owner's explicit acceptance or a scoped acceptance of a checked product. |
| `workflow-create` | Replaced by `ato run`. The host calls `workflow.create` under the generated worktree and binds its private ID. Origin and recovery configuration come from verified host setup, not public flags. |
| `workflow-link` | Internal observation metadata. A same-Task Run link is automatic. For a separately authorized successor, preserve same-repo, pinned spec/evidence, complete acceptance mapping and no authority expansion. Never report successor completion as v1 acceptance of the old product. |
| `workflow-validate` | Replaced by `ato project validate`. Reuse Workflow validation after strict versioned input normalization. |

### H1 inventory

| Legacy name | Disposition and migration |
| --- | --- |
| `doctor` | Replaced by `ato agent doctor`. Report Reviewer version, read-only capability and actual qualification independently from Worker installation. |
| `hub-list` | Replaced by `ato list [--repo PATH]` plus task-scoped `ato agent binding`. There is no separately managed Hub ID to find or reuse. |
| `hub-create` | Retired dedicated ledger creation. Verified controller/interaction setup and the initial Reviewer are bound at task submission. Reviewer limits and optional adapter session management replace the old shared Hub turn control. |
| `hub-status` | Replaced by `ato agent binding TASK_ID` and `ato status TASK_ID`. Actual Run and review activity are independent of conversation binding. |
| `hub-attach-cmd` | Internal. Qualified explicit native attach remains a trusted adapter operation after exact identity and idle checks. There is no public command returning raw native ID or attach argv. |
| `hub-switch-agent` | Replaced by `ato agent reviewer` for the next technical reviewer and `ato agent handoff` for controller changes. Worker and user route are independent. |
| `hub-rollover` | Internal. Trusted adapter may park a qualified native session and begin fresh from a vetted brief under existing scope and limits. No public command to reset turns, work history or binding. |
| `task-create` | Replaced by `ato run` (freeze and admit) and `ato project validate` (read-only preview). Drop input hub/task IDs and caller-selected branch. |
| `task-status` | Replaced by `ato status TASK_ID` with independent check, technical approval and eventual user acceptance distinguished. |
| `task-run` | Replaced by `ato run` on first submission and `ato resume` after an admissible recovery. Host profiles replace `--worker-codex-home` and `--proxy`. |
| `task-review-only` | Replaced by `ato project recheck TASK_ID`. An already provisioned bound product receives checks/review only, under the same STOP, ownership and round limits. |
| `task-stop` | Replaced by `ato stop TASK_ID`. Retains nonblocking durable STOP and owned process cleanup. |
| `worktree-cleanup` | Replaced by `ato project cleanup TASK_ID --confirm`. Preserve original H1 dirty/lock/link checks, branch and evidence retention. |

### Launchers, runtime callers and Skills

- [`runner.runtime_command`](../runner.py) and the
  [registry bootstrap](../agent_registry.py) are the cross-process seam.
  [`run_cmd` fixture](../tests/fixtures/legacy_cli/run_cmd.py) used `_worker`.
  [`control`](../control.py) uses `_controller`. [`recovery`](../recovery.py)
  uses `_supervisor` and `resume --retry-failed`. Translate remaining
  unified calls to sealed private operations. A recovery attempt cannot
  provide its own authorization for `resume-run`. [`delegation`](../delegation.py)
  used `_dispatcher` and `start`, and is outside the new dispatch path.
- [`interaction`](../interaction.py), [`control`](../control.py) and
  [`recovery`](../recovery.py) have old command text in question, answer,
  operational, budget, handoff and recovery templates. The v1 composition
  uses typed records and new compact rendering before envelope hashing. It
  must not send those legacy templates, rewrite hashes of existing claims
  or bypass the queue gate. Reuse the [mailbox](../message_bus.py),
  [transport barriers](../transport.py), scoped actor checks and correlation
  rules. [`coordination`](../coordination.py) and
  [`failure_notice`](../failure_notice.py) currently use
  [`reader_command`](../ato_config.py) to generate `ato-read-handoff`.
  New wakes use sealed `read-handoff`. Legacy markers in retained receipts
  remain audit data. Protect current v1 canonical question, failure and
  final-completion claims at both dispatch barriers before any adapter call.
- `bin/ctask`, `bin/ctask-notify`,
  `bin/ato-read-handoff` at `86f19d9` and the installed H1
  `ato-hub` are removed from the new install/distribution surface. The
  [installer](../scripts/install.py) and
  [snapshot tool](../scripts/release_snapshot.py) own their prior-path
  inventory and rollback facts. The old installer options `--helpers`,
  `--hub` and `--hub-skills-dir`, along with the snapshot's `--include-hub`,
  are retired from new distribution selection. The new install always selects
  one entrypoint and can install the one Skill inertly.
- The legacy Skills at `86f19d9:skills/codex-long-task/` and
  `86f19d9:skills/ato-hub-orchestrator/` converge into [the ATO Skill](../skills/ato/SKILL.md).
  The unified Skill points at Task IDs and verified state and actor operations.
  It consumes actual user decisions and separate independent evidence.

## Release, cutover and validation

T6 distributes one public `ato` launcher and one Skill. Code-owned private
scripts ship inside the seal. Installation into a fresh selected target is
inert and idempotent for identical selections. The launcher names an exact
interpreter and snapshot, and child commands and reader instructions bind
the same source. The source/release manifest pins the source commit/tree,
dependency inventory, executable files, checked modes and interpreter
identity. Unknown, changed or cross-version references fail closed. Use the
existing snapshot stage/dry-run/activate/rollback distinctions. Read-only
diagnostics create no runtime, lock or policy side effects. Stage and
qualification use temporary isolated state and existing pinned optional
dependencies with fake local CLI/backends. Publishing source, stage success
or installing a launcher cannot activate a runtime. Real cutover needs a
separate operator instruction and a verified disposition of active work.

An authorized cutover checks the selected legacy and unified namespaces
for active Tasks and native processes, observer identity/heartbeat and
busy or queued/claimed/dispatching/unknown mailbox work, including live
turn slots. A busy or unverifiable boundary refuses. No drain, forced
quiescence or replay. Bind the transaction to the approved bin/state
paths and both exact snapshots/interpreters. Record prior existence,
bytes, permissions and checksums independently for `ato`, `ctask`,
`ctask-notify`, `ato-read-handoff` and `ato-hub`, as applicable. For
mixed selections record each prior source and require explicit
convergence authorization. Record old/new observer commands with exact
PID birth identities or verified absence, expected state roots, policy evidence, and durable
progress through quiescence and entrypoint writes. Stop only the verified
owned old observer gracefully, reserve the mailbox writer boundary and
recheck. Publish the single new launcher and remove only the recorded
superseded entries. Atomic single-file replacements and a private,
fsynced transaction journal make interrupted multi-entry installs
recoverable. Before accepting activation, verify the expected entrypoint,
exact observer identity and advancing heartbeat when observation was
authorized, and confirm the policies and namespaces. Never enable a new
observer or user route as a side effect of installation.

Rollback uses the same transaction and both namespaces. Verify quiescence
and identities again, reconcile the journal against actual files, restore
each recorded original executable's bytes and mode, remove only
transaction-created entries, and restore the previous authorized observer
with its recorded original interpreter/source/namespace. Repeat rollback
idempotently. A replaced file, ambiguous observer, busy mailbox or
unavailable prior seal leaves a recovery-required receipt instead of
overwriting or signaling an unrelated object. Retain the unified
Task/Run/evidence data and worktrees as diagnostics. A restored legacy
observer uses the original state namespace. No rollback replays native
turns, restores old SQLite/JSON over newer state, rebinds live actors,
or clears STOP, attempts or delivery history.
[Runtime upgrade and snapshot baseline](runtime-upgrade.md)

T1 checks exact old parser coverage, local links, whitespace and frozen
core/fixture bytes. Later stages verify their service/API contracts under
synthetic execution, including corrupt stores, unauthorized actors,
concurrent admission/ownership/STOP/handoff, stale approvals and unknown
delivery. T7 checks the public subprocess flow from run through answering
and exact acceptance, stop/resume, project isolation, reviewer integrity,
private runtime selection, installation and offline rollback. It also
runs the full offline suite, protected AST and architecture checks,
Python compilation, metadata and documentation checks. A fixture result
establishes only the tested local behavior. Historical state import,
provider or real-session qualification, live activation and user
acceptance of the release are outside these stage gates.
