# Agent abstraction compatibility and installation

Existing plans retain their chosen `agent.kind`, frozen contracts, legacy JSON
layout, attempt/elapsed history and STOP semantics. Registered session references
are explicit local bindings, not replacements for arbitrary native session IDs.
A read-only query does not migrate records or prove another agent's identity.

Run `python scripts/run_tests.py` after installing the optional test dependencies
listed in the README. Tests create isolated temporary repositories and config
homes. They do not prove provider authentication, model availability or native
coordinator compatibility. The fixture adapter demonstrates an extension point;
Claude/Pi coordinator and hub capabilities remain unavailable.

Use a durable versioned checkout for an approved installation. Preserve the old
launcher, controller configuration, unresolved receipts and all workflow data.
Review live owner/process identities, STOP, queued/unknown delivery and pending
answers before stopping only the authorized controller and replacing its entry.
Starting from a new source checkout grants no new task permissions or recovery
windows. See [runtime cutover](recovery-escalation-migration.md).
