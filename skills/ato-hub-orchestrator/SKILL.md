---
name: ato-hub-orchestrator
description: Drive an approved isolated ATO work/check/read-only-review workflow, switch its hub agent, or inspect its terminal evidence. Use for the H1 hub CLI, separate from existing ctask commands.
---

# ATO hub orchestration

Read [the implementation guide](../../docs/h1-offline-implementation.md) for the
CLI and qualification boundaries. Use `ato-hub` when explicitly installed from
the reviewed checkout; otherwise use that checkout's Python and `hub_cli.py`
with an explicit private `ATO_HOME`. Installation is a separate authorized action
(`install.py --hub`, optional `--hub-skills-dir`), never an automatic skill step.
The launcher defaults unset `ATO_HOME` to `$HOME/.local/share/ato-hub-state`;
an existing value is retained. This skill enables no service.

For an approved objective, freeze a hub task spec with the original scope,
authorization, worker agent, sandbox/permissions and independent host check argv.
Keep a manual final acceptance criterion. Worker and review agents are separate;
the hub's active agent controls subsequent review turns. Keep memory disabled.
Use an explicit private `ATO_HOME` outside the repository. A new task provisions
one new external worktree/branch and refuses collisions.

First run `ato-hub hub-list --repo REPO` with the canonical repository path.
Reuse a matching logical Hub; create one (`hub-create`) when none matches.
The list is a read-only, redacted summary, not a resume grant. Create the task
(`task-create --spec`), then execute
`task-run HUB_ID TASK_ID`. For authorized background execution, keep its actual
process/log identity and inspect `task-status`; a tool call returning does not
prove a background process exists. Terminal notifications default to durable
`recorded_only` evidence. No interactive wake-up is promised without a separately
configured trusted notification hook.

The first `task-run` executes the frozen first stage even if regression checks
already pass. With no preset stage, it constructs one initial implementation
task from the frozen objective and original command checks; with neither, it
refuses to launch a worker. Further stages keep the ordinary criterion-driven
selection. Use `task-review-only` for existing code that needs checks/review
without implementation. Do not replace original checks, authority or budgets.

Shared dependencies are explicit: `shared_env_links` allows only `.venv`, `venv`,
`node_modules`, `integrations/pi-acp/node_modules`. Each source must be an
existing ignored, untracked directory, with no symlink alias or existing target.
The link grants no additional write authority; cleanup retains the source.

Only for authorized network/model work, pass the reviewed native home and a
credential-free loopback proxy explicitly, for example:
`ato-hub task-run HUB_ID TASK_ID --worker-codex-home NATIVE_HOME --proxy http://127.0.0.1:7890`.
Native home must be canonical and outside the ledger. `ATO_HOME` stays isolated;
never copy credentials into the spec or ledger. `task-review-only` accepts
`--proxy`, not worker credentials. Ambient authentication/proxy settings are not
implicitly adopted, and offline fixture success grants no network permission.

On completion, read the state, frozen host check evidence and `reviews/round-N.md`.
Report branch, base, worktree, checks, review verdict and remaining manual
acceptance. `ready_for_user_review` is a hard stop. Do not acknowledge, merge,
push, activate a runtime, remove a worktree or infer user approval from a model
verdict. Real provider turns require the user's applicable authorization; offline
fixture success alone supplies none.

Use `task-stop` to signal STOP without waiting for the running hub lock. Do not
clear STOP or automatically replay interrupted workers, unknown delivery or saved
PIDs. Investigate retained workflow history first. Failed checks never skip
directly to model review. A reviewer mutation blocks the task immediately.

`task-review-only` reruns the original host checks and a read-only review of an
already bound worktree. It launches no worker/planner, including after a blocker
verdict. It retains round limits and ownership barriers.

`hub-switch-agent --kind KIND --source REASON` parks the old native session and
rebuilds context from `handoff-brief.md`; never pass another vendor's session ID.
Claude resume remains unknown: use `resume_if_supported` for an explicit fresh
fallback. `hub-rollover --source REASON` retires the current session. An attach
command is available only for a qualified active session while the hub is idle;
it is printed, not executed. `worktree-cleanup --confirm` is an explicit
operator action; it refuses dirty/owned worktrees and retains the branch.
