# Persistent-message-core stage evidence

This is a stage record. It does not close the full acceptance in
[`agent-neutral-mailbox-task.md`](agent-neutral-mailbox-task.md).

Validation in the restricted scheduler worker on 2026-11-15:

| Command/check | Observed result |
| --- | --- |
| `OPERATOR_HOME/.local/bin/python3.12 .codex/tasks/agent-neutral-mailbox-preparation/accept.py bus` | 19 tests passed, no skips; final run 0.624 seconds |
| `OPERATOR_HOME/.local/bin/python3.12 -m unittest discover -s tests -p 'test_*.py' -v` | 139 tests, 77.507 seconds; 118 passed, 9 failures and 12 errors |
| `git diff --check` | Passed |
| `/usr/bin/sandbox-exec -p '(version 1)(allow default)' /usr/bin/true` | Exit 71, `sandbox-exec: sandbox_apply: Operation not permitted` |

All 19 new bus cases passed in the broader run too. Its failures were confined
to existing `test_agents.py` and `test_delegation.py`. Delegation fixture setup
failed binding an `AF_UNIX` socket with `PermissionError: Operation not permitted`
(11 errors). Adapter fixtures failed before their fake native CLI could run;
a targeted legacy `AdapterTests` fixture reproduced `blocked` and its private
adapter stderr contained `sandbox-exec: sandbox_apply: Operation not permitted`.
That accounts for the 9 adapter failures and its missing result-metadata error.
No legacy source or baseline test assertion was changed to bypass these limits.
Full baseline/native acceptance must still run in an appropriate host environment.

The final bus run additionally checks native receipt identity retention when
receiver acceptance wins the delivery race, and resolved routing source in the
public message snapshot. Changes after the broad run were confined to bus
evidence handling and its focused assertions; focused acceptance passed again.

Implemented interfaces: `message_bus.MessageBus`, `EndpointCredential`, mailbox
errors, `transport.ManagedTransport`, `DeliveryResult`, `TransportEvent`,
`dispatch_one` and `receive_event`. Protocol and adapter responsibilities are
documented in [message-protocol.md](message-protocol.md) and
[agent-transports.md](agent-transports.md).

No production deployment, native user config/auth, actual hub state, external
stable runtime or scheduler-owned task definitions were modified. Adapter
implementations, mixed hub/origin integration, real native two-turn local-provider
qualification, migration/rollback tooling and final handoff remain subsequent
stages. All original full-goal criteria, authority limits and budget history
remain in force.
