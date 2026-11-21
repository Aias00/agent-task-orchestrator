# Workspace-binding stage validation

Status: implementation ready for the scheduler's frozen `workspace` acceptance.
This is a stage result, not completion or activation of the maintenance goal.

The new `tests/test_tracking_workspace_binding.py` exercises public CLI/module
paths with isolated temporary `ATO_HOME`/`CODEX_HOME`, synthetic UUIDs, actual
`git init` repositories and read-only `git rev-parse`. A private trap executable
fails any unexpected queue submission; no real queue, model, provider, business
or native-agent test is used. No original headers/indexes/task files are edited
by the association API.

The suite covers the actual pre-existing question/controller mismatch followed
by exact registration and recovery; original contract/state/origin/native-byte
preservation; actual `ask` and hub reads; role and actor boundaries; unchanged
v2 project verification; idempotence; native transcript append compatibility;
revocation/history; execution start/revision expiry; evidence and ledger tamper;
wrong origin/home/header/repository/contract/hub; invalid grants; foreign copied
workflows; actual Git identity/replacement and ambient Git overrides; explicit
direct-child restriction; unqualified Git indirection; symlink workflow/header/
evidence/index/Git/lock escapes; bounded proof/source handling; STOP/completion;
and missing-cwd/exact-project legacy compatibility. The test intentionally
rejects worktree-shaped Git indirection without creating a commit or enabling a
linked-worktree extension.

Worker verification:

- Directed stage suite: 21 tests, zero failures/errors/skips (see stage receipt).
- Related inherited compatibility suites: 174 tests, zero failures/errors/skips:
  `test_codex_session_adapter`, `test_interaction`, `test_control`,
  `test_session_v2_contracts`, `test_session_v2_recovery`,
  `test_session_v2_transport`, `test_neutral_hub`.
- All 49 inherited test/fixture hashes match the frozen external baseline.
- Modified implementation files: `codex_session_adapter.py`, `interaction.py`,
  `session_router.py`, `runner.py`; added `workspace_association.py` and the
  directed test/docs. `AGENTS.md` already differs from the seed baseline in the
  supplied worker checkout; this stage never edited it.

Reproduce the directed suite without the host-only external receipt writer:

```sh
PYTHONDONTWRITEBYTECODE=1 OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python -m unittest discover -s tests -p test_tracking_workspace_binding.py -v
```

The scheduler must run the unchanged external gate outside the worker sandbox:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/acceptance.py workspace
```

That frozen gate was not run or claimed passed by the worker because its
receipts belong to the external host log directory. Full 591-test baseline
regression plus all subsequent stage tests belongs to final qualification; no
assertions are relaxed for worker socket/native-sandbox restrictions.

No installation, Git commit/push, live activation, business deployment,
credential/grant creation, original attempts/STOP/repair reset, scheduler state
mutation or real-record qualification is part of this stage. The final
origin-only bounded read-only proof remains outstanding for its dedicated task.
