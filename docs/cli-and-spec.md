# CLI & Specification Reference

## Commands

| Command | Description |
| --- | --- |
| `ato run --repo PATH --spec FILE [--foreground]` | Submit a task into a new isolated Git worktree |
| `ato list [--repo PATH]` | List tasks (optionally filtered by repository) |
| `ato status TASK_ID` | Inspect task state, execution, checks, review, product, notifications, and watchdog |
| `ato stop TASK_ID` | Durably request STOP and terminate live worker process group |
| `ato resume TASK_ID [--foreground] [--timeout-seconds SECONDS]` | Continue a `stopped`, `blocked`, or answered `waiting_input` task |
| `ato inbox` | List open human questions across tasks |
| `ato answer REQUEST_ID --answer TEXT` | Record a user reply to an open question |
| `ato accept TASK_ID [--evidence FILE]` | Record optional manual user approval receipt |
| `ato project validate --repo PATH --spec FILE` | Validate and preview normalized spec digest |
| `ato project diff TASK_ID [--stat]` | Show structured diff summary (and unified patch unless `--stat`) |
| `ato project trace TASK_ID` | Show bounded task lifecycle event timeline |
| `ato project recheck TASK_ID [--checks-only]` | Re-run frozen host checks (and reviewer unless `--checks-only`) |
| `ato project cleanup TASK_ID --confirm` | Retain product commit on branch and remove worktree |
| `ato agent doctor [--probe-versions]` | Inspect local Codex, Claude, Cursor, AGY, and Pi CLI availability |
| `ato agent binding TASK_ID` | Inspect task origin, worker adapter, and reviewer config |
| `ato agent reviewer TASK_ID --kind KIND [--model M] [--effort E]` | Select or change task reviewer (`codex`, `claude`, `agy`) |
| `ato agent ask TASK_ID --kind K --question Q --reason R` | Open a human question in `ato inbox` |
| `ato system status` | Inspect v2 runtime, Observer heartbeat, and notification counts |
| `ato system notification-policy [--repo PATH]` | Inspect global notification and repo mute policy |
| `ato system observer-start` / `observer-stop` | Start or stop the background Observer/Watchdog daemon |

## Task Specification (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "repo": "/path/to/repo",
  "base_ref": "HEAD",
  "worker_contract": {
    "objective": "Implement feature X with unit tests",
    "scope": "Only files under src/ and tests/",
    "authorization": "Local edits and test execution only",
    "agent": {"kind": "codex"},
    "sandbox": "workspace-write",
    "max_attempts": 3,
    "acceptance": [
      {
        "id": "unit-tests",
        "kind": "command",
        "description": "Test suite passes",
        "checks": [["python3", "-m", "unittest"]]
      }
    ]
  },
  "reviewer": {
    "agent": {"kind": "codex"},
    "max_review_rounds": 10
  },
  "guardrails": {
    "protected_paths": ["README.md"],
    "forbid_delete_paths": ["tests"]
  },
  "worktree": {
    "shared_env_links": [".venv"]
  }
}
```

Supported worker kinds: `codex`, `claude`, `cursor`, `pi`.
Supported reviewer kinds: `codex`, `claude`, `agy`.

Worker `timeout_seconds` defaults to 600 (range 5–7200); reviewer timeout defaults
to 300 (range 5–3600). `ato project validate` shows `timeout_seconds` and
`timeout_source` (`default` or `spec`), plus `reviewer_timeout_seconds` and
`reviewer_timeout_source` when configured. `ato run`, `ato resume`, and `ato status`
receipts expose the effective worker timeout and source in `execution` and each
`run_history` entry. Submission/resume events in `ato project trace` record them too;
old records with unknown provenance report `legacy`.

Use `ato resume TASK_ID --timeout-seconds 3600` to override only the new run
(`timeout_source: resume_override`), in foreground or background. The value must
contain only base-10 digits in 5–7200. This preserves the Task ID, isolated
worktree, frozen spec digest, and acceptance criteria. A later resume without
the option returns to the spec/default timeout. The `task_resumed` event also
records `previous_timeout_seconds` and `timeout_overridden`.

The `pi` worker runs `pi --mode json --print` under macOS `sandbox-exec` with `pi-path-guard.mjs` (see [ato/adapters/pi.py](../ato/adapters/pi.py)).

Cursor is supported only as a worker (`"agent": {"kind": "cursor"}`), with no
Cursor coordinator/controller or reviewer. Executable lookup uses `ATO_CURSOR_BIN`,
then `CTASK_CURSOR_BIN`, then `cursor-agent` on PATH or in the managed CLI directory;
it does not select a generic `agent` binary or shell alias. Set
`ATO_SUBMISSION_KIND=cursor` for Cursor submissions to suppress ambient Codex origin IDs.
Optional model names support parameter lists, for example
`claude-opus-4-8[context=1m,effort=high,fast=false]` (maximum 256 characters,
no whitespace, quotes, control characters, or leading `-`).

The Cursor worker runs `cursor-agent -p --output-format stream-json` under macOS
`sandbox-exec`. Read-only tasks also use `--mode plan`; workspace-write tasks use
`--workspace`, `--trust`, and `--force`. The outer sandbox restricts writes to the
worktree (excluding sensitive paths such as `.git`, `.env`, and `.cursor`), task
runtime, and native `~/.cursor` state; read-only tasks cannot write the worktree.
Cursor's nested sandbox is disabled only inside this outer sandbox. Missing sandbox
support blocks the task with `UNSUPPORTED_CAPABILITY`. Final output must contain a
valid worker result JSON object, optionally enclosed in a single JSON code fence.


## Rework, retries, and no progress

`reviewer.max_review_rounds` defaults to **10** (integer range **1..50**), including
reviewers selected with `ato agent reviewer`. This limits review turns, including
the initial review. A `request_changes` verdict continues normal rework while
progress is made; reaching the limit blocks with `REVIEW_CHANGES_REQUESTED`.
Previous blocker findings are supplied to the next reviewer turn.

`worker_contract.max_attempts` (default 3) caps execution failures separately from
review rework. Only a failed worker execution increments `execution_failures`;
exhaustion blocks with `WORKER_FAILED`. Guardrail and independent check failures
use a separate `verification_failures` repair counter with the same cap, reset
once both pass and execution advances to review. Normal review rework consumes
neither failure budget. With `max_attempts=1`, advisory check failure still settles
as `completed` with reason `CHECKS_FAILED`; guardrail violations still block.

Optional `worker_contract.max_no_progress_rounds` defaults at runtime to **2**
(integer range **1..10**). Omission adds no field to the normalized contract,
preserving the existing canonical spec and digest. The first failure or blocker
round establishes a baseline (streak 0). Consecutive identical execution, check,
or guardrail failures on an unchanged tree increase the streak. Review rework
also increases it when all previous blockers remain, even if the tree changed;
blockers are matched by file, category, and case/whitespace-normalized title.
Resolving a previous blocker resets the review streak. At streak 1, worker
feedback warns about the repeated issue. At the configured limit the run pauses
with `status=blocked`, `reason=REPEATED_NO_PROGRESS`, an actionable next step, a
trace event, and a failure notice. Inspect the issue before resuming.

`stage` exposes the category (`implement`, `execution_failure`, `review_rework`,
`verification_repair`, or `repeated_no_progress`), monotonic `turn`/`attempts`,
independent counters and limits, `progress_made`, `repeated_kind`, and
`repeated_items`. Runtime directories use `attempt-{turn}` across all cycles.
Watchdog's advisory `EXECUTION_FAILURE_RETRY` signals repeated execution failures;
healthy review rework produces no failure alert. `REPEATED_NO_PROGRESS` signals a
repeat from streak 1 or a run paused for that reason. `UNHANDLED_FAILURE` includes
the failure category. Watchdog does not itself stop or resume execution.

Once a terminal task has a matching successful sample and no active or pending
alerts, Watchdog reports `state="settled"`. Cached settled samples stay settled
even after the 180-second freshness window; changed tasks require a new sample.
