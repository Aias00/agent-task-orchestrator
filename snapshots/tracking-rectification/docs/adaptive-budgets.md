# Task-based execution budgets

## User-selected advisory execution time

When the user requires uninterrupted work within an already authorized goal,
record the instruction once as a machine-local policy:

```sh
ctask budget-policy set --mode advisory --source 'actual user instruction' --request stable-policy-id
ctask budget-policy status
```

This applies to existing fixed/adaptive tasks and future tasks on this machine.
Their frozen contracts, original estimates, charged time, failure history and
STOP files remain unchanged. `status.budget.mode` becomes `advisory`,
`enforcement` identifies the effective policy, `remaining_seconds` is `null`
(there is no elapsed-time ceiling), and `exhausted` is false. Original soft/hard
values remain forecast references; `forecast_exceeded` reports elapsed overrun.
The sourced policy has an idempotent, checksum-chained history outside task state.
Only the configured hub may change it when a hub is bound.

The workflow, standalone runner, child stage and `budget-run` continue after
their historical runtime estimate. No periodic user time renewal is required.
Reforecasting remains useful for reporting and can exceed the old estimate.
The controller sends deduplicated forecast warnings to origin, including an
overrun notice, and supersedes old exhaustion/reforecast alerts without claiming
that the task actually resumed. An overrun does not launch extra repairs or
create a new supervision window. Actual progress remains independently verified.

STOP, changed contracts, repository leases, per-command hang timeouts, bounded
attempts/stages/planning and repeated-failure protections still apply. No new
production permission or human acceptance is granted. A user stop never becomes
an automatic resume. Do not create external absolute-budget `ctask stop` guards
under advisory mode. For an already stopped task, origin must identify the stop
source and use the supported continuation process; changing policy itself does
not remove STOP or start a process. No wall-clock estimate is a token/cost quota,
and this policy does not bypass provider quotas or actual missing credentials.

The absence of a local policy preserves existing installations. Explicit
`budget-policy set --mode enforced` restores the bounded behavior documented
below, with a new user source; installation alone does not alter policy.

## Optional enforced envelopes

An adaptive envelope separates an expected active allocation (`soft`) from a
task-specific maximum (`hard`). Both include charged work. Reserve release and
origin reforecasting within that maximum need no new user approval. New business
permissions, human acceptance and work beyond the maximum remain real decisions.
These limits measure elapsed execution time, not tokens or money.

## Evidence-based remaining work

Estimate implementation, required reruns, host verification, authorized delivery
and recovery from actual timings. Reuse unchanged checks. Exclude human/device
waiting. Separate measurements from uncertainty in `basis`.

```json
{
  "items": [
    {
      "id": "receipt-audit",
      "description": "Audit the two uncertain execution receipts",
      "seconds": 600,
      "upper_seconds": 1200,
      "basis": "Prior host timings; upper includes one uncertain-result investigation",
      "evidence": ["/absolute/path/to/actual-audit-evidence.json"]
    },
    {
      "id": "verify",
      "description": "Required original final checks",
      "seconds": 300,
      "upper_seconds": 600,
      "basis": "Latest same-source checks; reuse unchanged results",
      "evidence": ["/absolute/path/to/actual-check-results.json"]
    }
  ],
  "risk_percent": 25,
  "max_extensions": 2
}
```

These numbers illustrate a schema, not a universal task budget.
`ctask budget-estimate --estimate estimate.json` computes expected remaining time
and upper time plus the risk margin without changing any task. Evidence files
must exist when installing the policy.

For a new workflow include `budget: {"source": "actual instruction authorizing
this bounded adaptive envelope", "estimate": <object above>}`. For an existing
task explicitly adopt the policy:

```sh
ctask budget-configure WORKFLOW --estimate estimate.json \
  --source 'actual instruction and task-specific envelope basis' --request stable-adoption-id
```

Soft becomes at least used time plus expected remaining work; hard becomes at
least used time plus the uncertain upper estimate. Already granted limits are
retained. No frozen contract, acceptance, STOP, permission, recovery count or
used time is edited. Existing fixed tasks stay fixed until explicit adoption.
Installing code alone grants no additional time; configure does not start work.

## Automatic reserve and origin review

The runner deterministically releases an estimated work chunk near the soft
boundary after new independent check commands pass. Repeated passing runs of the
same command, output and model claims are not progress. Hard and extension count
bound the reserve. Attempts still have their own timeout; return structured
`continue` before it. Children share the parent envelope, without a second reserve
or an obsolete initial deadline.

The controller sends a deduplicated review to the verified origin near the soft
boundary, without a repair takeover or repeated user approval. Avoid concurrent
changes when a worker remains active. After it yields/exits, revise the expected
allocation from existing evidence:

```sh
ctask budget-reforecast WORKFLOW --estimate remaining.json \
  --source 'actual origin evidence review' --request stable-review-id
ctask resume WORKFLOW --retry-failed
```

Reforecast only uses reserve inside the existing hard limit; it cannot grow hard,
reset use or increase recovery counts. If remaining work no longer fits, submit
one consolidated evidence-based envelope, not repeated arbitrary extra hours.
Review delivery is not execution. Waiting/manual gates require their actual input.

## Meter host work in the same task

```sh
ctask budget-run --source 'actual authorized host verification' WORKFLOW -- python3 verify.py
```

This executes argv without a shell under the repository lease, STOP, frozen
contract and total ceiling. It grants no new business operation or acceptance.
Do not pass credentials in argv or start unmetered detached jobs. At the ceiling,
the command process group is terminated. Original final checks still apply.

Import previously incurred host work once, before computing remaining capacity:

```sh
ctask budget-charge WORKFLOW --seconds MEASURED_SECONDS --evidence /absolute/receipt.json \
  --source 'actual host work and any overrun correction' --request stable-charge-id
```

Stable IDs prevent duplicate charges and changed retry inputs. Charges preserve
overruns honestly; they do not retroactively authorize them. Standalone run time
also accumulates across resumes. Explicit external waits pause both child and
parent active clocks while STOP/contract guards remain active. Waiting seconds
are separate. A long-running observation command counts as active work unless
the task yields as external waiting.

Status reports budget mode, use, soft/hard, reserve and the `budget.json` audit
path. The contract-bound ledger is hash chained; inconsistent history fails
closed. Supervision windows and repair counts remain separate existing limits;
a runtime policy does not silently renew an old supervisor or add a repair.

## Budget alerts and verified recovery

The controller reads this same ledger for 80%/90% warnings against the hard
ceiling, including recorded host charges and uncommitted active heartbeat time.
Explicit external waits do not accrue, and unknown unmetered origin work is
labelled unknown. Paused/stopped/completed tasks do not trigger new budget wakes.
Soft exhaustion with authorized reserve remains an origin reforecast; it is not
a new hard-budget approval. Hard exhaustion produces a durable hub alert only,
without launching an unfunded repair. Threshold deliveries deduplicate across
restarts; unknown delivery is never blindly replayed.

The hub controller verifies recovery using a live workflow owner and live actual
command (including an active child stage), followed by a later observation of
output, command or criterion progress. A start event, generation change, queue
receipt or heartbeat timestamp alone cannot close an execution follow-up. The
stalled command ending is also not evidence that the overall task recovered.
These controller rules do not change task outcomes, business permissions or
supervisor takeover allocations. Old observations made under weaker rules are
not retroactively proof of recovery.

## Answer recorded just before an automatic budget stop

A guard can stop the old allocation seconds after the hub records a real answer.
Ordinary `accept-answer` still refuses STOP. The verified origin can instead
provide the original guard's JSON receipt:

```sh
ctask accept-answer QUESTION --budget-stop-evidence /absolute/guard-status.json
ctask budget-configure WORKFLOW --estimate remaining.json --source 'actual approved decision' \
  --request stable-adoption-id --hard-limit-seconds APPROVED_TOTAL_SECONDS --accepted-answer QUESTION
ctask resume WORKFLOW --retry-failed --accepted-answer QUESTION
```

The receipt must say `active_budget_stop_requested`, contain a successful stop
outcome for exactly this workflow, and match the STOP timestamp/deadline. The
already-recorded answer must precede that stop. Contract, origin, generation and
actual answer/delivery must still match. This is not a general STOP override and
does not apply to a later answer, a completed/paused task, a new generation,
changed guard evidence or a subsequent STOP. Only origin can receive the answer;
the hub cannot impersonate it. Origin still checks what the user's actual words
approve; the code does not interpret every answer as permission.

Acceptance records evidence only: STOP, task state, budget and processes remain
unchanged. The next command installs the bounded policy while retaining STOP and
all charges. An explicit approved hard cap bounds even a larger forecast. Resume
then rechecks the accepted proof and policy before clearing only that matching
stop through the normal launch path. Any later stop/evidence change blocks it.
The controller reopens that stopped follow-up only after this explicit supported
resume; actual recovery still requires live processes and subsequent progress.
