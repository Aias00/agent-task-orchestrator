# Checkpoint stage handoff

This implements only the assigned incremental checkpoint / bounded closing /
hard failure and history retention stage of task.md. It preserves the complete
objective and does not activate, install, deploy, publish, commit or migrate any
runtime or task. See incremental-checkpoints.md for policy, proof format and
bounds. The source changes are incremental_checkpoint.py, Runner/Workflow
integration, worker-session reconstruction/timeout propagation and host-recheck
policy matching. Legacy plans remain explicit legacy behavior.

Validation uses only bounded foreground temporary-repository fixtures. The new
tests/test_incremental_checkpoint.py exercises real Runner and Workflow paths,
atomic crashes before/after record write, partial JSON, forged/replayed/stale and
expired receipts, file/mode/deletion/link drift, original host evidence and input
invalidation, hard timeout, malformed outcome, cleanup failure, STOP, bounded
closing, no-progress accounting, exact native continuation and explicit new-ID
reconstruction. The native CLI in those tests is an explicit synthetic protocol
fixture with a temporary index/rollout. Synthetic kernel start identities are
supplied only by the test harness because this worker sandbox denies ps; the
production implementation has no fallback and refuses unknown owner identity.
No actual native CLI or provider call was made.

The bounded local verifier is evidence/checkpoint-stage-verify.py. Its JSON
report names the intentionally omitted inherited detached-launch tests; they
remain required for the later full host regression. It compares all 53 protected
test/fixture byte hashes with the external frozen baseline. Earlier failed logs
are retained. Fixture results are not independent host qualification, provider
qualification, a speedup claim or full-task completion.

The scheduler must run this original argv outside the worker sandbox. The worker
did not run acceptance.py because it writes the frozen external operations log
folder, which is outside the authorized worker write roots:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py checkpoint
```

The origin must inspect the exact final candidate after all remaining stages and
run every original acceptance mode. The separately authorized actual native
sample is origin-owned only:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py host_sample --run-origin
```

That action is the single bounded existing-Codex/profile comparison: foreground,
legacy ATO and warm ATO; two examples per arm, two turns per example; at most 12
native turns and 1200 seconds; temporary Git/file tasks only. It must retain the
same current model/auth/permission profile without reading/copying auth, changing
native configuration, production/business effects, extra services or automatic
additional samples after failure/unknown. Its script remains
scripts/verify_execution_efficiency_local.py --output PATH --existing-profile
--max-turns 12; preparing that full benchmark belongs to the later metrics stage.
A success receipt must bind the exact final candidate manifest.

Remaining full-task conditions include the separate verification-cache stage,
accurate spans/counters and all three benchmark arms, fresh complete compatibility
and fault regression (>=759, no failure/error/skip), inactive reproducible source
archive/manifest with extraction validation, complete architecture/migration/
validation/operator handoff and rollback, and that origin actual native proof.
Predecessor runtime/responsibility/worker and Linger 677 evidence retains its
original scope and bytes; it does not qualify these changed source files.
