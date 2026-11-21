# Source and capability map

The stable source baseline is exported from the deployed compact-handoff
controller, including the compatible CLI runtime. Publication does not switch
installed launchers, migrate task records or grant execution permissions.

| Responsibility | Source |
|---|---|
| Standard-library primitives, shared public exception and compatibility exports | `ato_core.py`; re-exported by `runner.py` and `recovery.py` |
| Argument surface and domain CLI dispatch | `ato_cli/parser.py`, `ato_cli/*_cmd.py`; `runner.main()` remains the entrypoint |
| Complete workflow, stage execution and independent checks | `workflow.py`, `runner.py`, `permissions.py` |
| Bounded recovery, host actions and accumulated time | `recovery.py`, `execution_state.py`, `host_recheck.py`, `host_budget.py`, `budget.py` |
| Durable task observation, questions and disposition | `control.py`, `interaction.py`, `workflow_closure.py`, `failure_attention.py` |
| Passive controller ledger and notification-policy projections | `control_store.py`, `notification_state.py` |
| Registered adapters, scoped actors and session routing | `agent_registry.py`, `session_router.py`, `local_session_adapter.py`, `codex_session_adapter.py` |
| Durable messaging, turn correlation and traces | `message_bus.py`, `transport.py`, `acp_bridge.py`, `task_trace.py` |
| ATO/native state-root separation | `ato_config.py` |
| Managed Pi coordinator and bounded Pi file worker | `pi_session_coordinator.py`, `pi_session_profile.py`, `pi_acp_worker.py` |
| Compact private host material and once-only completion policy | `coordination.py`, `completion_notifications.py`, `notifications.py`, `hub_queue_gate.py` |
| CLI entrypoints and optional conversation integration | `bin/`, `scripts/install.py`, `skills/codex-long-task/` |

The structural source refactor preserves the protected core filenames and public
CLI. Passive stores add no independent ledger, delivery path or authorization;
they read/write the same existing tables and files. The document index is in
[README.md](README.md). Project metadata does not replace checkout installation
or activate an installed runtime.

An adapter being installed is distinct from authenticated readiness, native
continuation support, real-provider qualification and full task acceptance.
The legacy Codex adapter can use verified existing native conversations. The
neutral local CLI uses explicit scoped actors. Managed Pi coordination and
file workers have different capability/grant boundaries; native Pi resume is
not implicitly supplied. Optional production Pi profiles remain macOS-specific
and version-pinned. Claude worker availability does not establish a generic
Claude coordinator transport. AGY/acpx work is an unfinished development
snapshot, not part of this stable baseline.

Operator records belong outside Git. `operator-evidence/` is ignored and only
used when explicitly supplied to historical qualification utilities. Offline
fixtures use synthetic identities and a minimal sanitized Pi error example;
they cannot recreate a cloud result or an original user's approval.
