# Offline snapshot, cutover and rollback

`scripts/release_snapshot.py` stages a specific Git tree and verifies the
qualified snapshot. Supply canonical absolute paths, the full commit, and an
unused snapshot directory. Core staging uses Git and Python 3.12 or later.

```sh
python3.12 scripts/release_snapshot.py --stage \
  --repo "$SOURCE" --commit "$COMMIT" --snapshot "$SNAPSHOT"
```

Stage verifies the tree from a Git archive, creates a virtual environment,
checks the dependency graph, and compiles the tracked Python sources. For
Pi/ACP, add `--with-pi --wheelhouse "$WHEELHOUSE" --npm-cache "$NPM_CACHE"`.
This installs the pinned Python and Node dependencies offline. Use
`--node-bin` and `--npm-bin` to select exact tools. Existing staging commands
that supply the dependency cache paths still select Pi/ACP.

`--run-tests` runs the full offline fixture suite, which requires the same
Pi/ACP dependency caches. The manifest records `with_pi` and records `tests`
as `not_run` by default or `passed` after a successful requested test run. It
also records hashes, modes, links, logs, and interpreter inventory in the
read-only snapshot. Stage keeps failed evidence for inspection.

For a fresh target, `python3.12 -IB scripts/quick_install.py` stages and
installs the checked-out commit in one command. If a snapshot is already
prepared, use its `scripts/install.py` to install the single `ato` executable
and optionally the Skill. A different executable already selected at the
target requires the journaled procedure below.

## Inspect and switch an existing selection

Prepare absolute paths for the bin directory, unified ATO state directory and
native home. The actual operator authorizes a safe cutover and the disposition
of any pending work. Inspect the exact context first.

```sh
python3.12 scripts/release_snapshot.py --dry-run \
  --repo "$SOURCE" --commit "$COMMIT" --snapshot "$SNAPSHOT" \
  --bin-dir "$BIN_DIR" --ato-home "$ATO_HOME" --codex-home "$CODEX_HOME"
```

The report identifies selected snapshots and interpreters, previous and
candidate observer fingerprints, and mailbox and process blockers. It writes
no runtime files, locks, or policy. A legacy ATO context defaults to the
selected ATO/native pair. Use `--legacy-ato-home` and `--legacy-codex-home`
for a different pair. Use `--legacy-context` for the original CODEX_HOME-only
layout, with equal *legacy* home locators. If a prior H1 launcher exists,
`--legacy-hub-home` selects its exact state root. Pass the same options to
activation and rollback.

Once the operator authorizes the selected paths and safe boundary, create a
transaction in an unused private directory outside the bin, state and
snapshot paths.

```sh
python3.12 scripts/release_snapshot.py --activate --allow-runtime-change \
  --commit "$COMMIT" --snapshot "$SNAPSHOT" --bin-dir "$BIN_DIR" \
  --ato-home "$ATO_HOME" --codex-home "$CODEX_HOME" --transaction "$JOURNAL"
```

Mixed prior sources require explicit `--converge-mixed-entrypoints`. When
an external interpreter for a prior entrypoint has been updated in place,
`--allow-prior-interpreter-change` records its current digest in the
journal. The interpreter path and link must still match. The target requires
exact verification.

The journal records each managed executable's presence, exact bytes and mode, and
the sealed source and interpreter for each present entry. It includes the old
and new observer identity and both state contexts. Dry-run reports Task and
mailbox activity. Existing Runs keep their sealed source and mailbox rows
remain in place. The tool stops the verified observer with
SIGTERM and holds both mailbox writer boundaries while replacing the launcher
and removing the recorded retired entries. An already authorized observer
requires a prepared target observation configuration. The tool verifies its
identity and advancing heartbeats. With observation absent, the tool only
switches the executable selection.

## Restore the exact previous selection

```sh
python3.12 scripts/release_snapshot.py --rollback --allow-runtime-change \
  --bin-dir "$BIN_DIR" --ato-home "$ATO_HOME" --codex-home "$CODEX_HOME" \
  --transaction "$JOURNAL"
```

Rollback uses the same journal and both namespaces. It verifies the original
sealed sources, entrypoint bytes and modes, mailbox state and process
identities. It restores each recorded original executable and deletes
transaction-created executables that were previously absent. It restores the
previously authorized observer in its original context. Repeating a verified
rollback leaves that selection in place. A failed preflight leaves the
journal unchanged. If recovery fails after mutation begins, the journal
records `recovery_required`. Retain the journal and sealed snapshots for
recovery.

Task and Run ledgers, STOP, attempt histories, worktrees and delivery claims
remain available in their original namespaces. The selected release can
observe Runs from an earlier release while the original processes continue.
A later explicit continuation of a settled Run records the newly selected
release in its next Run receipt.

The selected release can clean up verified idle completed or stopped Tasks
from an earlier release with `ato project cleanup TASK_ID --confirm`. For
completed Tasks, it rechecks the retained product and preserves its exact
contents on the task branch before removing the worktree. A stopped Task
requires a clean worktree.

Retain older sealed snapshots while their Runs can still be active, and keep
the transaction journal for rollback. Fixture qualification and source
installation leave real activation and provider verification pending.

The installer, release script and runtime share snapshot, mailbox and
launcher verification through `ato_release_lib.py`. See the
[host setup guide](ato-host-setup.md) for authorized delivery reconciliation
and the [module map](source-map.md) for the public and private runtime
boundaries.

Optional Skill installation is locked separately. Before upgrading a Skill,
archive the current version outside its destination, then install from the
selected sealed snapshot. Restore the archived Skill alongside an executable
rollback when the Skill was changed separately.
