# Verification policy v1 (inactive candidate)

This stage adds verification_cache.py to the real Runner.checks path and
Workflow.check_total. It does not change any existing task contract or enable a
deployed runtime. A plan without verification_policy retains the original
fresh-check behavior and JSON semantics. The exact opt-in schema is:

```json
{
  "verification_policy": {
    "version": 1,
    "checks": {
      "artifact": {
        "inputs": ["src", "tests/check_artifact.py"],
        "env": ["BUILD_MODE"],
        "cacheable": true
      }
    }
  }
}
```

Keys are actual task/criterion IDs. There are no model-selected commands,
imports, cache directories, bypass flags or TTL extensions in this schema.
Unknown fields, IDs, absolute paths and parent traversal are rejected. Missing
declarations and cacheable=false mean fresh execution. Workflow stages inherit
only host-selected entries for exact task IDs; a generated task with a different
ID remains uncached. The planner still selects frozen original command argv.
Host-recheck verifies the same policy inheritance and runs fresh.

## Trust and conservative dependency scope

The policy declares complete inputs and identifies diagnostic outputs that are
not acceptance dependencies. A cacheable declaration alone is insufficient.
Version 1 qualifies static offline Python script checks invoked with the exact
selected interpreter, without additional interpreter flags/arguments. It checks
static file access and local imports, rejects unknown imports, dynamic paths or
introspection, shell/native tool wrappers, external/network/process/clock state,
callable definitions/annotation evaluation, filesystem stat/stream semantics,
startup hooks and unknown search paths. Diagnostic output reads are permitted
only inside an update to that same output with its return value discarded;
using an undeclared diagnostic output in acceptance forces a fresh run.
Successful receipts also bind diagnostic output post-state; deleting or
corrupting a counter/output cannot reuse its prior success.
Unsupported commands execute using
their original argv and return code; an unknown dependency is not a cache hit
or qualification success. This intentionally conservative scope can be extended
only with independent dependency evidence, not by claiming arbitrary programs
are pure. An undeclared input or unknown dependency always runs fresh.

Every fingerprint includes declared file/directory contents, membership and
modes; automatic script/local import bytes and modes; interpreter and Python
toolchain evidence (including the selected shared Python library and platform);
dependency/config lockfiles; original argv hash; immutable
contract; project/task/runtime selection; parent contract and execution
generation; sandbox, permission and host-handoff boundary; and the execution
environment. Environment values are stored as hashes only, including changes
to variables beyond the declared whitelist. Raw script contents, environment
values, credentials, prompts, stderr and native transcripts are not copied
into receipts. Input symlinks, hard-linked files, missing files, mutable reads,
unsupported startup/dependency state or bounds overflow invalidate reuse.

A creation-time witness and successful return-code receipts are stored under
ATO_HOME/verification-cache-v1, outside the project and explicit worker write
grants. Full-access workers and overlapping private roots are uncached. The
private storage has owner-only modes, no-follow reads, bounded atomic writes
and a local flock. Missing, partial, wrong-project/contract/policy or
nonprivate witnesses cannot be recreated by a check. Invalid cache evidence
falls back to the original check, preserving legacy execution authority.

## Execution, provenance and freshness

Runner guards still run before lookup and before accepting a hit, including
STOP and frozen contract/runtime checks. A present execution owner requires an
exact current PID and verified kernel start identity. Unknown delivery or
cleanup cannot reuse a check. A receipt is published only after real host execution returns
zero, exact process cleanup completes, the original log exists with matching
bytes/mode, and complete acceptance-input fingerprints before/after execution
agree. Diagnostic outputs are bound to the actual successful post-state. Failed,
pending, timed-out or unstable runs are not cached. Old successes are retired
before fresh executions, including final executions, so a failed fresh check
cannot revive an earlier pass.

A hit returns the original log path. verification_reused events retain the
original run, input/source/toolchain fingerprint, execution start/end/duration,
return code and original evidence proof. It is not a new process execution or
model invocation. verification_executed and existing command_started/finished
events distinguish real executions. Checkpoint observations preserve the full
original reuse receipt and revalidate its inputs and expiry on reconstruction.
Receipt lifetime is bounded to 3600 seconds (code-owned; no user TTL extension).
Future timestamps, expiry, missing/changed/link logs or input/environment drift
invalidate reuse. No counters/history/attempts are reset.

Runner final/full/integration labels always execute fresh; internal callers
also have an explicit fresh keyword. Host/external/manual/health/permission
labels stay fresh. Manual workflow criteria never enter the command cache:
their original acknowledgement/evidence/contract/generation checks run on each
check_total call. External state must be declared cacheable=false and unknown
external dependencies are rejected independently of that declaration. Before
an opted-in Workflow can become completed it executes check_total(fresh=True)
over every command and rereads all manual acknowledgements. A stage hit cannot
supply total-task completion.

## Bounds, compatibility and handoff

File inventories are bounded to 8192 entries and 128 MiB, logs to 16 MiB,
private receipts to 4 MiB and 512 current command entries; imports are bounded
to 128 local sources. Exceeding bounds runs fresh without inventing evidence.
Unrelated files can conservatively invalidate a runtime/dependency fingerprint;
the implementation does not claim optimal invalidation or provider latency
improvement. The only performance evidence in this stage is the count of
actually executed/reused synthetic independent checks.

tests/test_incremental_verification.py exercises the actual Runner/Workflow
routes and foreground temporary-repository CLI completion. The bounded local
gate additionally imports the unchanged external Verification TestCase and
checks all 53 frozen test/fixture hashes. It writes evidence only inside this
candidate. Original acceptance.py writes outside the worker sandbox and is
reserved for the scheduler's original host argv. Remaining full regression,
metrics/benchmark, reproducible bundle, migration and actual Codex profile
qualification are separate full-task conditions; this stage cannot complete
them or activate any runtime.
