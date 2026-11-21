# Integrated inactive execution-efficiency candidate

The full contract is [task.md](task.md). This is a portable local source candidate,
not the installed runtime. Its exact qualified predecessor is the completed
Linger workflow `20261119-032425-7f6af654`, 677 tests, candidate manifest
`2256534fb9f58ec277f18511b5662479a8da82aa0433a58657a121b200bc02f6`.
The frozen external baseline and predecessor manifest are copied as provenance;
that predecessor's acceptance does not qualify changed candidate bytes.

## Execution and compatibility

`runtime_selection.py` records the code-owned runner, actual interpreter, source
and dependency manifest, compatibility version, project, frozen contract,
generation and actor at trusted creation. A private receipt outside model write
roots witnesses the selection. CLI create selects a binding; Workflow stages,
planning, execution, host checks and resumed bootstrap inherit it. `load(directory)`
is read only. Another default entry point routes to the task's selected runtime;
source/dependency drift, altered records or bad project/actor/generation fail
closed. No plan supplies imports, commands or a replacement runtime. Existing
unbound JSON remains explicit legacy behavior without conversion or rebinding.
The independent global launcher can change for new work; it is not a task's
immutable version. The separate authorized session-decoupling launcher transition
is neither blocked nor activated by this candidate. See [runtime details](runtime-binding.md).

`task_responsibility.py`, existing `control.py`, recovery and
`failure_attention.py` distinguish live execution, bounded task claims, host
work, external waiting and unresolved disposition. An active/unknown ordinary
origin session is observational only. Claims bind the exact workflow, project,
contract, role, generation, verified actor and finite next action/expiry. A
heartbeat or diagnostic response cannot resolve a business failure. Stable
semantic failure episodes include real failed attempts/checks and exclude elapsed
time. New records compare episodes; old incomparable checksum drift stays unknown
and auditable. Presented/claimed or unknown deliveries are never replayed.
Stale-notice predicates and reason codes explain why a notice became stale without
silently closing its blocked task. [Responsibility evidence](responsibility-checkpoint.md)
describes the task-specific disposition paths.

## Worker, progress and checks

The agent registry owns vendor capabilities. Explicit
`worker_session_policy={version:1,mode:"task_bound_native"}` routes the actual
Runner through `worker_sessions.py` and the Codex adapter. Private host receipts
bind task/project/contract/runtime/profile/owner and the exact native ID. Each
turn has a unique correlated message, native turn, append-only rollout prefix
and verified effective model/permissions. Resume is `exec resume EXACT_ID`, never
`--last`. New tasks cannot adopt origin/hub/user sessions. Every CLI turn starts
a new OS process; warm means native context reuse. Unsupported adapters and
unknown cleanup/delivery remain fail closed. Explicit v2
`checkpoint_reconstruction` is a new native session with preserved ancestors,
not native resume. See [worker sessions](worker-sessions.md).

`checkpoint_policy.version=1` requires v2 classified accounting and a bounded
filesystem grant. `incremental_checkpoint.py` atomically publishes private,
sequence-checked records: full objective, safe file hashes/modes, current step,
original host check observations, native identity/turn, policy/runtime and all
invocation/failure/continue/wait history. Restoration verifies actual files,
receipts, age, cleanup, permission and original evidence. Running observations,
text summaries and heartbeats do not authorize progress. Closing-margin prompts
reserve time before hard timeout; an unsupported checkpoint steer is never sent.
Malformed output and hard failure retain unknown side effects and real failure
counts. See [checkpoint rules](incremental-checkpoints.md).

`verification_policy.version=1` maps actual task/criterion IDs to explicit
`inputs`, environment names and `cacheable`. `verification_cache.py` is called
by actual Runner checks and Workflow overall checks. Complete trusted fingerprints
include argv/interpreter/scripts/toolchain/dependencies, content and modes,
contract/runtime/environment and permission boundary. Evidence must still exist
at its original hash and time; only successful host executions qualify. Missing,
unsafe, linked, unknown or incomplete inputs rerun unchanged argv. Live external,
manual, acknowledgement and health gates never cache. Final integration forces
fresh checks. See [cache policy](incremental-verification.md).

## Measurement and final proof

`execution_metrics.py` adds spans to existing task trace, with parent relations
and exclusive interval union, preserving v2 ledgers. Queue, warmup, worker unknown,
restart, independent check and checkpoint are distinct; whole-process wall is
never called inference. Model/tool latency and request/read counts remain unknown
unless actually measured. `status`/`task-summary` retain full objective, stages,
process ownership, independent evidence, unresolved checks and responsibility.
See [measurement limits](metrics-and-benchmark.md).

The offline benchmark runs identical synthetic two-turn file tasks through direct,
legacy cold and warm Runner paths. The separate origin executor
`scripts/execution_efficiency_actual.py` prepares two samples per arm, two turns
each, at most twelve turns and 1200 seconds, with the existing native home,
configured model and identical narrow grant. Each sample uses a separate temporary
Git repo and a pinned Workflow/stage; only exact newly returned native IDs are
read. Direct resumes one ID, legacy creates two IDs, and warm uses Runner's exact
receipt/checkpoint path. Fresh Workflow checks accept every arm. Successful file
operations and native model/policy/turn evidence are mandatory. A per-candidate
exclusive claim and retained journal prohibit automatic replay after any uncertain
sample. No model call is made by packaging, importing, describing or offline tests.

Packaging includes source, tests, docs, modes and frozen provenance, excluding
auth, task state, dependencies and private native history. `candidate.json` and
USTAR/gzip bytes are deterministic; extraction checks work without the original
host. Runtime activation and overall acceptance remain false until separately
qualified; this delivery never activates itself.
