# Phase timing and controlled offline comparison

This is the assigned metrics stage of the inactive execution-efficiency
candidate. The complete objective and authority remain in task.md. This stage
does not qualify an actual provider, activate a runtime, or finish the complete
workflow. The completed Linger677 predecessor remains historical evidence for
its original bytes only.

## Measurement model

execution_metrics.py stores bounded version-1 spans in the existing
trace.sqlite3. Existing task_trace events, deduplication, v2 invocation/failure
ledgers and their accounting semantics remain unchanged. Every new span has a
unique ID, same-trace parent, enumerated phase, wall anchor and monotonic elapsed
duration. No prompts, argv, tokens, config contents or native transcript bodies
are copied into spans. Exceptional exits close observational spans as raised;
this never means the operation, delivery or cleanup succeeded. A hard crash
leaves an open span with unknown duration. SQLite commits each start/terminal
record and rejects replacement of an already closed span. Span count is bounded
at 4096 per trace; overflow is explicit and never complete telemetry.

The actual Runner.command path measures independent_check, worker_unknown or
orchestration_unknown. A native child process's duration includes model,
tools, protocol, polling and cleanup; it is never labelled inference time.
restart measures the OS Popen call only, not a guessed native startup latency.
Runner.worker_command and worker-session eligibility/receipt preparation measure
warmup, with child command spans. Capacity admission measures queue. Actual
incremental checkpoint publication measures checkpoint, including publication
failures. Cache hits record verification_reuse, with no new check execution
span. Check attempts and launched executions are separate: only an observed
Popen return marks process_started. STOP/guard refusals and missing executables
never count as launched checks; open/crashed launch status remains unknown.
Original check evidence remains at its original location/time.

The query partitions the union of closed intervals. A child excludes its time
from its ancestors. Overlapping sibling intervals with differing phases count
once as concurrent_unknown; same-phase overlaps count once. Parent cycles,
missing parents, children outside parent bounds, invalid/nonfinite intervals,
open spans and overflow are visible qualification gaps. These observations
never supply scheduler or acceptance authority. Wall-clock discontinuities that
break nesting remain invalid/incomplete rather than becoming invented latency.

phases_seconds reports unobserved phases as null. In particular, model_request
and tool latency stay unknown without explicit measured spans from a qualified
producer. Existing normalized tool events can count observed tools, but cannot
supply missing start/end latency. model_request_count is likewise unknown
without explicit request observations. No installed CLI help/version implies
model timing or request count evidence.

covered_seconds is the sum of the exclusive phase partition, not the sum of
span durations. observed_wall_seconds is the envelope between the first/last
closed span. uninstrumented_gap_seconds is the difference. This is an observed
trace window, not a claim to account for the entire task lifetime. Existing
state elapsed_seconds remains the executor's accumulated wall record. The
separate v2_ledger projection retains active_seconds (settled command sums),
wait_seconds/yield_seconds and unsettled wait projection. These have different
scopes and are explicitly nonadditive with spans. In particular, ledger waits
between stopped foreground invocations do not become a fabricated monotonic
external_wait span. Their recorded duration remains visible in the v2 ledger;
external_wait phase latency remains null when no span measured it. Queue wait
is measured inside actual admission and is also part of the older aggregate
wait ledger, never added a second time.

status, trace and task-summary expose phase_timing. Task-filtered trace
queries include only spans attributed to that validated task ID. Workflow task views join
only verified recorded stage bindings and partition their raw spans together;
they do not sum workflow and stage totals. The original process/owner,
responsibility, canonical questions, full objective, stage-only completion,
remaining checks and delivery-unknown barriers retain their existing meaning.
Queries copy bounded SQLite/WAL observations to private temporary storage and
never initialize a trace, native session, capacity store or execution ledger.

## Reproducible controlled fixture

Run only the synthetic fixture experiment:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python scripts/benchmark_execution_efficiency_fixture.py --output /tmp/efficiency-fixture.json
```

It starts no sockets/services/schedulers and invokes no actual Codex/Pi CLI or
model. All child processes are foreground synthetic CLI/check processes in
bounded temporary Git repositories. Synthetic kernel identities exist only in
the harness; no production unknown-owner fallback is added. The executable is
tests/fixtures/execution_efficiency_cli.py and every output is labelled fixture.

The arms are foreground direct adapter, legacy cold Runner, and opt-in warm
Runner. All start with byte/mode-identical guide.txt, artifact.txt and check.py;
the same complete two-increment objective; explicit workspace-write, never
approval, no network or extra writable roots; fixture-model-v1 protocol with
identical scripted delay; and exactly the same independent check argv. Each arm
executes two actual synthetic protocol turns and independently validates native
session, project, turn, message, effective model and permissions. Cold legacy
identities are additionally verified by the harness without pretending legacy
production mode acquired native-resume qualification.

The direct arm uses the explicit adapter with an exact native ID for its second
turn; legacy uses two cold IDs; warm uses the actual Runner/worker_sessions
path with one verified native ID and two distinct correlated turns. CLI process
startup count remains two in every arm: warm context reuse is not a persistent
process. All produce byte-equal final artifacts. Preflight and turn checks run
in all arms, then each requests two unchanged probes and a fresh final check.
Only warm opts into the existing verified input-fingerprint cache. Every final
check really executes; no worker answer accepts the artifact.

Receipts retain seed hashes/modes, task/model/protocol/permission contract,
exact synthetic identities/turns, argv hashes, script and implementation hashes,
actual model-request count in the scripted protocol, actual cold initialization
and read counters/bytes, prompt byte count, check counter versus trace count,
returncode/log digests, original reuse provenance, exclusive timing, active/wait
and total wall. Temporary native-shaped records and raw prompts are deleted.
The measured scripted cold guide read can be avoided by exact context reuse;
it does not prove a real provider's read/context caching. Prompt bytes are
observable input volume, not inferred provider context tokens.

No fixture result implies actual provider throughput, price, reasoning speed or
latency improvement. provider_performance and provider_speedup remain unknown.
Warm can measure fewer repeated initializations/check executions while showing
no provider speedup. Real-world wall results may be noisy or slower.

## Actual proof schema and remaining origin work

scripts/verify_execution_efficiency_local.py prepares the actual proof contract
and validator for the later origin-owned executor. It rejects fixture proofs,
unknown assertions, altered profiles/contracts, replayed turns/messages,
identity sharing, missing fresh host checks, unequal artifacts and >1200-second
samples. The contract requires three arms, two samples per arm and two turns per
sample, exactly 12 native turns. It records effective model, exact binary/profile
and final candidate manifest hashes and retained native evidence digests.

This stage deliberately does not implement or run the actual-model executor.
The reserved --output PATH --existing-profile --max-turns 12 invocation fails
closed with actual_cli_qualification_not_run and zero turns until the later
integration/delivery stage supplies that executor. The validator alone is not
native qualification and cannot make frozen host_sample acceptance pass. The
origin must not run or resample it before reviewing that completed integration.
The full-task handoff must preserve this explicit remaining condition.
