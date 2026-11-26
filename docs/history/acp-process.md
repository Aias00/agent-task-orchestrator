# ACP process candidate

The candidate registers `agent.kind: "acp_pi"` through the existing trusted
adapter registry. A plan can select the registered kind and an optional model
identifier; it cannot supply a command, environment, module path, or native
receipt. Other registered adapters remain the default for their existing plans.
The optional Python ACP SDK is imported only when this profile actually runs.

Each attempt gets a new private runtime directory and a fresh ACP session. The
adapter validates the pinned pi-acp source, derives a local copy, and starts it
under the existing macOS sandbox policy. The fixed Pi launcher offers only the
existing file-tool allowlist and path guard. Read-only planning omits mutation
tools. Native state and adapter output live under the attempt directory; the
profile does not grant writes to the user's Pi configuration directory. A
missing SDK, pinned package, sandbox, or native terminal receipt causes an
explicit failure or an unverified outcome. The scheduler does not retry an
uncertain delivery inside the bridge.

The bridge accepts structured native receipts only from this code-owned
profile, for the current ACP session and a new turn nonce. Native errors are
sticky even if ACP reports `end_turn`; the latter is transport completion, not
task completion. A schema-valid worker result is written only after a verified
native stop and transport completion. The runner independently executes the
immutable host checks before it can mark the task completed. A manual user
acceptance criterion remains a separate user action; neither the status nor
model output satisfies it.

Query a saved run without starting an agent:

```sh
python runner.py trace /absolute/path/to/run --json
python runner.py trace /absolute/path/to/run --task example --json
python runner.py status /absolute/path/to/run
```

The bounded SQLite timeline records enumerated kinds, source, time, order,
attempt and hashed session/turn/tool IDs. Three updates to the same tool ID
count as one call. Token chunks coalesce into one progress event. It does not
persist model text, thoughts, raw stderr, tool arguments, or credentials in
the trace; scheduler-owned artifacts have their existing independent policies.
Trace write failures are best effort and do not decide the verdict. Missing
traces are explicitly unavailable. Event timestamps are not process-liveness
proof. Observability adds no notifications or polling daemon.

This is an isolated candidate, not a deployed or live-validated route. Its
fixture evidence and the recorded Pi evidence are distinct from a new live
provider test, which was not authorized or run. See the migration and demo
documents for constraints and reproducibility.
