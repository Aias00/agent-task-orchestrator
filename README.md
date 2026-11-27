# ATO (`agent-task-orchestrator` v2)

`ato` is a lightweight local orchestrator for durable coding agent tasks in isolated Git worktrees.
One Task ID tracks the task spec, execution runs, independent host checks, optional read-only technical review, human Q&A inbox, diff/guardrails, watchdog monitoring, and Codex completion notifications.

A Task runs its declared worker and records independent checks separately; failed checks are surfaced as `reason="CHECKS_FAILED"` on the completed status when advisory checks are enabled, so always inspect `reason` and `checks` alongside `status`.

## Installation

Requires macOS (or Linux for core Codex workflows), Git, and Python 3.12+. Core installation has zero third-party dependencies.

```sh
# From a local checkout (editable: code edits take effect immediately)
sh ./install.sh --editable --skills-dir "$HOME/.codex/skills"

# Or fresh install from GitHub
curl -fsSL https://raw.githubusercontent.com/Aias00/agent-task-orchestrator/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
ato --help
```

To include optional Pi / ACP support (`agent.kind = "pi"` or `"acp_pi"` and `PiCoordinator`), add `--with-pi` (requires Node.js 24+ and npm):

```sh
sh ./install.sh --editable --with-pi
```

## Quick start

```sh
# 1. Validate a task spec
ato project validate --repo /path/to/repo --spec ./task.json

# 2. Submit a task (prefix ATO_SUBMISSION_KIND=agy when invoking from AGY)
ato run --repo /path/to/repo --spec ./task.json

# 3. Inspect status, checks, watchdog, and diff
ato status TASK_ID
ato project diff TASK_ID --stat

# 4. Handle human questions, recheck, stop, resume, or cleanup
ato inbox
ato answer REQUEST_ID --answer "..."
ato project recheck TASK_ID
ato stop TASK_ID
ato resume TASK_ID
ato project cleanup TASK_ID --confirm
```

Start the optional background Observer daemon to receive Codex completion notifications and Watchdog alerts:

```sh
ato system observer-start
ato system status
```

## Documentation

- [Architecture](docs/architecture.md) — 4-layer single-package architecture, SQLite schema, and safety invariants
- [CLI & Spec Reference](docs/cli-and-spec.md) — commands, task specification format, guardrails, reviewers, and Pi/ACP
- [Agent Skill](skills/ato/SKILL.md) — concise instructions for Codex/AGY agents driving `ato`
