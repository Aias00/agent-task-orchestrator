# ATO (`agent-task-orchestrator` v2)

`ato` is a lightweight local orchestrator for durable coding agent tasks in isolated Git worktrees.
One Task ID tracks the task spec, execution runs, independent host checks, optional read-only technical review, human Q&A inbox, diff/guardrails, watchdog monitoring, and Codex completion notifications.

A Task runs its declared worker and records independent checks separately. Exhausted checks block with `reason="CHECKS_FAILED"`; inspect status, reason, checks, review, diff and pending questions before reporting verified completion.

## Controllers and execution agents

Controllers use [MCP first](skills/ato/SKILL.md) for discovery, validate/submit,
list/status/diff/trace/inbox and STOP/resume. HTTP API and CLI remain supported for
operators and scripts. See the [migration and qualification guide](docs/mcp-controller.md)
for Codex, Cursor agent, AGY (Antigravity) and Grok Bot setup and limitations.

Controller transport is separate from execution: workers remain `codex` (macOS/Linux),
`claude`, `cursor` and `pi` (macOS with sandbox-exec). Optional read-only reviewers
remain `codex`, `claude`, `agy`, `codex_acpx`; see [ACP setup](docs/acpx-reviewer.md).
MCP does not introduce a Grok Bot worker or change sandbox/retry/check behavior.
New MCP/API tasks do not bind or wake native conversations. Existing verified
Codex-origin notifications remain supported; AGY notifications remain unsupported.

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

The Pi worker (`agent.kind = "pi"`) uses the lightweight `pi` CLI under macOS `sandbox-exec` with no extra Python or Node packages beyond optional `pi` itself.

## Remote coordinator entrypoints

Optional HTTP REST (`/api/v1`) and official MCP Streamable HTTP (`/mcp`) share one
listener and the existing ATO engine. On the later deployment host:

```sh
sh ./install.sh --server
# Configure repository aliases, scopes and token environment variables first:
ato serve --config /path/to/server.json
# Local MCP clients may instead use an explicit configured stdio principal:
ato mcp --stdio --config /path/to/server.json
```

The listener defaults to `127.0.0.1:8765`. Both HTTP entrypoints require bearer
authentication; human answers use a separate token from bot control. Submission
is for trusted controllers because task specs authorize executable work/checks.
See [MCP/API deployment and usage](docs/mcp-api.md) and the
[safe host configuration example](docs/server-config.example.json) for optional
dependency installation, TLS proxy policy, REST/SDK examples and crash-safe
idempotency limitations. Poll task status/inbox; webhook/push integration is not
included. The [prior isolated API/MCP validation](docs/mcp-api-validation.md) and
[controller regression report](docs/mcp-controller-validation.md) distinguish SDK
verification from native client and deployment checks still pending.

## CLI quick start (operators and authorized fallback)

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
- [CLI & Spec Reference](docs/cli-and-spec.md) — commands, task specification format, guardrails, reviewers, and Pi worker
- [MCP & HTTP API](docs/mcp-api.md) — authenticated transports, scopes, deployment, idempotency and polling
- [MCP controller migration](docs/mcp-controller.md) — distributable client examples and qualification boundaries
- [Agent Skill](skills/ato/SKILL.md) — MCP-first controller instructions and authorized CLI fallback
