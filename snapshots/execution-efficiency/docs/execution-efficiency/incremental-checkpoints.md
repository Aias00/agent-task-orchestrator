# Incremental host checkpoints (inactive candidate)

The new policy is explicit. Existing plans, result schemas, old checkpoint files,
attempt limits, STOP/grant rules and historical IDs retain their established
semantics. No task is migrated and nothing is activated or installed.

```json
{
  "execution_policy": {
    "version": 2,
    "max_continuations": 6,
    "max_waits": 4,
    "max_yields": 3,
    "max_no_progress": 4
  },
  "worker_session_policy": {"version": 1, "mode": "task_bound_native"},
  "checkpoint_policy": {
    "version": 1,
    "closing_margin_seconds": 30,
    "interval_seconds": 15,
    "max_age_seconds": 86400
  }
}
```

`closing_margin_seconds` is positive and strictly smaller than the existing
single-invocation `attempt_seconds`. The selected policy measures this protection
with a monotonic clock, including independent commands. Each invocation receives its own prompt
with the hard protection and an earlier closing deadline. The hard timeout is
unchanged. The installed worker protocol has no qualified checkpoint.request or
steer operation; the existing Runner loop records arrival at the closing margin
without sending input, extending the deadline, or starting another observer.
Unknown capabilities are never success. Supporting a different cooperative
request protocol requires separate qualification and a future explicit policy.

The same existing loop samples workspace fingerprints at `interval_seconds`
(1–300). It writes only changed snapshots, at most 64 periodic records per
invocation, followed by a terminal record after actual native/result validation,
process cleanup, and original host checks. A running snapshot is explicitly
nonresumable even if it contains useful file hashes. It cannot certify delivery,
completion, or permission to dispatch another native turn. The monitor is removed
in a finally block before independent checks execute.

## Evidence and publication

`incremental_checkpoint.py` keeps its authority under the private
`ATO_HOME/incremental-checkpoints-v1/<task-directory-hash>` outside the repository
and every explicitly granted writable root. Full-access workers cannot select
this policy because their private host authority would not be isolated. Each
receipt binds the exact project, frozen plan, task ID, parent workflow contract
and generation, runtime manifest, Runner actor, native/session/turn/message,
profile, invocation sequence, complete objective and cumulative v2 history.
It records the current step and code-derived remaining task/check IDs. Worker
summaries, raw prompts, stderr, config contents and native transcripts are not
copied into this store. Native identities and their prior failed/unknown history
are retained by the existing private worker ledger.

The safe workspace inventory includes bounded nonprivate artifacts, including
extensionless outputs and source files whose names contain session terminology.
Diagnostic streams and database files are excluded from source progress. Every selected file is opened via
no-follow directory descriptors and hashed with its mode. Private/auth/history,
dependency/build and hidden paths are excluded without reading their contents.
This is an explicitly scoped safe inventory, not certification of undeclared
external dependencies. Incomplete/oversized/racing reads cannot make a resumable
receipt. Deletions, additions, mode changes and symlinks invalidate the inventory.

Original checks still run through `Runner.command`, with their exact argv and
hard protection. The host records actual return codes and log hashes, input
inventory, executable/script bytes and modes, contract/profile and a hash of the
process environment. No environment values are persisted. A check that changes
its inputs is retained as an observation with invalidated input evidence. Only a
stable matching host check is labeled passed/failed in a checkpoint. Missing or
changed logs, script/interpreter changes and changed workspace/profile inputs
invalidate that evidence. No check execution is skipped by this stage; the
separate verification-cache policy remains a later task stage.

Publication is a bounded private reserve/write/commit protocol: atomically
reserve a unique sequence in the head, atomically write a new immutable record,
then atomically publish its digest in the head. Files are 0600 and directories
0700, with ownership/link/type checks and file/directory fsync. A crash before or
after record writing leaves pending/orphan evidence and refuses resume; a
partially written or replayed record cannot become terminal authority. The
complete chain is validated on resume, with no deletion or compaction: at most
2048 records and 32 MiB read per task validation, 2 MiB per JSON receipt
and 128 KiB for the untruncated reconstruction context. Reaching
a bound requires inspection, never history reset. Terminal receipts expire after
`max_age_seconds` (default 86400; explicit range 1–604800); future dates fail
closed. Expiry is a barrier, not permission to retry or mint a replacement ID.

## Continuation and reconstruction

Before spending another invocation, Runner validates the exact owner and STOP,
parent and runtime binding, private head and complete record chain, actual files,
profile, worker output, original evidence, native receipt and cumulative ledger.
Invocation/failure/continue/wait/yield counters and all history must match; time
accounting may only grow through existing host checks or durable waits. Summary
text and heartbeat activity cannot replace any proof or reset no-progress bounds.

The v1 task-bound worker policy uses the exact verified native ID. The optional
new policy below explicitly starts a fresh task-owned native ID at every clean
continuation and provides the host-validated checkpoint in the initial prompt:

```json
"worker_session_policy": {"version": 2, "mode": "checkpoint_reconstruction"}
```

This mode requires `checkpoint_policy`. It preserves every preceding session's
source, native identity and native readback proof in an ancestor list, and all
turns, output digests and accounting history remain cumulative. The adapter
verifies the ancestor/current native files and unchanged effective profile. The
new identity comes from the newly dispatched native protocol receipt; no model
can choose an ID or import command. Reconstruction is never labeled native
resume or context reuse. Lost/claimed/unknown delivery, changed native files,
stale generations or unconfirmed cleanup refuse both modes; they never trigger
an automatic switch from resume to a new ID.

Valid continue records do not spend failures, including when final artifact
checks still fail. Real timeout, malformed outcome/JSON, cleanup failure or
checkpoint-publication failure spends a real failure at most once in the selected
policy and preserves unknown side effects. Timeout return code 124 remains a
timeout across the native adapter wrapper. The selected policy also bounds the
final SIGKILL wait; lack of exact cleanup confirmation remains quarantined.
Existing invocation, no-progress, continue/wait/yield limits remain finite.

Workflow freezes and propagates the policy into real child Runner execution,
rejects stage overrides, and retains independent total-task acceptance. A stage
checkpoint/end-turn is never overall workflow completion. Host-recheck retains
existing STOP, owner, terminal-proof and delivery-unknown barriers and cannot
promote a recorded checkpoint refusal. All this remains an inactive local
candidate awaiting the frozen origin checks and later complete-task qualification.
