# Source and capability map

The unified public entry is [ato](../README.md#quick-start). The selected
sealed source provides the public CLI, private runtime and the internal
Workflow kernel.

| Responsibility | Source |
|---|---|
| Standard-library primitives, shared public exception and compatibility exports | `ato_core.py`; re-exported by `runner.py` and `recovery.py` |
| Public launcher, command parsing and composition | `bin/ato`, `ato.py`, `ato_cli/__init__.py`, `ato_application.py` |
| Task/Run ledger, admission, ownership and accepted product retention | `ato_task_store.py`, `ato_execution.py`, `ato_execution_host.py`, `ato_product.py`, `ato_process.py` |
| Enrolled credentials, bindings, answers, acceptance and observation | `ato_host.py`, `ato_binding.py`, `ato_acceptance.py`, `ato_observer.py`, `ato_private.py`, `ato_runtime.py` |
| Complete workflow, stage execution and independent checks | `workflow.py`, `runner.py`, `permissions.py` |
| Bounded recovery, host actions and accumulated time | `recovery.py`, `execution_state.py`, `host_recheck.py`, `host_budget.py`, `budget.py` |
| Durable task observation, questions and disposition | `control.py`, `interaction.py`, `workflow_closure.py`, `failure_attention.py` |
| Passive controller ledger and notification-policy projections | `control_store.py`, `notification_state.py` |
| Registered adapters, scoped actors and session routing | `agent_registry.py`, `session_router.py`, `local_session_adapter.py`, `codex_session_adapter.py` |
| Durable messaging, turn correlation and traces | `message_bus.py`, `transport.py`, `acp_bridge.py`, `task_trace.py` |
| ATO/native state-root separation | `ato_config.py` |
| Managed Pi coordinator and bounded Pi file worker | `pi_session_coordinator.py`, `pi_session_profile.py`, `pi_acp_worker.py` |
| Trusted Pi/Node resolution, reviewed release fingerprints and protected native launch | `pi_toolchain.py`, `integrations/pi-acp/compatibility.json` |
| Verified pi-acp backend, named transformations and atomic private adapter publication | `pi_acp_compat.py`, `scripts/derive_pi_acp.py` |
| Explicit opt-in production Pi profile and read-only credentials (offline candidate) | `pi_production_profile.py`, `pi_production_credentials.py` |
| Per-action existing-owner budget fence, conservative request reservations | `pi_production_fence.py`, `pi-coordinator-guard.mjs` |
| Owned CONNECT exit, bounded custom HTTPS/SSE transport and native group anchor | `pi_production_egress.py`, `pi_production_transport.mjs`, `pi_production_exec.py` |
| Compact private host/failure material and once-only completion policy | `coordination.py`, `failure_notice.py`, `completion_notifications.py`, `notifications.py`, `hub_queue_gate.py` |
| Snapshot qualification, read-only inspection and journaled cutover/rollback | `scripts/release_snapshot.py`, `ato_release_lib.py`, `ato_seal.py` |
| Source installation, trusted operator administration and conversation integration | `scripts/install.py`, `scripts/ato_host_setup.py`, `skills/ato/SKILL.md` |
| Neutral read-only hub contracts, OS boundary, worktree integrity and private session ledger (offline candidate) | `hub_contract.py`, `hub_sandbox.py`, `hub_worktree.py`, `hub_ledger.py` |
| Code-owned thin AGY/Claude/Codex reviewers and explicit capability registry | `hub_adapter_agy.py`, `hub_adapter_claude.py`, `hub_adapter_codex.py`, `hub_registry.py` |
| Internal H1 work/check/review/fix loop and guarded Workflow bridge | `hub_loop.py` |

The eight protected core modules retain their original implementation and
existing Workflow and mailbox records. Unified services bind those records to
the selected Task, Run and enrolled actor. Project metadata distributes the
launcher, Node guards and integration manifests. The sealed source installer
selects the runtime. [Documentation index](README.md)

An adapter being installed is distinct from authenticated readiness, native
continuation support, real-provider qualification and full task acceptance.
The legacy Codex adapter can use verified existing native conversations. The
neutral local CLI uses explicit scoped actors. Managed Pi coordination and
file workers have different capability/grant boundaries; native Pi resume is
not implicitly supplied. Optional production Pi profiles remain macOS-specific
and version-pinned. Claude worker availability does not establish a generic
Claude coordinator transport. The old AGY/acpx integration remains a historical
snapshot. The separate [H1 offline candidate](h1-offline-implementation.md) uses
native thin reviewers and no acpx dependency; it has not activated a runtime.

W2-1 production Pi is an explicitly registered `pi_acp_production` offline
candidate. It neither changes `LocalPiProfile` nor adopts any existing task.
It requires an exact sealed source snapshot, host-reviewed provider/model/address
policy, an explicit permit callback and an accounted existing workflow owner.
There is no default reviewed cloud provider. Each request reserves its entire
reviewed worst-case cost; missing usage never refunds the reservation. The v1
text/OpenAI-compatible request path bounds serialized input plus a framing
reserve conservatively, so oversized contexts fail before network admission.
Native load/resume and production checkpoint reconstruction remain unsupported.
Offline qualification does not authorize cloud calls or installed-runtime
activation. See the [design](w2-1-production-pi-design.md) and
[pinned prerequisite spike](w2-1-spike.md).

Operator records belong outside Git. `operator-evidence/` is ignored and only
used when explicitly supplied to historical qualification utilities. Offline
fixtures use synthetic identities and a minimal sanitized Pi error example;
they cannot recreate a cloud result or an original user's approval.
