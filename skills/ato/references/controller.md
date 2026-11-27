# Controller contract and recovery

Checked against `ato/server/service.py::OPERATIONS` and real SDK responses.
Runtime discovery is authoritative. No unknown arguments are accepted. Boolean
arguments must be JSON booleans; optional arguments are omitted, not null.

| Tool | Exact arguments | Scope | Successful `data` |
| --- | --- | --- | --- |
| `ato_repositories` | `{}` | read | `repositories:[{alias}]` |
| `ato_validate` | `repo`, `spec` object | control | `repo, valid, spec_sha256, base_commit` |
| `ato_submit` | `repo`, `spec` object, `request_id` | control | admission receipt with `task_id, status`; poll status |
| `ato_list` | optional `repo` | read | `tasks:[{task_id,repo,status,reason,updated_at}]` |
| `ato_status` | `task_id`, optional `check_history` (false) | read | status/execution/checks/review/product/pending and notification facts |
| `ato_diff` | `task_id`, optional `stat` (false) | read | `task_id, base, branch, worktree_state, summary, files`; bounded `patch` unless stat |
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
origin binding remains subject to existing verification. CLI timeout preview is
richer than transport validation. Independent verification and STOP rules are the
same. CLI `project recheck`, `accept`, `project cleanup`, merge and deployment
remain separate operator actions requiring their own authorization.

Worker/reviewer timeout defaults are 600/300 seconds. The optional resume override
is for one run (`resume_override`), later resumes use spec/default again. Review
rework (`max_review_rounds`, default 10, 1..50), worker failure (`max_attempts`,
default 3), guardrail/check repair and repeated-no-progress counters remain
separate. `max_no_progress_rounds` defaults to 2 (1..10); `REPEATED_NO_PROGRESS`
and `REVIEW_CHANGES_REQUESTED` are blockers to inspect, not reasons to resubmit.
Watchdog alerts are advisory; healthy review rework is not execution failure.
