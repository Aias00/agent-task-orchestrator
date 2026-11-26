# Agent Task Orchestrator

`ato` submits and manages durable agent work in isolated Git worktrees. One
Task ID follows the specification, execution attempts, STOP, independent
checks, human questions and delivery. A Task completes when its independent
checks and any required technical review pass. Users can record approval
separately.

Specs can optionally protect paths or forbid deletions with `guardrails`.

## Quick start

Install from a qualified snapshot using its interpreter. Choose an executable
directory and, optionally, a Skill directory.

```sh
"$SNAPSHOT/.venv/bin/python" -IB "$SNAPSHOT/scripts/install.py" \
  --bin-dir "$BIN_DIR" --skills-dir "$SKILLS_DIR"
ato --help
```

Configure the Worker once and run the installed `ato` as the account that
owns its private state. The default root is `$HOME/.local/share/ato`. Set
`ATO_HOME` to select a different private root. This account can submit and
manage its local tasks directly. Set `ATO_ACTOR_FILE` when acting as an
explicitly enrolled actor. [Trusted host setup](docs/ato-host-setup.md)

Write a JSON spec next to the repository or use an absolute `repo` path.

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
