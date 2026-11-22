# Worker action ownership

The worker prompt previously prohibited deployment unconditionally, even for a
stage explicitly assigned an already authorized deployment. Without a frozen
`host_handoff`, the same stage could not hand the action to its origin either.
An outcome carrying a real blocker as `continue` then ran unchanged acceptance
again and replaced its useful reason with a generic check failure.

New plans can declare action ownership in `execution_policy`, on a standalone
plan or the total workflow contract. The policy records existing authorization;
it does not obtain authorization, replace the sandbox, or permit other business
actions. Do not infer ownership from a task name, natural-language keyword,
full-access sandbox, a worker request or a planner response.

```json
{
  "sandbox": "danger-full-access",
  "permissions": {
    "source": "The actual user instruction permitting this bounded deployment"
  },
  "execution_policy": {
    "source": "The same verified deployment authorization and its evidence",
    "instructions": "Deploy only the reviewed candidate to the named existing service; preserve its unrelated configuration and rollback evidence",
    "actions": {"deploy": "worker"},
    "required_actions": ["deploy"]
  }
}
```

Known actions are `deploy`, `git_commit`, `git_push` and `send_messages`. Each
selects `worker`, `origin` or `deny`; omitted actions are denied. A worker action
requires the trusted adapter's existing `explicit_permissions` capability, a
full-access profile and an explicit permissions source. Current file-only
adapters cannot claim this capability. The action policy is instruction and
admission control, not command-level OS enforcement or a new sandbox.

For an origin-owned deployment, select `"deploy": "origin"` and provide the
existing bounded `host_handoff`. The worker prepares materials and returns
`needs_host`; it does not deploy. Any origin-owned policy without a valid handoff,
or required action without a permitted owner, fails validation before a model
attempt. `required_actions` is explicit task metadata, not a prose classifier.
Unannotated legacy prose cannot be semantically checked for every contradiction.

Workflow stages inherit the frozen policy and cannot introduce, broaden or
replace it, including dynamically generated stages. Planners remain read-only.
Policies do not alter final acceptance, publication gates, actor authentication,
STOP, cumulative history, attempt limits or unknown-delivery handling.

An outcome reporting a nonempty blocker with `continue`, `complete` or
`ready_for_checks` now settles as `blocked` once. The original blocker and next
step are retained in task state and an event; the runner does not run acceptance
or spend another attempt for that outcome. A normal `continue` with an empty
blocker and a bounded external `waiting` keep their existing behavior. Removing
the cause does not silently resume the task or renew its exhausted attempts.

Legacy contracts retain the original worker action restrictions; activating
this runtime does not edit their contracts or grant new worker authority. Their
origin can perform already authorized delivery itself. An explicit linked
continuation is needed to change action ownership, retaining original acceptance,
STOP and cumulative history. Existing `host-checks` still requires the original
host-handoff contract; do not invent one in frozen JSON.

Offline qualification is in `tests/test_execution_policy.py`: an actual local
fixture delivery followed by independent acceptance, working origin handoff,
pre-admission rejection, stage escalation rejection, unchanged legacy policy,
STOP, and a blocked outcome that cannot trigger checks or repeated attempts.
It makes no real provider, remote deployment or business acceptance claim.
