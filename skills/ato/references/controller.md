# Controller contract and recovery

Discovery is defined by `ato/server/service.py::MCP_OPERATIONS` and
`ato/server/spec_schema.py`; REST/OpenAPI retain optional timeout defaults.
The explicit-timeout schema/preview change has regression code but is not yet
runtime qualified. Earlier SDK/native reports apply only to their recorded revisions.
Runtime discovery is authoritative. No unknown arguments are accepted. Boolean
arguments must be JSON booleans; omit optional top-level arguments rather than
using null. Nested nullable fields are identified in the discovered spec schema.

| Tool | Exact arguments | Scope | Successful `data` |
| --- | --- | --- | --- |
| `ato_repositories` | `{}` | read | `repositories:[{alias}]` |
| `ato_validate` | `repo`, `spec` object | control | `repo, valid, spec_sha256, base_commit, worker_kind, sandbox, timeout_seconds, timeout_source`; reviewer timeout/source when configured |
| `ato_submit` | `repo`, `spec` object, `request_id` | control | admission receipt with `task_id, status`; poll status |
| `ato_list` | optional `repo` | read | `tasks:[{task_id,repo,status,reason,updated_at}]` |
| `ato_status` | `task_id`, optional `check_history` (false) | read | status/execution/checks/review/product/pending and notification facts |
| `ato_diff` | `task_id`, optional `stat` (false) | read | `task_id, base, branch, worktree_state, summary, files`; bounded `patch` unless stat |
| `ato_reviews` | `task_id`; optional `group_id`, `member_id`, `review_id`, `section`, `cursor`, `limit` | read | paginated `items`, `total`, `next_cursor`; full member/aggregate evidence |
| `ato_trace` | `task_id` | read | `task_id, events` (bounded) |
| `ato_inbox` | `task_id` | read | `requests, count` (open questions for this task only) |
| `ato_stop` | `task_id`, `request_id` | control | status receipt; poll fresh cleanup facts |
| `ato_resume` | `task_id`, `request_id`, optional `timeout_seconds` integer 5..7200 | control | new-run receipt; poll fresh status |
| `ato_answer` | `task_id`, `question_id`, `answer`, `request_id` | human | question answer receipt; no implicit resume/accept |

`task_id` matches `task-[0-9a-f]{32}`; `question_id` matches
`request-[0-9a-f]{32}`. `answer` must be nonblank and at most 8192 UTF-8 bytes.
Mutation `request_id` matches `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}` and identifies
an operation, not an MCP JSON-RPC message. UUID4-based keys avoid collisions;
never use the illustrative keys in the example calls for real work.

`spec.repo` must exactly equal the selected repository alias. No filesystem root,
external writable root, spec/evidence path or shell-dispatch argument is accepted.
A spec can contain executable acceptance checks: control access is trusted code
execution authority, not a multi-tenant sandbox. Never include credentials in it.

New MCP validate/submit calls require `spec.worker_contract.timeout_seconds`
(integer 5..7200) and, when reviewer is not null, `spec.reviewer.timeout_seconds`
(integer 5..3600). Omissions, null, booleans, strings and out-of-range values
return `MCP_WORKER_TIMEOUT_REQUIRED` or `MCP_REVIEWER_TIMEOUT_REQUIRED` with a
static field-specific message, before new intent persistence or worker admission.
No coercion or hidden 600/300 fallback is applied. Other optional fields remain
optional: worker kind `codex`, sandbox `workspace-write`, max attempts 3; reviewer
is disabled when absent/null. See discovered nested schemas for agent/model,
acceptance argv/manual shape, reviewer, worktree and guardrail fields and limits.
Model/effort choices and adapter support still follow the engine's rules.

Choose execution timeouts explicitly for the agreed workload; the example's
1800 seconds is illustrative. An optional reviewer example is
`"reviewer":{"agent":{"kind":"codex"},"timeout_seconds":300}`.
Validation only previews effective settings and never starts a worker. Inspect
`timeout_seconds` / `timeout_source` (`spec` for MCP), plus reviewer timeout/source.
Submit/resume/status expose worker values in `execution` and `run_history`.
Reading a config/spec file inside the worker cannot extend its outer deadline.
Native MCP request/network timeouts do not set the worker execution timeout.
After schema changes reconnect/rediscover tools rather than relying on cached fields.

Every business result is `{schema_version:1,ok:true,data:...,error:null}` or
`{schema_version:1,ok:false,data:null,error:{code,message,task_id}}`; error task ID
can be null. MCP errors additionally set `isError:true`; protocol/transport errors
may have no envelope. Tool visibility is scope-filtered and calls independently
authorized. `read` and `control` are separate scopes, usually both on a coordinator.
A `human` principal must not also have `control`.

## Durable intent and recovery

Before each new mutation persist a private record, for example:

```text
server = deployment identity (including persistent ATO store/journal)
principal = coordinator name (never bearer token)
operation = submit
request_id = submit-<fresh UUID4 hex>
arguments = exact repo/spec payload sent, without credentials
outcome = prepared | unknown | resolved | unresolved
task_id = receipt/error task ID when known
```

Use the controller's existing durable task store or a private file written and
flushed before sending; do not store this under a tracked repository or workers'
readable prompts. Record response and task ID before considering another action.
After controller restart load the saved record, never regenerate its key. If the
record was lost, reconcile with the host; matching a list entry by title is not
proof that a submission did or did not happen.

The host journal keys on principal name plus a hash of the key, and checks the
operation/argument digest and repository binding. Cross-MCP/HTTP replay works
only with the same principal and persistent journal; HTTP uses `Idempotency-Key`
instead of the tool argument. CLI does not participate in this journal. Do not
change identity, server store or transport to evade a denial or unresolved write.

On unknown delivery a same-key retry cannot execute a second effect. It returns
cached data or `REQUEST_UNRESOLVED`; it does not guarantee exactly-once execution
across a crash. Successful replay is historical, so always read fresh status.
Unresolved intent requires host inspection of task trace, run/worktree/process
state and journal. A reserved task ID can precede task persistence; `NOT_FOUND`
does not authorize a replacement. No timeout/lease expiry, reset or automated
recovery endpoint exists. Never delete the journal to force a retry.

Legacy intents with omitted timeouts remain replayable with exact original
arguments/key, including unresolved intents. The server reauthorizes and checks
the shared REST/MCP journal before applying new-submission timeout validation.
Changing a legacy payload to add a timeout with the same key is a collision.
`ato_validate` has no replay identity and rejects an omitted timeout even for an
old payload; do not gate reconciliation of an already-sent intent on new validation.
Some clients enforce the refreshed tool schema locally and cannot send an old
payload. Retain the original intent, inspect the known task ID, and involve the
authorized host operator. The shared REST endpoint can reconcile that exact
principal/key/arguments against the same journal using existing authority; raw
MCP calls reaching the service also retain this replay path. This is not permission
to change identity, resubmit via CLI, or bypass client approvals. Never edit old
journal digests, task specs or saved arguments to satisfy a new schema.

## CLI fallback

Use `ato --help` for the installed CLI version. Only use an already authorized
CLI when MCP is unavailable, with a verified local repo path mapped by the operator
to the intended alias. Explain the fallback. Do not infer host paths from aliases.

| MCP operation | Existing CLI equivalent |
| --- | --- |
| repositories | No equivalent alias discovery; obtain operator's authorized local mapping |
| validate | `ato project validate --repo PATH --spec FILE` |
| submit | `ato run --repo PATH --spec FILE` |
| list | `ato list --repo PATH` |
| status | `ato status TASK_ID --check-history` |
| diff | `ato project diff TASK_ID --stat` (omit `--stat` for patch) |
| trace | `ato project trace TASK_ID` |
| inbox | `ato inbox` (filter returned questions by Task ID before relaying) |
| stop | `ato stop TASK_ID` |
| resume | `ato resume TASK_ID` (optional `--timeout-seconds SECONDS`) |
| human answer | Authorized human only: `ato answer QUESTION_ID --answer TEXT` |

For CLI spec files use the real authorized repository path, not the MCP alias.
CLI submissions from AGY use `ATO_SUBMISSION_KIND=agy`; from Cursor terminals
use `ATO_SUBMISSION_KIND=cursor`. Do not apply these variables to MCP. Codex CLI
origin binding remains subject to existing verification. CLI and transport previews
both report timeout/source. Independent verification and STOP rules are the
same. CLI `project recheck`, `accept`, `project cleanup`, merge and deployment
remain separate operator actions requiring their own authorization.

Local CLI and REST worker/reviewer timeout defaults remain 600/300 seconds; new
MCP task requests require explicit values. The optional resume override is for one
run (`resume_override`); later resumes without an override use the frozen spec
timeout, including its original default, without mutating the spec. Review
rework (`max_review_rounds`, default 10, 1..50), worker failure (`max_attempts`,
default 3), guardrail/check repair and repeated-no-progress counters remain
separate. `max_no_progress_rounds` defaults to 2 (1..10); `REPEATED_NO_PROGRESS`
and `REVIEW_CHANGES_REQUESTED` are blockers to inspect, not reasons to resubmit.
Watchdog alerts are advisory; healthy review rework is not execution failure.


## Joint panels and complete evidence

Use either the existing single `reviewer.agent` shape or `reviewer.members`
(2..8); never combine them. Members require unique stable `id`, `agent` and an
explicit MCP `timeout_seconds` (integer 5..3600). Supported kinds remain codex,
claude, agy, codex_acpx; ACP remains fresh-only with host defaults. Shared and
member checklists apply together. `policy` supports only `all`; execution is
sequential and max rounds counts entire groups. See
[the panel example](../templates/joint-review-spec.example.json).

`ato_reviews` requires read scope and the task's repository authorization. Default
`section: "groups"` lists groups; `limit` is 1..20, default 20. Follow `next_cursor`
until null while retaining task/filters/section. Listings use ascending DB ID
cursors, detail arrays use offsets. With `group_id`, use `section: "members"` for
complete summaries, configs and bounded ACP facts. `findings`, `checked_items`,
`residual_risks` return aggregate evidence or, with `member_id`, the original
member's evidence. An incomplete group has no aggregate; select a member.
Aggregate sources reference member ID and original finding ID within the group.
`section: "legacy"` lists old single reviews; `review_id` plus an evidence section
reads them. Neither native sessions nor raw runtime logs are public.

Do not equate one member's approve, a historical group, `complete`, or STOP
acknowledgment with task completion. Inspect `review_panel.state`, `verdict`,
`config_matches`, `run_completed`, `approval_usable`, and task checks/pending.
A failed member stops remaining execution; blocked/request_changes receipts are
collected from every member. Rework addresses all blockers and the next round
reviews everyone again. Full recheck reviews everyone. Checks-only reuse requires
a complete same-product/same-config all-approve group from a completed run.

No control mutation tool was added. Authorized local editing uses
`ato agent reviewer TASK_ID --reviewer-id ID --kind KIND` for an existing member;
it requires idle/revision fencing and invalidates changed approvals. Add/remove
members in a new task spec. Capacity errors retain all findings: inspect history
and resolve the capacity/scope issue, never discard blockers or blindly resume.
This extension is implemented in source; tests and deployed-server updates still
require separate authorization. Refresh discovery only after the actual update.
