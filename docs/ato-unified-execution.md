# Unified execution service (T3)

[Approved task plan](ato-unification-task-plan.md) · [Unified contract](ato-unified-contract.md)

## Launch and recovery

Use `ExecutionService(store, profile, authorize=..., hooks=...)`.
`start(actor, task_id, foreground=False)`, `resume(actor, task_id,
foreground=False)` and `recheck(actor, task_id, checks_only=False,
foreground=False)` admit the same private driver. `request_stop(actor,
task_id)` writes a durable STOP epoch and mirrors it into the Workflow.
`task_status` and `list_tasks` retain the store's observation grant checks.
The execution authorization callback receives the authenticated principal,
`TaskScope`, current binding, and one of `initial`, `resume`, `recheck`,
`stop`, or `reconcile`. It must check the operation grant against that
binding and return literal `True`.

The canonical Git common directory selects one orchestration flock across
linked worktrees. The driver inherits the locked descriptor. The original
worktree flock and kernel mailbox lease authorize Workflow, host checks,
and technical review. Kernel-only intervals reacquire that same lease
under the repository gate. A durable Run launch intent pins the source
snapshot, generation, host profile, STOP epoch and binding revision. The
private child verifies an inherited one-use capability and publishes its
process birth identity and the kernel fence before dispatch. Background
returns after a verified startup or settlement receipt. Foreground waits
on that child. [Execution service](../ato_execution.py) · [Private bootstrap](../ato_runtime.py)

On STOP the driver terminates its verified process groups and records
cleanup. Run admission after STOP retires only the matching epoch and kernel
mirror. Review and independent checks retain the original Workflow and its
attempt and elapsed history. A missing startup or terminal result stays
fenced. The trusted `reconcile(actor, task_id)` operation can publish an
already prepared terminal receipt for the same Run after verifying its
owner exited, the group is absent, and the exact kernel generation and
lease still match. It starts no work. Without that correlated receipt,
PID absence or reuse still leaves the Run unresolved.
[Process identity](../ato_process.py)

The default unified elapsed time policy is advisory. The first launch pins
the verified time policy and subsequent Runs reject drift. Individual
command and review deadlines, stage and attempt limits, STOP and review
rounds still apply. Tech review uses the H1 tripwire and structured
verdict. A pass yields `needs_acceptance` with content-addressed check
receipts and a product manifest. Product and evidence are reverified
before use. [Product evidence](../ato_product.py) · [H1 implementation](h1-offline-implementation.md)

## Shared calls for T4 and T5

Use the following methods and callbacks through trusted composition. Task
and binding values passed into callbacks are read-only inputs. Each returned
snapshot is detached from stored state.

| Interface | Integration |
| --- | --- |
| `task_snapshot(task_id)` | Returns Task and spec IDs, canonical repository key, revision, effective STOP epoch, Run/Workflow identity, full generation, redacted execution activity and current qualified product. This is for internal callers with an already verified scope. |
| `transaction(task_id, expected_revision, expected_stop_epoch, expected_binding_revision, reviewer_change=False)` | Short comparison and publication boundary. Yields the verified Task directory descriptor, Task record and binding snapshot. Publish the leaf's committed pointer inside the context. A successful transaction advances Task revision. Use `reviewer_change=True` to contest review admission under the same lock. The driver publishes review activity and pins its review revision under that lock. |
| `product_transaction(task_id)` | Acquires repo and worktree admission, then measures current product and verifies all check and review references. Yields the Task snapshot for acceptance. Keep the gate held through the short `transaction` that publishes the matching human receipt. |
| `ExecutionHooks.binding(task, spec)` | T5 returns `{task_id, revision, controller_ref, interaction_ref, reviewer, review_revision}`. The selected agent must match the frozen eligible kinds. Route changes preserve actual Run ownership. |
| `ExecutionHooks.pending(task, binding)` | T4 returns redacted request summaries already projected for the current binding. The composition layer applies actor-specific presentation and delivery policy. |
| `ExecutionHooks.decision_status(task, binding)` | T4 returns `waiting`, `needs_input` or `delivery_unknown` for a current material decision barrier, or `None`. An ordinary notification receipt does not block a product. |
| `ExecutionHooks.acceptance(task, product)` | T4 returns the disposition, criteria and receipt ID of the verified human acceptance for that exact product and check set, or `None`. Only a current recorded disposition projects completion. |
| `ExecutionHooks.decision_barrier(task, binding, operation)` | T4 revalidates scoped blocking requests and material delivery uncertainty at admission and child startup. Return `None` to allow or a typed service error code for a barrier. |
| `ExecutionHooks.claim_continuation(task, binding, run, generation)` | The exact admitted resume or host-check child calls this under its kernel lease. T4 records consumption for that Run and returns the immutable evidence hash, or `None` when no decision is required. The driver retains the reference before work. |
| `ExecutionHooks.host_handoff(task, binding, run, generation)` | For a pending frozen host action, T4 returns a `HostHandoffProof` for the previous Run and generation. Explicit recheck then completes only the original checks and eligible review. The proof records the already performed action. |

T6 wires the same verified binding and decision readers into the application
and `ato_runtime.runtime_hooks(store)` in the selected sealed source. The
child bootstrap accepts identifiers plus a capability over an inherited
pipe. T6 supplies the host profile through trusted configuration and keeps
it outside the public spec and ambient actor environment.

## Offline scope

The included bounded Worker profile handles Codex in workspace-write or
read-only mode with one host-selected native permissions profile. The Codex
frontend starts directly and its command tools use native Seatbelt. The
profile gives writes only to the selected workspace and per-turn scratch,
fences Git and Task records, and keeps command-tool network disabled.
Independent host checks use their own outer Seatbelt. Other Worker protocols and broader grants require their own
verified write boundary. The H1 reviewer selection uses its existing
read-only profiles. `resume_if_supported` starts fresh for the supplied
one-shot reviews. `require_resume` refuses admission because the profile
does not establish and reconcile a persistent Reviewer session.

The targeted tests use temporary repos and local synthetic executables.
The synthetic profile has a constructor-only test seam, independent
checks, the production process identity path and the H1 integrity
tripwire. The host suite covers real local sandbox and native fixture
mechanisms. Provider and real session qualification require separate
evidence. [T3 execution tests](../tests/test_ato_unified_execution.py)
