# Worker-stage handoff

This is only the assigned native worker-session stage of task.md. It is not
full-task delivery and does not activate a runtime or feature in any live task.
The candidate integrates the default Codex adapter with actual Runner and
Workflow paths through worker_session_policy version 1. Legacy plans and their
historical schemas, IDs, STOP behavior and counters remain unchanged.

Implementation is in worker_sessions.py, codex_worker_session.py and the small
integration changes in agents.py, runner.py, workflow.py and host_recheck.py.
See worker-sessions.md for the protocol and failure barriers. The code records
one exact native session per task and unique correlated native turns. It does
not claim persistent CLI process reuse. EOF/turn completion never substitutes
for artifact checks or total workflow acceptance. Native stdout and stderr are
separate evidence files. Unknown delivery and cleanup remain quarantined.

The local verifier is evidence/worker-stage-verify.py. It runs only bounded
foreground temporary-repository fixtures, including the added worker-session
suite and selected inherited compatibility tests. The two inherited detached
runner/resume tests are omitted from that worker subset and are named in its
JSON report; they must still run in the eventual fresh full regression. All 53
inherited test/fixture bytes are compared with the external baseline. Existing
failed test logs remain available. The fixture harness explicitly supplies
synthetic kernel process-start identities. Production code has no such
fallback: /bin/ps is denied in this worker sandbox and unknown owner identity
refuses execution. The fixture suite also tests that refusal directly.

The scheduler must run this exact original stage acceptance argv outside the
worker sandbox (the command writes logs to the external frozen operations
folder, which the implementation worker may not modify):

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py worker
```

No actual Codex model or provider turn was run by this worker. Reading installed
CLI help established syntax only. Native source/turn/profile formats that do
not satisfy the code-owned parser remain unqualified and fail closed. Actual
qualification remains with the origin after it reviews the exact final
candidate and all later stages produce their required files and manifest:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py host_sample --run-origin
```

That is the existing explicitly authorized single bounded sample: foreground,
legacy ATO and warm ATO; two examples per arm and two turns per example; at most
12 native turns and 1200 seconds; temporary Git/file tasks only, same current
model/profile/permissions and native authentication. Do not read/copy auth,
mutate native configuration, create a service, publish or deploy. Preserve
failure/unknown evidence and do not automatically add another sample. The later
benchmark script remains scripts/verify_execution_efficiency_local.py with
--output PATH --existing-profile --max-turns 12. It is outside this worker stage.

Full-task remaining requirements include incremental verified checkpoints,
input-fingerprinted independent-check reuse, accurate timing and all three
benchmark arms, fresh complete compatibility/fault regression, reproducible
inactive candidate archive/manifest, extraction checks, migration/rollback and
that final origin-owned native qualification. Runtime and responsibility-stage
results remain their own predecessor evidence; this stage does not replace or
relabel them. No speedup or full delivery is claimed from protocol fixtures.
