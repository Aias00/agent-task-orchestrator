# Qualification and handoff stage

The checkout now includes nonmutating upgrade assessment, isolated source staging,
offline state snapshot/restore rehearsal, migration/restart/rollback tests, and
updated protocol, transport, architecture, README and bundled skill documentation.
No installed runtime, launcher, actual hub, native account or scheduler ledger was
changed. The external stable runner still owns the full seven-criterion contract.

## Evidence boundary

Current available deterministic suite: **147/147 passed** out of 177 discovered
cases. Independently retained available baseline: **90/90 passed** out of 120.
The remaining **30 cases** belong to the unchanged socket/nested-sandbox host
modules (`test_agents.py`, `test_delegation.py`). Both available-suite runs had
zero failures, errors or skips. The original frozen `delivery` command passed;
its dry-run snapshot assertion verified the temporary old deployment was unchanged.
The final strengthened six migration tests also passed separately, including
installing the staged runtime into isolated bin/skills directories and invoking
that new launcher. These results are **not** a complete baseline/full/native pass.

Worker deterministic regression results and exact source SHA256 values are in
`build/message-transport/delivery-validation.json`; retained logs are adjacent.
Migration tests use only temporary old JSON/controller records. The six tests
exercise nonmutating dry-run, independently runnable staged source, retained old
runtime/data, restored budgets/limits/STOP/failures/history, old controller restart
without resending unknown outboxes, additive mailbox creation, snapshot integrity,
path isolation, native-auth exclusion and SQLite sidecar refusal.

This restricted worker does not repeat known Unix/loopback fixture socket or
nested sandbox-exec failures from the adapter/integration stages. It runs all
other current and frozen deterministic modules with their assertions intact,
without marking excluded host modules as skipped or claiming a full-suite pass.
The external host must run unchanged `accept.py baseline`, `full`, and `native`
before whole-goal completion. Original frozen test/check hashes are independently
verified; no tests or acceptance definitions were weakened.

The initial supplementary current-suite invocation from stdin could not be
reloaded by Python multiprocessing spawn (four failures/three errors). That
harness error was corrected with the retained file-based main-guarded
`build/message-transport/run_worker_regressions.py`; all 147 cases then passed.
Its initial log remains as historical evidence, not an unresolved product defect.
The skill quick-validator was attempted but its optional PyYAML dependency is
absent; Ruby's installed YAML parser validated the unchanged name/description
frontmatter, and installation tests validated skill packaging and path resolution.

Earlier host native evidence is retained in
`build/message-transport/native-host-report.json` and `host-validation.json`:
Claude Code 2.1.325 and Pi 1.5.2 each returned two correlated native turns in one
session against the local provider. Subsequent bus/adapter changes require fresh
host native qualification. This stage changes delivery tooling/docs/tests and
does not turn historical native results into a current pass. Neither fixture
evidence nor real native local-provider evidence proves cloud account login.

## Host acceptance handoff

The seven original acceptance IDs remain: `legacy-baseline`, `durable-mailbox`,
`cli-adapters`, `mixed-agent-flow`, `native-protocol`, `complete-regression`, and
`delivery-handoff`. Run their original frozen argv; do not replace checks with
filtered worker commands or historical reports. The stage is code-ready for the
external checks; it is not whole-goal completion or live installation approval.

The local artifact `build/message-transport/handoff.md` maps implementation to
tests/evidence and records source identity, versions, limitations and next host
steps. General installation/rollback instructions live in
[runtime-upgrade.md](runtime-upgrade.md). Build artifacts are ignored by Git and
must be retained separately if the checkout is packaged or moved.
