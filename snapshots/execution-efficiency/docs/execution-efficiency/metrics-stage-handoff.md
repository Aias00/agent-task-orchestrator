# Metrics stage handoff

This stage implements observational phase timing and the same-contract offline
three-arm comparison. It preserves the full task.md objective and is inactive.
It does not complete the overall execution-efficiency task. No live task/hub,
auth/config/history, global launcher, installed runtime/dependency, external
baseline/check definitions or scheduler-owned .codex/tasks were changed. No
model/native CLI call, service/supervisor, detached job, install, commit, push or
deployment was performed. All process tests use bounded foreground temporary
Git/file fixtures. All 53 protected inherited test/fixture bytes must still match
the frozen external baseline.

Implementation and semantics are in metrics-and-benchmark.md. The core is
execution_metrics.py, integrated with Runner.command/worker_command/checks/
capacity admission, worker-session eligibility, checkpoint publication,
task_trace, status and task-summary. Query-only joins validate workflow stage
bindings before joining raw spans, partition parent/child/overlap time once,
and keep v2 ledger command/wait sums separate. Unknown native model/tool latency
stays unknown. Launched check counts require an actual observed Popen return;
STOP and pre-launch errors cannot inflate execution counts.

The new suite is tests/test_execution_efficiency_metrics.py. Its bounded local
gate is evidence/metrics-stage-verify.py, which combines metrics, inherited
trace/task-summary, worker/runtime/checkpoint/cache fault checks, foreground
Runner/Workflow tests and the five frozen external Runner cache regressions.
It deliberately omits two inherited detached-launch tests under the worker
boundary; they remain mandatory in the origin's complete regression. The final source-frozen gate passed 241 tests (including 31 metrics
tests), with zero failures, errors, skips, source drift or protected-byte drift.
Exact results and protected-byte checks are recorded in evidence/metrics-stage-tests.json.
Earlier gate and fixture receipts remain with their original scope; none is an
independent original acceptance.py run or actual native qualification.

Final offline comparison receipts are evidence/metrics-fixture-qualified.json
and its log. They bind the exact synthetic executable and implementation hashes,
equal initial file bytes/modes, task/check/model/permission contracts, exact
synthetic session/turn identities, final equal artifact hashes, actual command
returncodes/log digests, real check-counter observations and cache provenance.
Every arm has two turns and two CLI process startups. Direct and warm use one
native-shaped context with two exact turns; legacy uses two. Legacy performs two
cold initializations and six independent check subprocesses; warm performs one
cold initialization and four checks, reusing two unchanged successful receipts.
Every arm executes its final check fresh. These are scripted counts only;
provider performance and speedup remain unknown. CLI process reuse is not
claimed.

The scheduler must run this exact original frozen host gate, outside the
implementation worker sandbox:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py metrics
```

This worker did not run that argv because the fixed harness writes its external
operations log directory. Local fixture checks do not substitute for it. The
assigned stage is ready_for_checks when the final checkpoint's local report is
successful; this is not a claim that the original host command passed.

## Remaining full-task conditions

scripts/verify_execution_efficiency_local.py currently prepares the strict
origin-only actual-proof schema and validator, as assigned to this stage. It
contains no actual-model executor. Its reserved native invocation fails closed
with actual_cli_qualification_not_run and zero native turns. The later
integration/delivery stage must implement and review that executor before any
origin model call; schema validation is not qualification. Do not run the
reserved command now or interpret a zero-turn failure as permission to resample.

After all integration and candidate packaging work is complete, the only
explicitly authorized actual sample remains:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py host_sample --run-origin
```

The harness invokes the final reviewed script as:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python scripts/verify_execution_efficiency_local.py --output PATH --existing-profile --max-turns 12
```

Only the origin may perform that one bounded existing-Codex/profile comparison:
foreground, legacy ATO and warm ATO; two examples per arm and two turns per
example; at most 12 native turns and 1200 seconds; temporary Git/small-file tasks
only. Use the same current native model/auth/permission profile, no reading or
copying auth contents, native configuration mutation, wider grants, business/
production effects or new service. Preserve exact task-bound identity and
claimed/unknown no-replay/cleanup fences. Retain failed/unknown evidence for
specific origin review; do not automatically try another sample/window.
Successful actual proof must bind the exact final candidate manifest. A fixture
or predecessor proof cannot qualify changed final bytes.

Other full-task requirements remain: all routes integrated and compatibility/
fault qualified; fresh full discover >=759 with zero failure/error/skip;
portable reproducible inactive candidate.tar.gz/candidate.json with exact
baseline/predecessor/source/tests/docs/modes; independent --check and extracted
validation; architecture, migration, rollback, validation and operator handoff;
and the origin's original acceptance modes plus actual Codex qualification.
Old attempts/history/STOP/grants and legacy IDs/schema semantics remain intact.
No global activation or Git publication is authorized.
