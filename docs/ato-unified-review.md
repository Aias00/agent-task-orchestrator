# ATO unified offline review handoff (T7)

The candidate covers the eight daily commands on one public `ato` launcher and
keeps one Task ID through the task lifecycle. Human approval of this source
candidate and the coordinator’s independent T7 gate are pending.
[Approved T0–T7 plan](ato-unification-task-plan.md) · [Public contract](ato-unified-contract.md)

## Source under review

| Stage | Integration commit | Evidence and focus |
| --- | --- | --- |
| T0 | `aeba9ff` | Four review fixes retained as ancestors. The T0 record has the exact source revisions, four regression groups and the original 887-test host run. [Baseline](ato-unified-baseline.md) |
| T1 | `c2cbb4c` | Frozen gate passed for the contract, old command map and links. [Contract](ato-unified-contract.md) |
| T2 | `7674a29` | 16 passed for Task and Run records, repository scope, concurrent and interrupted provisioning, read-only queries and index rebuilding. [Task store tests](../tests/test_ato_unified_task_store.py) |
| T3 | `2e74128` | 14 passed for launch, first work on a green baseline, PID reuse, STOP, independent checks, technical review and bounded retry. [Execution notes](ato-unified-execution.md) · [Execution tests](../tests/test_ato_unified_execution.py) |
| T4 | `75d08b0` | 10 passed for inbox visibility, exact user events, version-bound acceptance and stale, unknown and duplicate decisions. [Interaction and acceptance tests](../tests/test_ato_unified_acceptance.py) |
| T5 | `6ea4ecd` | 11 passed for scoped handoff, live Run ownership, independent Reviewer selection and stale grants. [Handoff tests](../tests/test_ato_unified_handoff.py) |
| T6 | `4777dd1` | 8 passed for the public CLI and journaled release with synthetic process fixtures. [Public CLI and release tests](../tests/test_ato_unified_cli.py) · [Release regression tests](../tests/test_release_snapshot.py) |
| T7 | Working tree for coordinator commit | Installed subprocess tests and this review handoff. [End-to-end tests](../tests/test_ato_unification_e2e.py) |

## Installed CLI cases

The new suite installs `ato` from a sealed fixture copy. Each case creates its
own temporary bin, Skill directory, ATO and native homes as needed. Tasks use
temporary Git repositories and local scripted Codex Worker and Reviewer
executables. The tests exercise the production launcher, normal host profile
and Run subprocess. Trusted fixture setup enrolls the local actors and uses
the controller API to create a question. The public commands record the
synthetic user's explicit answer and exact product acceptance.
[Host setup](ato-host-setup.md) · [Release procedure](runtime-upgrade.md)

| Case | Assertions |
| --- | --- |
| Fresh install and command surface | One `ato` and one Skill, idempotent install, help for the eight daily commands and every management subcommand, typed rejection of retired or private verbs, private invocation fences, and read-only diagnostics without creating state. |
| Two repositories, one ID per Task | Exact repository filter and scope isolation, Worker runs despite a green baseline, product stays in the bound worktree, technical review and independent check receipts precede user acceptance, private Run source matches the installed seal, and public output omits fixture credentials and native session data. |
| Question and exact acceptance | Controller and cross-project answers are refused. The same user answer is idempotent and a conflicting answer is refused. The product awaits separate explicit acceptance. After a product edit and recheck, the old acceptance is invalidated and the revised product requires a new decision. |
| Live handoff and STOP/resume | The authorized transfer keeps the active PID and Run, hands the same pending request to the new interaction endpoint, and removes old route visibility. STOP verifies the driver, Worker and child are gone. Resume uses the same Task ID, creates a new Run and keeps the prior attempt count. |
| Unconfirmed outcome | The local Worker makes one attempt and exits without a result. The public status preserves uncertainty and explicit resume is fenced without another attempt. |
| Upgrade and rollback | Synthetic old launchers with distinct modes are replaced by the selected sealed `ato`. Two rollbacks restore the old bytes, modes and absence of `ato`. The original namespace, new Task ledger and worktree product survive. |

## Observed checks and remaining qualification

The worker used macOS arm64, Python 3.12.15 and Node 24.22.1. All six Python pins
matched `requirements-acp.lock`, and the installed `pi-acp` version `0.0.34`
matched the npm lock. Dependencies were already present. Test commands used
local fixtures and isolated temporary state.

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_ato_unification_e2e.py' -v
.venv/bin/python scripts/run_tests.py
.venv/bin/python scripts/check_architecture.py
```

- The four T0 review fixes are ancestors of the candidate. Ten targeted T0
  regressions passed in the worker. The 59 tests in the frozen T2–T6 gates
  had zero failures, errors or skips.
- All 185 Python sources compiled. The eight protected core modules and both
  baseline fixtures match `86f19d9` byte-for-byte. The frozen core AST and
  vendor import checks passed. The architecture scan found eight mutual
  dependency pairs among 74 root modules and eight across all 75 runtime
  modules. Every pair is in the frozen baseline.
- The 18 public command, source layout, distribution metadata, document
  inventory and architecture tests passed. The launch surface has one bin
  entry and one Skill. The repository-wide Markdown link test passed.
  The installed surface test passed through the sealed launcher in the worker.
- The frozen T7 command reached the new suite in the worker. The four other
  cases require macOS Seatbelt or the release tool's process inventory.
  The worker observed `sandbox_apply: Operation not permitted` and denial of
  `/bin/ps`. The frozen command stopped at this suite, before its full-suite
  step. This run leaves the independent T7 gate pending.
- A separate full offline run completed 925 tests in 300.529 seconds. The
  runner reported 47 failures, 42 errors and zero skips. Failing groups
  included Seatbelt, process identity, loopback and Unix socket fixtures.
  This run used a short temporary root after a deeper one exceeded the
  historical compact handoff fixture's notice limit. That fixture passed on
  its own and in the full rerun with the short root.
- All five installed CLI cases passed in a diagnostic run that replaced
  Seatbelt application and process enumeration in a disposable snapshot.
  This exercised the CLI, subprocess and ledger assertions. It leaves
  those two OS boundaries for independent host verification.

The initial T7 host attempt and stage recheck each passed the five installed
CLI cases and all 925 offline tests. The overall recheck passed the five CLI
cases but reported one error among 925 tests. During STOP followed by resume,
the parent could open the task record just before the child atomically
replaced it, then receive `BUSY` while waiting for the startup receipt.
The parent now retries that observation within the existing startup deadline.
The deterministic regression checks that the same Run reaches acceptance
with exactly one Worker attempt. The original workflow will re-run the
frozen T7 gate against the corrected candidate. The final gate requires zero
failures, errors and skips, followed by human review. The installed cutover case has no running
observer. Release regressions cover observer restoration and startup errors
with synthetic process identities. Actual agent/provider qualification,
historical imports, publication and live activation remain outside this review.
