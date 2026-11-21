# Three CLI communication stage

This is the historical native-adapter stage record. Subsequent workflow integration
and changed-source validation are recorded in `message-integration-validation.md`.

Source: `message_adapters.py`, `scripts/verify_native_mailbox.py`,
`tests/test_message_adapters.py` and its explicit protocol process fixture.
The frozen whole-goal contract remains `docs/agent-neutral-mailbox-task.md`.
This stage does not activate a runtime or complete that contract.

Installed interfaces inspected on 2026-11-15: Claude Code 2.1.325 `--help` and
`--version`; Pi 1.5.2 `--help`, installed `docs/rpc.md`, `docs/rpc-commands.md`,
`docs/models.md` and model configuration reference; Codex `queue --help`.
Read-only CLI help/version checks use isolated native config directories. An
initial Pi help command without isolation was denied when it tried to create its
default trust lock; all subsequent Pi invocations are isolated.

## Independent host continuation

The external stable host re-ran native qualification after the worker reported
its socket-bind restriction. Both installed CLIs passed two correlated turns in
the same native session against a loopback-only provider. After removing HOME
replacement and retaining explicit Claude/Pi config/cache/session isolation, the
host re-ran the probe successfully again. Source/runtime identities and retained
receipts are recorded in `build/message-transport/host-validation.json` and
`native-host-report.json`; those are fixture evidence, not cloud authentication.
All 14 adapter behavior tests passed again after this change, without skips.

The worker failures below remain valid observations of its restricted
environment. They do not override the host pass. A code-ready worker should
return its structured stage result and let the runner execute the original host
checks; it should not repeatedly bind sockets or nest sandbox-exec inside its
own sandbox. Whole-goal mixed-origin integration and delivery remain unfinished.

Adapter acceptance:

```sh
OPERATOR_HOME/.local/bin/python3.12 .codex/tasks/agent-neutral-mailbox-preparation/accept.py adapters
```

The latest adapter acceptance run passed all 14 tests in 4.35 seconds, without
skips. Process tests cover two correlated turns without process replacement, exact
transcript resume, missing-transcript refusal, exclusive ownership, identity and
project mismatch, duplicate receipts, late cancelled replies, timeout/crash/
malformed records/wrong acknowledgements, failed turns, process cleanup,
environment isolation and restricted tools, and Codex exact queue receipts with
the legacy executable override. These are protocol fixture tests, not native CLI
qualification. Neither source nor assertions of the frozen acceptance are edited.

Mandatory native acceptance:

```sh
OPERATOR_HOME/.local/bin/python3.12 .codex/tasks/agent-neutral-mailbox-preparation/accept.py native
# Or retain its report/evidence in a chosen temporary output location:
OPERATOR_HOME/.local/bin/python3.12 scripts/verify_native_mailbox.py --output /tmp/ctask-native/report.json
```

The worker attempted this interface, but the environment denied
`ThreadingHTTPServer(('127.0.0.1', 0), ...)` at `socket.bind` with PermissionError
before either native CLI launched. No native pass, same-session proof or cloud
authentication is claimed. Run this command from the external stable host where
the authorized loopback fixture can bind; inspect any native protocol failures
before passing the stage. The probe emits failures and exits nonzero rather than
skipping, weakening acceptance or requesting broader worker permissions.

The assigned-stage retry reproduced this restriction with both the public probe
and the frozen `accept.py native` command. Its retained failure report is
`/tmp/ctask-native-stage/report.json`: both kinds failed before native startup,
so there are no native request/reply receipts to inspect. The same retry passed
all 14 adapter tests in 3.982 seconds. Read-only isolated help/version inspection
again confirmed Claude 2.1.325, Pi 1.5.2 and Codex 0.165.0; installed Pi RPC and
CLI documentation confirms prompt disposition, settled-event handling and
same-ID session continuation. These interface checks do not substitute for the
blocked two-turn qualification. Reassigning the same probe to this restricted
worker cannot supply the missing host evidence; the stable host must execute it
where the authorized local fixture can bind. No acceptance or task ledger was
changed.

The broader current suite was attempted once at the earlier 12-adapter-test
checkpoint: 151 cases, 130 passed, 9 failures and 12 errors in 80.64 seconds;
log `/tmp/ctask-adapter-tests.log`. Eleven delegation errors were denied Unix
socket binds. The ten legacy agent tests failed before their fixture work could
run; an independent read-only check confirmed
`sandbox-exec: sandbox_apply: Operation not permitted` (exit 71) in this worker.
These legacy files were not changed. The final two adapter tests were then added
and the complete 14-case adapter module passed. No full-suite pass is claimed;
the host still needs to run the retained baseline and current full suite.
Python compilation and `git diff --check` passed.

Remaining whole-goal work includes routing legacy core callers through adapters,
non-Codex hub/origin and recovery/controller integration tests, preserving all
legacy authorization/budgets/history in migration/restart, installation/rollback
tooling, independently retained baseline tests, complete final suite and release
handoff. Do not modify the stable runtime, real hub data or scheduler-owned task
ledger to complete this stage.
