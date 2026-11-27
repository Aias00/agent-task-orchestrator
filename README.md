# ATO (`agent-task-orchestrator` v2)

`ato` is a lightweight local orchestrator for durable coding agent tasks in isolated Git worktrees.
One Task ID tracks the task spec, execution runs, independent host checks, optional read-only technical review, human Q&A inbox, diff/guardrails, watchdog monitoring, and Codex completion notifications.

When independent checks still fail after the allowed repair attempts, or fail during a recheck, the task is `blocked` with `reason="CHECKS_FAILED"`. ATO keeps the product and check receipts so you can inspect the failure and recheck after correcting it.

## Supported agents

### Task submission

| Origin | How it works |
| --- | --- |
| Codex | Submit with `ato run` from a Codex session. The Observer can send completion notifications to a verified origin session. |
| AGY (Antigravity) | Prefix submission with `ATO_SUBMISSION_KIND=agy` and query the result with `ato status TASK_ID`. Session notifications are currently unsupported. |
| Terminal or script | Use `ato run` and query by Task ID. Set `ATO_SUBMISSION_KIND=cursor` from a Cursor terminal to record the source. |

### Workers

Select a worker using `worker_contract.agent.kind`.

| Kind | Required CLI | Platform |
| --- | --- | --- |
| `codex` (default) | Codex CLI | macOS or Linux |
| `claude` | Claude Code | macOS with `sandbox-exec` |
| `cursor` | Cursor CLI (`cursor-agent`) | macOS with `sandbox-exec` |
| `pi` | Pi CLI | macOS with `sandbox-exec` |

Run `ato agent doctor` to see which CLIs ATO can find. Optional read-only reviewers are `codex`, `claude`, `agy`, and `codex_acpx`. The ACP reviewer needs Node.js, a compatible acpx runtime build, and a Codex ACP launcher. See the [ACP Reviewer setup](docs/acpx-reviewer.md).

## Installation

Requires macOS (or Linux for core Codex workflows), Git, and Python 3.12+. ATO core has zero third-party dependencies. Install the CLI for your chosen worker separately.

### Latest from GitHub `main`

```sh
curl -fsSL https://app.aias.eu.org/ato | sh
```

The short URL redirects to the GitHub installer. You can also use the direct URL.

```sh
curl -fsSL https://raw.githubusercontent.com/Aias00/agent-task-orchestrator/main/install.sh | sh
```

### Pin a version

Set `ATO_INSTALL_REF` on the installer process to fetch a specific tag. Use the same tag in the URL to pin the installer script too.

```sh
curl -fsSL https://raw.githubusercontent.com/Aias00/agent-task-orchestrator/v2.1.0/install.sh \
  | ATO_INSTALL_REF=refs/tags/v2.1.0 sh
```

### Install from a local checkout

```sh
sh ./install.sh --editable --skills-dir "$HOME/.codex/skills"
```

The launcher is installed in `~/.local/bin` by default.

```sh
export PATH="$HOME/.local/bin:$PATH"
ato --version
```

Re-run your chosen install command to update a regular installation. For an editable installation, update the checkout. If the Observer is already running, restart it to load the updated code.

```sh
ato system observer-stop
ato system observer-start
ato system status
```

## Quick start

```sh
# 1. Validate a task spec using the format in the CLI & Spec Reference
ato project validate --repo /path/to/repo --spec ./task.json

# 2. Submit a task (prefix ATO_SUBMISSION_KIND=agy when invoking from AGY)
ato run --repo /path/to/repo --spec ./task.json

# 3. Inspect tasks, current status, check history, and the diff
ato list --repo /path/to/repo
ato status TASK_ID
ato status TASK_ID --check-history
ato project diff TASK_ID --stat

# 4. Handle human questions, recheck, stop, resume, or cleanup
ato inbox
ato answer REQUEST_ID --answer "..."
ato project recheck TASK_ID
ato stop TASK_ID
ato resume TASK_ID --timeout-seconds 1800
ato project cleanup TASK_ID --confirm
```

The timeout override applies only to the new run. Start the optional Observer daemon for Watchdog monitoring and completion notifications in verified Codex sessions. AGY-origin tasks can be tracked with `ato status TASK_ID`.

```sh
ato system observer-start
ato system status
```

## Documentation

- [Architecture](docs/architecture.md) — 4-layer single-package architecture, SQLite schema, and safety invariants
- [CLI & Spec Reference](docs/cli-and-spec.md) — task specification, verification history, retry policy, and CLI commands
- [Codex ACP Reviewer](docs/acpx-reviewer.md) — optional runtime and launcher setup
- [Agent Skill](skills/ato/SKILL.md) — concise instructions for Codex/AGY agents driving `ato`
