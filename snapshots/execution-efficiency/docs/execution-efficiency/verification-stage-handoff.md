# Verification stage host handoff

Assigned scope: actual Runner/Workflow host check reuse for unchanged trusted
complete input fingerprints. Implementation is verification_cache.py with
Runner.checks, Workflow.check_total/stage inheritance, host_recheck policy
matching and incremental checkpoint provenance integration. The candidate is
inactive. No external source/check/baseline, installed dependency, scheduler
state, historical task, live hub, native config/history, global launcher or
deployed runtime was changed. No commit, push, install or actual model/native
CLI call was made.

The exact policy and conservative offline Python dependency scope are described
in incremental-verification.md. Unsupported commands are uncached and retain
their original argv. Final/full integration and host/external/manual checks are
fresh. Missing evidence, malformed witnesses, dependency drift, changed files or
modes, links, unknown authority and expiry cannot supply a cached pass.

Local evidence lives in evidence/verification-stage-tests.json and
evidence/verification-stage-final-qualified.log, produced by the foreground bounded
evidence/verification-stage-verify.py. It includes the actual Runner counter,
environment, script, final-freshness and missing-log cases imported read-only
from the frozen host_behaviors.py. It is worker fixture evidence, not the
independent original host acceptance. The final bounded gate passed 186 tests
with zero failures, errors or skips: 37 new verification tests, the five frozen
external Runner cases, and 144 related compatibility/fault tests. All 53
protected predecessor test/fixture files match the external baseline hashes.
The initial new-suite failure log is
retained in evidence/verification-initial.log; the corrected implementation
refuses reuse for in-memory authority drift, while the original execution
guard continues to reject persisted frozen-contract drift. Two inherited
detached-launch tests are omitted from this bounded foreground gate and remain
mandatory in the later full host regression.

Foreground CLI fixture tests supply synthetic kernel start identities inside
their harness because the worker sandbox denies ps. The production cache has
no unknown-owner fallback. That fixture identity is not a native CLI/provider
qualification; unknown real execution-owner identity disables cache reuse.

The scheduler must execute the original frozen stage acceptance argv outside
the implementation worker sandbox:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py verification
```

That command has not been run by this worker: it writes the frozen external
operations log directory, which is outside the authorized writable roots. This
is ready_for_checks, not a claim that the host command passed.

After all subsequent stages, the origin must inspect the exact final candidate
and run every original acceptance mode. The only authorized actual native
sample remains origin-owned:

```text
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py host_sample --run-origin
```

Its script is scripts/verify_execution_efficiency_local.py --output PATH
--existing-profile --max-turns 12. The explicit handoff authorizes only one
bounded existing-Codex/profile comparison: foreground, legacy ATO, warm ATO;
two examples per arm and two turns per example, at most 12 native turns and
1200 seconds, temporary Git/file tasks only, same configured model/auth and
permission profile. Do not read/copy auth, mutate native configuration, add
services or production/business effects, or automatically resample after
failure/unknown. Successful proof must bind the final candidate manifest.

Passing this stage does not complete the total task. Remaining full-task
conditions include accurate spans/counters and all three benchmark arms;
fresh complete compatibility/fault discover >=759 with no failure/error/skip;
the inactive reproducible source archive/manifest and extracted validation;
architecture, migration, validation and operator rollback/handoff documents;
and the origin's actual default Codex native session/profile proof. Existing
Linger677/2256534 and prior runtime/responsibility/worker/checkpoint receipts
retain their original scope and do not qualify these newly changed bytes.
