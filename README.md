# Agent Task Orchestrator

`ato` submits and manages durable agent work in isolated Git worktrees. One
Task ID follows the specification, execution attempts, STOP, independent
checks, human questions and delivery. A Task runs its declared stages and
records independent checks separately; failed checks are surfaced as
`reason="CHECKS_FAILED"` on the completed status, so inspect `reason` and
`checks` alongside `status`. Users can record approval separately.

Specs can optionally protect paths or forbid deletions with `guardrails`.

## Installation

For a fresh installation on macOS, you need Git and Python 3.12 or later.
The core installer works without Node.js, npm, or a dependency cache.

```sh
curl -fsSL https://app.aias.eu.org/ato | sh
export PATH="$HOME/.local/bin:$PATH"
ato --help
```

The short URL redirects to the GitHub installer and fetches the current
commit on `main`. You can also download the installer from GitHub directly.

```sh
curl -fsSL https://raw.githubusercontent.com/Aias00/agent-task-orchestrator/main/install.sh | sh
```

### Install a specific version

Choose a tag from the release list. This example installs `v1.0.1`.
[GitHub Releases](https://github.com/Aias00/agent-task-orchestrator/releases)

```sh
curl -fsSL https://raw.githubusercontent.com/Aias00/agent-task-orchestrator/v1.0.1/install.sh |
  ATO_INSTALL_REF=refs/tags/v1.0.1 sh
```

From a Git checkout, run `sh ./install.sh` to install the checked-out commit.
The default launcher is `~/.local/bin/ato` and snapshots are stored under
`~/.local/share/ato-releases`. To change these locations, use `--bin-dir`
and `--releases-dir`. Use `--skills-dir "$HOME/.codex/skills"` to install the
Codex skill. With a piped installer, pass options after `sh -s --`.

### Add Pi/ACP support

Pi/ACP is optional. For a fresh install with Pi/ACP, use Node.js 24 or later,
npm, and the dependency caches for the commit you are installing. From that
Git checkout, prepare the caches and install with the same Python interpreter.

```sh
WHEELHOUSE="$HOME/.cache/ato/wheels"
NPM_CACHE="$HOME/.cache/ato/npm"
mkdir -p "$WHEELHOUSE" "$NPM_CACHE"
python3.12 -m pip download --only-binary=:all: -r requirements-acp.lock -d "$WHEELHOUSE"
npm ci --ignore-scripts --no-audit --no-fund --prefix integrations/pi-acp --cache "$NPM_CACHE"
ATO_INSTALL_PYTHON=python3.12 sh ./install.sh --with-pi --wheelhouse "$WHEELHOUSE" --npm-cache "$NPM_CACHE"
```

Pi CLI versions and adapter pins are listed in the
[compatibility registry](integrations/pi-acp/compatibility.json).

### Upgrade an existing installation

Use the [journaled cutover and rollback procedure](docs/runtime-upgrade.md)
to switch an existing launcher and Observer to a new snapshot. If you already
have a sealed snapshot for a fresh installation, run its
[scripts/install.py](scripts/install.py) with the snapshot's interpreter.

## Quick start

For local tasks, install and sign in to the native Codex CLI. ATO looks for
`codex` on `PATH` or uses `CTASK_CODEX_BIN` when set. Configure a profile to
select explicit tools or enroll scoped actors. Run `ato` as the account that
owns its private state, which defaults to `$HOME/.local/share/ato`. Set
`ATO_HOME` to choose another private root and `ATO_ACTOR_FILE` to act as an
enrolled actor. [Trusted host setup](docs/ato-host-setup.md)

Place a JSON spec using `"repo": "."` in the repository, or set `repo` to the
repository's absolute path.

```json
{
  "schema_version": 1,
  "repo": ".",
  "base_ref": "HEAD",
  "worker_contract": {
    "objective": "Update the command's output",
    "scope": "Edit within the isolated worktree and run the declared test",
    "authorization": "Approved local implementation and check",
    "acceptance": [
      {
        "id": "tests",
        "description": "The repository's tests pass",
        "kind": "command",
        "checks": [["python3.12", "-m", "unittest"]]
      }
    ]
  }
}
```

```sh
ato project validate --repo "$REPO" --spec "$SPEC"
ato run --repo "$REPO" --spec "$SPEC"
ato status "$TASK_ID"
ato project diff "$TASK_ID" --stat
ato list --repo "$REPO"
```

`run` returns the Task ID once it verifies background admission. Add
`--foreground` to wait for the same Run. Commands return one JSON result
with `ok`, `data` and `error` fields. A command-only specification is
sufficient. If the Observer has a verified submission session and transport,
the first successful completion goes to that Codex conversation.

Read pending decisions through the authorized interaction identity. An
enrolled user can record their answer or optional approval with scoped
credentials. For a local Task, its state owner can record an optional
approval with `ato accept "$TASK_ID"` without setting `ATO_ACTOR_FILE`.

```sh
ATO_ACTOR_FILE="$USER_PROOF" ato inbox
ATO_ACTOR_FILE="$USER_PROOF" ato answer "$REQUEST_ID" --answer "$USER_ANSWER"
ATO_ACTOR_FILE="$USER_PROOF" ato accept "$TASK_ID" --evidence "$EVIDENCE"
ato stop "$TASK_ID"
ato resume "$TASK_ID"
```

Check status after requesting STOP. Resume requires verified cleanup and
the current controller's authority. Unresolved native delivery or execution
remains fenced until the host can reconcile it. Use
`ato project cleanup "$TASK_ID" --confirm` to remove a verified idle worktree.
For a completed Task, cleanup preserves the verified product on its task
branch, committing any verified uncommitted files. A stopped Task must have
a clean worktree. Use `ato agent doctor --probe-versions` to check configured
tool versions locally. Management help is under
`ato project --help`, `ato agent --help` and `ato system --help`.

## Release and verification

See the [release guide](docs/runtime-upgrade.md) for offline staging,
read-only diagnosis, authorized cutover and rollback. Launcher installation
is inert. The release journal preserves each previous executable and the
original observer identity alongside the selected snapshots. Verified idle
Tasks can be retained through a release switch. Task records, worktrees and
mailbox evidence remain in their respective namespaces.

Run `python3.12 scripts/run_tests.py` with the pinned dependencies present
in the selected Python environment and the npm fixture under
`integrations/pi-acp`. The suite uses synthetic adapters and temporary state.
The [contract](docs/ato-unified-contract.md) specifies result codes and
evidence. The [document index](docs/README.md) includes development status and
historical implementation records.

## Repository layout

| Directory | Responsibility |
| --- | --- |
| `bin/`, `ato_cli/` | Public launcher and command parsing |
| `ato_common/` | Shared configuration, file IO, and process identity |
| `ato_task/` | Task and Run records, execution, acceptance, and observation |
| `ato_workflow/` | Workflow stages, session routing, mailbox, and notifications |
| `ato_review/` | Review contracts, isolation, and worktree checks |
| `ato_adapters/`, `ato_pi/` | Native agent adapters and optional Pi support |
| `ato_release/`, `scripts/` | Sealing, installation, and release commands |
| `docs/history/`, `scripts/history/` | Archived qualification records and tools |

The root contains the public and private Python entrypoints, shared primitives,
and compatibility modules. [Source and capability map](docs/source-map.md)

## License

ATO is distributed under the [MIT License](LICENSE).
