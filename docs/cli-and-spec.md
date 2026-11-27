# CLI & Specification Reference

## Commands

| Command | Description |
| --- | --- |
| `ato run --repo PATH --spec FILE [--foreground]` | Submit a task into a new isolated Git worktree |
| `ato list [--repo PATH]` | List tasks (optionally filtered by repository) |
| `ato status TASK_ID` | Inspect task state, execution, checks, review, product, notifications, and watchdog |
| `ato stop TASK_ID` | Durably request STOP and terminate live worker process group |
| `ato resume TASK_ID [--foreground]` | Continue a `stopped`, `blocked`, or answered `waiting_input` task |
| `ato inbox` | List open human questions across tasks |
| `ato answer REQUEST_ID --answer TEXT` | Record a user reply to an open question |
| `ato accept TASK_ID [--evidence FILE]` | Record optional manual user approval receipt |
| `ato project validate --repo PATH --spec FILE` | Validate and preview normalized spec digest |
| `ato project diff TASK_ID [--stat]` | Show structured diff summary (and unified patch unless `--stat`) |
| `ato project trace TASK_ID` | Show bounded task lifecycle event timeline |
| `ato project recheck TASK_ID [--checks-only]` | Re-run frozen host checks (and reviewer unless `--checks-only`) |
| `ato project cleanup TASK_ID --confirm` | Retain product commit on branch and remove worktree |
| `ato agent doctor [--probe-versions]` | Inspect local Codex, Claude, AGY, Pi, and ACP availability |
| `ato agent binding TASK_ID` | Inspect task origin, worker adapter, and reviewer config |
| `ato agent reviewer TASK_ID --kind KIND [--model M] [--effort E]` | Select or change task reviewer (`codex`, `claude`, `agy`) |
| `ato agent ask TASK_ID --kind K --question Q --reason R` | Open a human question in `ato inbox` |
| `ato system status` | Inspect v2 runtime, Observer heartbeat, and notification counts |
| `ato system notification-policy [--repo PATH]` | Inspect global notification and repo mute policy |
| `ato system observer-start` / `observer-stop` | Start or stop the background Observer/Watchdog daemon |

## Task Specification (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "repo": "/path/to/repo",
  "base_ref": "HEAD",
  "worker_contract": {
    "objective": "Implement feature X with unit tests",
    "scope": "Only files under src/ and tests/",
    "authorization": "Local edits and test execution only",
    "agent": {"kind": "codex"},
    "sandbox": "workspace-write",
    "max_attempts": 3,
    "acceptance": [
      {
        "id": "unit-tests",
        "kind": "command",
        "description": "Test suite passes",
        "checks": [["python3", "-m", "unittest"]]
      }
    ]
  },
  "reviewer": {
    "agent": {"kind": "codex"},
    "max_review_rounds": 1
  },
  "guardrails": {
    "protected_paths": ["README.md"],
    "forbid_delete_paths": ["tests"]
  },
  "worktree": {
    "shared_env_links": [".venv"]
  }
}
```

Supported worker kinds: `codex`, `claude`, `pi`, `acp_pi`.
Supported reviewer kinds: `codex`, `claude`, `agy`.
Foreground Pi ACP coordinator is available programmatically via `ato.adapters.pi_acp.PiCoordinator`.
