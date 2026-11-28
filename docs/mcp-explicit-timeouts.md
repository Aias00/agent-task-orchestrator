# Explicit MCP task parameters — implementation, verification pending

Task `task-860fa8b8db25472c8fe86a2edcd680a3`, based on
`41cd0b091820953371e04356393c18d6d8efb8d8`, 2026-11-27.
Implementation-only work in the isolated worktree. No tests, compilation, lint,
build, dependency installation, harness, server/native smoke or provider exercises
were run. Manual acceptance remains pending. Installed packages, running service,
client configuration and production stores were not changed. No deployment,
restart, merge or push was performed.

## Change and rationale

- `ato/server/spec_schema.py` describes the nested inline task contract with
  runtime enums/ranges, optional defaults and command/manual acceptance shapes.
  MCP validate/submit require explicit worker timeout 5..7200 and enabled reviewer
  timeout 5..3600. REST/OpenAPI share the field descriptions but retain optional
  timeouts/defaults. The schema module adds no third-party runtime dependency.
- Service validation enforces the MCP rule for raw calls over both transports.
  Missing/invalid timeouts produce static field-specific error codes/messages.
  Submit checks happen in journal `prepare`, after historical replay/collision
  lookup and before a new durable intent, admission or process spawn. No journal
  format/digest or frozen task spec is changed.
- Validation previews effective worker kind, sandbox, timeout/source and enabled
  reviewer timeout/source, replacing the host repository root with its alias.
  It starts no worker. Existing submit/resume/status execution receipts remain.
- CLI/REST defaults, legacy execution, worker adapters, retries, STOP and hard
  timeouts remain in place. Resume without an override uses the frozen spec
  timeout, including its original default, not a prior run override.
- Distributed controller instructions, examples, API documentation and optional
  harness inputs now supply/teach explicit MCP values. A file read by a worker
  cannot change its outer execution deadline; MCP network timeout is separate.

## Compatibility limits

Clients must rediscover tools/reconnect after a schema upgrade. New MCP omission
is deliberately a breaking input change. Old exact principal/key/arguments
retries still reach cached outcomes or unresolved reconciliation on the server;
adding a field to a retry changes its digest and conflicts. Client-side schema
enforcement may block a legacy retry before delivery. Retain its intent and task
ID; use authorized host reconciliation, including exact shared REST-journal
replay when already authorized. Do not regenerate keys or edit historic records.
See [recovery details](mcp-api.md#idempotency-and-recovery).

Schema character limits do not replace runtime UTF-8 byte limits or Git/path
checks. JSON Schema may consider `120.0` an integer; the service requires the JSON
integer representation `120`, without coercion. Normalization remains
authoritative for semantic constraints. The
compatibility-only `memory` field remains ignored, not a new loading facility.
Validation receipt fields are additive; consumers enforcing an exact old set
must update. No claim is made about current Grok/client cache behavior, production
transport, native workers or deployed service availability.

The earlier [API/SDK](mcp-api-validation.md), [controller](mcp-controller-validation.md)
and [native Codex](codex-mcp-validation.md) reports retain their historical scope.
They do not qualify this patch.

## Tests to run only after explicit instruction

Using an already prepared Python 3.12 environment (optional transport dependencies
must already be available; installing them requires separate authorization):

```sh
python3.12 scripts/run_tests.py
python3.12 scripts/run_tests.py --server
python3.12 scripts/check_server_transport.py
```

The first command covers dependency-free service/schema/journal tests plus the
existing CLI/default/timeout/STOP regressions. The second additionally covers
actual SDK tools/list, raw HTTP-MCP and stdio calls with synthetic child dispatch.
The harness covers a real loopback socket and CLI stdio using temporary stores;
it never invokes a provider. No native/provider qualification command is required
or authorized by this implementation task.

Pending regression coverage:

- Complete nested discovery, actual enums/bounds and MCP vs OpenAPI requirements.
- Omitted/null/bool/string/float/out-of-range worker/reviewer timeout rejection
  before normalization, intent persistence, task/worktree creation or dispatch;
  rejected keys remain usable for corrected new input.
- Valid boundaries, optional/null reviewer, effective receipts, alias-only preview,
  existing required fields/path restrictions and no validation side effects.
- HTTP-MCP/stdio discovery/error/receipt parity and normal explicit submit/replay.
- Legacy successful/unresolved replay across transports and service reconstruction,
  unchanged journal rows/digests, collision, reauthorization and alias rebinding.
- REST/CLI defaults and frozen-spec resume behavior after a run override; existing
  legacy/retry/STOP tests remain intact.

All acceptance criteria are manual and remain pending until separately reviewed
and tested. Implementation completion is not verified runtime success.
