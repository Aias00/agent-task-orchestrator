# Agent Task Orchestrator

`ato` submits and manages durable agent work in isolated Git worktrees. One
Task ID follows the specification, execution attempts, STOP, independent
checks, human questions and acceptance. The optional Reviewer supplies
technical evidence. The enrolled user accepts the exact checked product.

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
      },
      {
        "id": "delivery",
        "description": "The user accepts the exact checked product",
        "kind": "manual"
      }
    ]
  }
}
```

```sh
ato project validate --repo "$REPO" --spec "$SPEC"
ato run --repo "$REPO" --spec "$SPEC"
ato status "$TASK_ID"
ato list --repo "$REPO"
```

`run` returns the Task ID once it verifies background admission. Add
`--foreground` to wait for the same Run. Commands return one JSON result
with `ok`, `data` and `error` fields. Read pending decisions from the
authorized interaction identity. The enrolled user records their own answer
and, once independent checks pass, accepts the precise product and checks.

```sh
ATO_ACTOR_FILE="$USER_PROOF" ato inbox
ATO_ACTOR_FILE="$USER_PROOF" ato answer "$REQUEST_ID" --answer "$USER_ANSWER"
ATO_ACTOR_FILE="$USER_PROOF" ato accept "$TASK_ID" --evidence "$EVIDENCE"
ato stop "$TASK_ID"
ato resume "$TASK_ID"
```

Check status after requesting STOP. Resume requires verified cleanup and
the current controller's authority. Unresolved native delivery or execution
remains fenced until the host can reconcile it. Management help is under
`ato project --help`, `ato agent --help` and `ato system --help`.

## Release and verification

See the [release guide](docs/runtime-upgrade.md) for offline staging,
read-only diagnosis, authorized cutover and rollback. Launcher installation
is inert. The release journal preserves each previous executable and the
original observer identity alongside the selected snapshots. Task records,
worktrees and mailbox evidence remain in their respective namespaces.

Run `python3.12 scripts/run_tests.py` with the pinned dependencies present
in the selected Python environment and the npm fixture under
`integrations/pi-acp`. The suite uses synthetic adapters and temporary state.
The [contract](docs/ato-unified-contract.md) specifies result codes and
evidence. The [document index](docs/README.md) includes development status and
historical implementation records.
