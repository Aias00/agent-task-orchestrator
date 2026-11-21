# Worker permissions and normal host handoffs

Legacy contracts keep their existing `workspace-write` / `read-only` policy.
No live plan, authorization, acceptance, elapsed budget, STOP or recovery count
is rewritten by this update. Permissions belong to the frozen task contract,
not to model-generated stages or automatic recovery. This update does not
activate the separate agent-neutral mailbox upgrade.

## Give a Codex worker its required development capabilities

```json
{
  "sandbox": "workspace-write",
  "permissions": {
    "source": "The actual instruction authorizing this task's local development and testing",
    "network_access": true,
    "writable_roots": ["/absolute/existing/task-cache"],
    "requirements": ["loopback", "unix_socket"]
  }
}
```

These are optional fields on a stage plan or total workflow. Omit unneeded
roots; they must already exist. Codex receives explicit network and writable-root
overrides. A read-only planner does not inherit these worker overrides. Stage
presets and generated plans cannot supply different grants from the workflow.
Claude/Pi file-worker adapters reject these new grants rather than silently
pretending they have Codex's permissions.

`workspace-write` with network access permits local TCP in the tested macOS
environment, but **does not make Chromium work**: its Mach bootstrap registration
is still denied. Browser tasks can instead declare an origin-owned host handoff,
or deliberately choose full access for a trusted task:

```json
{
  "sandbox": "danger-full-access",
  "permissions": {
    "source": "Actual authorization for this task to use full local execution access",
    "requirements": ["loopback", "unix_socket", "browser"]
  }
}
```

Full access is not scoped filesystem/network isolation. It cannot be combined
with fields claiming a narrower write or network boundary. Business operations,
deployment, messaging and final human decisions still follow the original task
authorization. Do not switch all tasks to full access or interpret a worker's
request as the permission source.

Before a model attempt, declared requirements are checked with the model-free
native `codex sandbox --permission-profile` command. The CLI's sandbox subcommand
uses a named profile representing the requested workspace/network/extra roots;
this is reported as `native-sandbox-profile`, not as a claim to have observed a
future `codex exec` process or every administrator constraint. The exact worker
argv remains in its execution log. Validate critical new setups with a real
worker too. Unsupported native sandbox CLI, missing browsers or denied actions
produce `permission_blocked` without spending a model attempt; no automatic
permission widening occurs.

```sh
ctask permission-check --repo /project --plan /project/contract-input.json
```

Supported probes: `loopback` (bind/listen/connect), `unix_socket` (local bind),
`browser` (installed Playwright Chromium launches and opens a page). Additional
write roots are tested with a temporary file and immediate cleanup. Probes do not
install browsers or dependencies, touch production, or change CLI/global config.

## Separate implementation readiness from host acceptance

The structured worker result accepts two additional states:

* `ready_for_checks`: implementation is ready; run the existing independent
  acceptance commands in the host runner. A failed check still prevents completion
  and returns its actual evidence. Known worker-only socket limitations are not
  grounds to skip the host checks.
* `needs_host`: an origin-owned action remains. This is accepted only with an
  explicit frozen `host_handoff` contract; otherwise it becomes an ordinary
  blocker. The worker must describe the remaining action, not claim it ran.

```json
{
  "host_handoff": {
    "source": "Actual prior user authorization for the full task and its host delivery",
    "instructions": "Origin runs the approved browser checks and reversible asset-only delivery; preserve the backend and request final user visual acceptance."
  }
}
```

With an already configured controller and verified origin, `needs_host` produces
one durable normal-handoff notification to that origin. It does not consume,
renew or enable repair supervision, restart a worker, clear elapsed time, or add
business authority. Unknown delivery is not blindly resent. STOP, paused native
origins and changed bindings remain fences. If origin is the hub, only the hub
followup is needed. Hub followups remain pending until real progress is observed.

After performing the **already authorized** host action, origin/operator runs:

```sh
ctask host-checks /absolute/workflow --source 'Actual host work and evidence source'
```

This foreground command obtains the repository lock, checks STOP and contract
integrity, charges execution against the same remaining cumulative workflow
budget, and runs the original stage/final checks. It does not launch a model,
deploy anything itself or acknowledge manual criteria. Failing checks keep the
task unmet; a final manual condition still becomes `needs_input`. A new business
decision/permission/budget must be routed through the existing hub. Do not use
this command to simulate a user answer.

Old contracts that lack these fields are continued by their origin within their
existing host authorization. Permission changes require an explicit linked
continuation preserving old acceptance/history/budget, never an edit to frozen
JSON. Existing result-schema files are not silently rewritten.

## Validation

`tests/test_permissions_handoff.py` covers grants, read-only planner isolation,
unchanged legacy defaults, no model calls after a failed capability probe,
independent checks after readiness, failed acceptance, bounded host handoff,
manual/STOP fences, cumulative time, once-only routing and unknown delivery.
Real local probes and a native Codex full-access fixture are recorded under
`build/permissions-handoff/`; their artifacts do not constitute production or UX
acceptance. No Git commit/push is part of this task.
