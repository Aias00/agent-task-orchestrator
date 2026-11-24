# Offline snapshot, cutover and rollback

`scripts/release_snapshot.py` stages a specific Git tree and verifies the
qualified snapshot. Supply canonical absolute paths. Select the full commit,
an unused snapshot directory, and local caches containing the pinned Python
wheels and npm dependency content.

```sh
python3.12 scripts/release_snapshot.py --stage \
  --repo "$SOURCE" --commit "$COMMIT" --snapshot "$SNAPSHOT" \
  --wheelhouse "$WHEELHOUSE" --npm-cache "$NPM_CACHE"
```

Stage uses a Git archive and verifies the tree before qualification. It then
installs from the local caches with offline flags, runs the full fixture
suite, checks the dependency graph and compiles the tracked Python sources.
It records hashes, modes, links, log and interpreter inventory in the
read-only snapshot. Stage keeps failed evidence without marking it qualified.
A separately supplied `--node-bin` and `--npm-bin` select exact trusted
tools. This qualifies the local fixture behavior.

For a fresh target, use the snapshot's `scripts/install.py`. It installs
the single `ato` executable and optionally the Skill. A different executable
already selected at the target requires the journaled procedure below.

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

Mixed prior sources require explicit `--converge-mixed-entrypoints`. The
journal records each managed executable's presence, exact bytes and mode, and
the sealed source and interpreter for each present entry. It includes the old
and new observer identity and both state contexts. A busy or uncertain Task,
native execution, mailbox delivery or turn slot blocks the change.
Quiesce launcher use for cutover. The tool stops the verified observer with
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
rollback leaves that selection in place. A third-party file replacement,
changed source, ambiguous PID, or busy mailbox records `recovery_required`
for inspection. Retain the journal and the sealed snapshots for recovery.

Task and Run ledgers, STOP, attempt histories, worktrees and delivery claims
remain available in their original namespaces. Verified idle Tasks can stay
on their original sealed release during activation or rollback, including
Tasks awaiting acceptance. Active commands, unsettled execution or provisioning,
and busy mailbox or turn slots still require a safe boundary.

`ato status` reports `RUNTIME_MISMATCH` and `SELECT_TASK_RUNTIME` for an
unfinished Task pinned to a different release. Retain the older sealed
snapshot and transaction journal. The journal identifies the release and
interpreter selected before the switch. Use that exact interpreter and
`ato.py` with the same recorded ATO and Codex homes for the Task's commands.
Fixture qualification and source installation leave real activation and
provider verification pending.
