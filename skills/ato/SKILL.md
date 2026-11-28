---
name: ato
description: Use MCP first to submit, inspect, stop and resume durable ATO tasks; preserve task IDs, independent verification and human authority. Use authorized CLI fallback only when MCP is unavailable.
---

# ATO controller

This skill controls the existing ATO engine. It does not change Worker/Reviewer
adapters, sandbox defaults, retries, independent host checks or notifications.
An already dispatched worker executes its task; it must not dispatch nested ATO work.

## Discover and submit

1. Discover the connected ATO MCP server's tools (`tools/list`, including further
   pages if supplied); inspect their input schemas. Clients may prefix tool names.
   Match the `ato_*` names below to the actual connection. Do not guess a tool,
   server identity, permission or repository from a filesystem path.
   Reconnect or refresh tool discovery after a server schema upgrade; cached
   schemas may still hide required nested execution fields.
2. Call `ato_repositories({})` and select an authorized `data.repositories[].alias`.
   Use that alias as both `repo` and inline `spec.repo`. Specs are objects, not file
   paths or JSON strings. Read [the exact contract](references/controller.md) and
   [example calls](templates/controller-calls.example.json). Include the complete
   objective, scope, authorization and acceptance criteria (command checks when
   authorized, manual criteria when human acceptance is required). Explicitly set
   `worker_contract.timeout_seconds` to an integer 5..7200 based on the task.
   If a reviewer is configured, explicitly set `reviewer.timeout_seconds` to an
   integer 5..3600. For a 2–8 member joint panel, set that timeout on every
   `reviewer.members[i]` instead; IDs must be unique and `policy` is `all`.
   See [the panel spec example](templates/joint-review-spec.example.json).
   New MCP requests have no implicit execution timeout defaults.
   Optional guardrails are `protected_paths` and `forbid_delete_paths`.
3. Call `ato_validate({"repo": ALIAS, "spec": SPEC})`. Require `ok:true` and
   `data.valid:true`. The transport preview returns `repo`, `valid`, `spec_sha256`,
   `base_commit`, `worker_kind`, `sandbox`, `timeout_seconds`, `timeout_source`, and
   `reviewer_timeout_seconds` / `reviewer_timeout_source` for single review, or
   `reviewer.members[].timeout_seconds/timeout_source` for a panel. Inspect
   these effective values before submission; MCP explicit timeouts have source
   `spec`. Validation starts no worker. A file read inside the worker cannot change
   its outer execution deadline. MCP network/request timeouts are separate.
4. Before `ato_submit`, generate a unique operation key, e.g. `submit-` plus a UUID4
   (`python3 -c 'import uuid; print("submit-" + uuid.uuid4().hex)'` where available).
   Durably save it with the server identity, principal name (no token), operation,
   exact arguments, known task ID and outcome in private controller task state.
   Keep this outside Git and worker prompts; retain it across restarts. If durable
   state is unavailable, stop before a write and explain the missing capability.
5. Call `ato_submit({"repo": ALIAS, "spec": SPEC, "request_id": KEY})` once.
   Save `data.task_id` immediately. An accepted/queued reply is not completion.
   Inspect `data.execution.timeout_seconds` / `timeout_source`; use fresh status
   for effective run settings after a replay or resume.
   All later operations follow that Task ID; do not create a replacement task to
   continue the same objective. New stop/resume actions each get their own key.

Read `structuredContent` (official Python SDK: `structured_content`), or parse the
JSON text content if the client omits structured content. Require the canonical
`{schema_version:1, ok, data, error}` envelope. Inspect `ok` and MCP `isError`
(Python `is_error`); missing/malformed results are not success. Do not retry a
mutation merely because the client could not parse its reply.

## Observe, handle questions and finish

- `ato_list({"repo": ALIAS})` returns allowed tasks; omitting `repo` lists all
  allowed aliases. `ato_status({"task_id": ID, "check_history": true})` supplies
  current status, reason, execution, checks/history, review, product and pending.
  Use `ato_diff({"task_id": ID, "stat": true})` for the summary, `stat:false`
  for the bounded patch, and `ato_trace({"task_id": ID})` for bounded events.
  Joint panels expose `review_panel` in status. Use `ato_reviews({"task_id": ID})`
  for history and then `group_id`, `member_id`, `section` and `next_cursor` for
  complete evidence. Partial groups, unknown ACP outcomes and a member error
  are not approval. `complete` describes the group; `approval_usable` also needs
  a completed run and matching configuration/product. Check task status and
  manual acceptance separately. Never infer a pass from a truncated trace.
- Poll status/inbox at a bounded cadence only while this controller is executing
  (e.g. 5 seconds, back off to 30 seconds). On a local wait deadline retain the
  Task ID and report current facts; it does not stop the task or authorize a retry.
  No detached poller, webhook or native wakeup is created by this skill.
- For `waiting_input` or nonempty `pending`, call `ato_inbox({"task_id": ID})`.
  Relay each question with its Task ID and inbox `request_id`. That inbox ID is
  the `question_id` for answering; it is NOT the mutation idempotency key.
  Only an actual authorized human's input may be recorded via `ato_answer` using
  separate human authority. Bots must never possess the human token, invent a
  reply, auto-approve, or impersonate a human through CLI. Answering alone neither
  resumes nor accepts. Keep the question/task linkage until the answer is recorded.
- Report verified completion only after a fresh status says `completed`, its
  `reason` contains no unresolved blocker, all required independent checks are
  `passed`, configured review is satisfied, and `pending` is empty. Inspect
  `product.diff_summary` / diff `summary` for `files_deleted`, `tests_deleted`,
  `tests_modified` and scope. Missing evidence is not a pass; inspect check history
  and trace. Distinguish engine completion from human acceptance, merge or deploy.
  Exhausted checks block with `CHECKS_FAILED`. A worker saying “done” is not evidence.
- Report Task ID, verified outcome, check/diff evidence, remaining limitations and
  next step. Read `completion_notification` and `watchdog` as separate facts:
  delivery/queued notification does not establish completion or human approval.

## STOP, resume and uncertain delivery

Use `ato_stop({"task_id": ID, "request_id": NEW_KEY})` for an authorized STOP.
Poll fresh status for `execution.stop_requested`, execution state/verification,
status/reason, and any `acpx_reviewer` cleanup facts. A STOP receipt alone does not
prove process cleanup; `CLEANUP_BLOCKED`, unknown execution or unresolved reviewer
outcomes need operator reconciliation. A previously completed task may remain
completed after STOP. Do not kill arbitrary PIDs or auto-resume an explicit STOP.

Resume only when authorized, pending human input is resolved and the task is
eligible (`stopped`, `blocked`, `waiting_input`; legacy completion needs inspection).
Call `ato_resume({"task_id": ID, "request_id": NEW_KEY})`; optional
`timeout_seconds` is an integer 5..7200 for this run only. Omit it to use the
frozen spec timeout (including the original default in legacy/local/API specs),
not a previous run override. Task ID, worktree, spec digest and acceptance stay unchanged.
Read execution/run_history/trace for the actual new run, not a cached receipt.

For disconnects, timeouts, protocol errors or missing replies after any write,
mark outcome unknown. Reconnect/discover and retry only the same operation, same
arguments and same `request_id` with the same principal/server journal. The
journal can return the original (possibly stale) result or `REQUEST_UNRESOLVED`.
Poll any known `error.task_id` via status/trace. A 404 is not proof of no effect.
If still unresolved, retain the key and escalate for host reconciliation; do not
change keys, switch to CLI submission, erase journal rows or infer rollback.
`IDEMPOTENCY_CONFLICT` means the key was used for other arguments/operation; inspect
the saved intent instead of guessing. See [recovery details](references/controller.md).
Historical submissions may lack explicit timeouts. Do not add fields to a retry:
the server looks up exact historical intents before applying the new policy.
If the client's new schema blocks that retry locally, retain the intent and use
the authorized host reconciliation path described in the reference.

## Availability, authority and fallback

Missing tools can mean missing scope, client filtering, policy or version mismatch.
Explain the affected operation and needed capability. `read` permits inspection;
`control` permits validate/submit/stop/resume; `human` permits actual human answers.
`AUTH_REQUIRED`, `FORBIDDEN` and client approval denial are authority failures,
not reasons to bypass controls. Do not upgrade scopes or acquire a human token.

Only when MCP is unavailable AND the existing local CLI is already authorized,
explain that fallback and use [the CLI mapping](references/controller.md#cli-fallback).
A disconnected write is ambiguous, not permission to resubmit through CLI. CLI
has no transport request journal. HTTP API and CLI remain supported for operators
and scripts. MCP exposes no accept, recheck, cleanup, merge or deploy controls;
do not use fallback to silently add these actions.

New `api`/`mcp` tasks have no native conversation binding. MCP polling is not push
and does not bind or wake Codex, Cursor, AGY or Grok Bot. Do not synthesize session
IDs, set origin variables to impersonate a client, or replace existing notification
mechanisms. Existing native-origin tasks retain their provenance when controlled
through MCP. See [setup and migration](references/setup.md).
