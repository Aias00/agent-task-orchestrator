# Source and capability map

The unified public entry is [ato](../README.md#quick-start). The selected
sealed source provides the public CLI, private runtime and the internal
Workflow kernel.

| Responsibility | Source |
|---|---|
| Standard-library primitives, shared public exception and compatibility exports | `ato_common/ato_core.py`; re-exported by `runner.py` and `recovery.py` |
| Public launcher, command parsing and composition | `bin/ato`, `ato.py`, `ato_cli/__init__.py`, `ato_task/ato_application.py` |
| Task/Run ledger, admission, ownership and accepted product retention | `ato_task/ato_task_store.py`, `ato_task/ato_execution.py`, `ato_task/ato_execution_host.py`, `ato_task/ato_product.py`, `ato_task/ato_process.py` |
| Enrolled credentials, bindings, answers, acceptance and observation | `ato_task/ato_host.py`, `ato_task/ato_binding.py`, `ato_task/ato_acceptance.py`, `ato_task/ato_observer.py`, `ato_task/ato_watchdog.py`, `ato_task/ato_private.py`, `ato_runtime.py` |
| Complete workflow, stage execution and independent checks | `ato_workflow/workflow.py`, `runner.py`, `ato_workflow/permissions.py` |
| Bounded recovery, host actions and accumulated time | `recovery.py`, `ato_workflow/execution_state.py`, `ato_workflow/host_recheck.py`, `ato_workflow/host_budget.py`, `ato_workflow/budget.py` |
| Durable task observation, questions and disposition | `ato_workflow/control.py`, `ato_workflow/interaction.py`, `ato_workflow/workflow_closure.py`, `ato_workflow/failure_attention.py` |
| Passive controller ledger and notification-policy projections | `ato_workflow/control_store.py`, `ato_workflow/notification_state.py` |
| Agent registry and scoped session routing | `agent_registry.py`, `ato_workflow/session_router.py` |
| Built-in worker registration and CLI subprocesses | `ato_adapters/agents.py` |
| Codex and local CLI sessions, Codex daemon transport and native event parsing | `ato_adapters/codex_session_adapter.py`, `ato_adapters/local_session_adapter.py`, `ato_adapters/appserver.py`, `ato_adapters/agent_native_events.py` |
| Durable messaging, turn correlation and traces | `ato_workflow/message_bus.py`, `ato_workflow/transport.py`, `ato_adapters/acp_bridge.py`, `ato_workflow/task_trace.py` |
| ATO/native state-root separation | `ato_common/ato_config.py` |
| Managed Pi coordinator and bounded Pi file worker | `ato_pi/pi_session_coordinator.py`, `ato_pi/pi_session_profile.py`, `ato_pi/pi_acp_worker.py` |
| Trusted Pi/Node resolution, reviewed release fingerprints and protected native launch | `ato_pi/pi_toolchain.py`, `integrations/pi-acp/compatibility.json` |
| Verified pi-acp backend, named transformations and atomic private adapter publication | `ato_pi/pi_acp_compat.py`, `scripts/derive_pi_acp.py` |
| Explicit opt-in production Pi profile and read-only credentials (offline candidate) | `ato_pi/pi_production_profile.py`, `ato_pi/pi_production_credentials.py` |
| Per-action existing-owner budget fence, conservative request reservations | `ato_pi/pi_production_fence.py`, `pi-coordinator-guard.mjs` |
| Owned CONNECT exit, bounded custom HTTPS/SSE transport and native group anchor | `ato_pi/pi_production_egress.py`, `pi_production_transport.mjs`, `ato_pi/pi_production_exec.py` |
| Compact private host/failure material and once-only completion policy | `coordination.py`, `ato_workflow/failure_notice.py`, `ato_workflow/completion_notifications.py`, `ato_workflow/notifications.py`, `hub_queue_gate.py` |
| Snapshot qualification, read-only inspection and journaled cutover/rollback | `scripts/release_snapshot.py`, `ato_release/support.py`, `ato_release/seal.py` |
| Source installation, trusted operator administration and conversation integration | `scripts/install.py`, `scripts/ato_host_setup.py`, `skills/ato/SKILL.md` |
| Neutral read-only hub contracts, OS boundary, worktree integrity and private session ledger (offline candidate) | `ato_review/hub_contract.py`, `ato_review/hub_sandbox.py`, `ato_review/hub_worktree.py`, `ato_review/hub_ledger.py` |
| Code-owned thin AGY/Claude/Codex reviewers and explicit capability registry | `ato_adapters/hub_adapter_*.py`, `ato_adapters/hub_registry.py` |
| Internal H1 work/check/review/fix loop and guarded Workflow bridge | `ato_review/hub_loop.py` |
| Historical internal qualification tools and records | `scripts/history/`, `docs/history/` |

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
