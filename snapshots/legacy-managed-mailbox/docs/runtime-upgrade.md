# Offline runtime upgrade and rollback

This checkout delivers local source and rehearsal tooling. It does not replace
the stable runtime driving this task. Installation, activation and live-state
changes require a separate explicit operator action. No command below was run
against the actual deployment/hub by this stage.

## Read-only assessment

```sh
python3.12 scripts/upgrade_runtime.py --dry-run --runtime /path/to/old-runtime
```

This reads only runner/source fingerprints and prints JSON. It never opens a hub,
task ledger or SQLite database, changes a launcher, copies credentials, invokes a
CLI, starts a scheduler, or infers native identities. It requires an existing
runtime with `runner.py`; a successful assessment is not acceptance or activation.

## Staging and temporary compatibility rehearsal

```sh
python3.12 scripts/upgrade_runtime.py --stage /new/private/upgrade-stage \
  --runtime /path/to/old-runtime
python3.12 /new/private/upgrade-stage/runtime/runner.py --help
```

The destination must not exist and must be outside source/runtime/state roots.
Only runtime Python/MJS, scripts, documentation and the bundled skill are staged;
no `.git`, `.codex/tasks`, build artifacts, native accounts or dependencies are
copied implicitly. The old deployment is retained unchanged as the rollback
source. The staging manifest records all source SHA256 values and the old runner
fingerprint. Staging is not a backup of the old source: retain that deployment
and its version independently. Run the repository tests from the original source
checkout, not the staged runtime (which intentionally excludes tests).

Optional snapshots are for explicit **offline** temporary state roots:

```sh
python3.12 scripts/upgrade_runtime.py --stage /new/private/rehearsal \
  --runtime /temporary/old-runtime --offline \
  --state-root /temporary/project/.codex/tasks \
  --state-root /temporary/home/ctask-interaction
python3.12 scripts/upgrade_runtime.py --restore /new/private/rehearsal \
  --destination /new/private/restored-records --offline
```

`--offline` is an operator assertion: the tool does not stop or kill owners. All
runner, controller, recovery, adapter and native CLI processes using those records
must already be stopped and verified absent. Quiesce/checkpoint SQLite normally
before snapshotting; WAL/SHM/journal sidecars cause refusal. Never delete sidecars
to force acceptance. No online-consistent backup is promised. Source files must
be stable too. Snapshots are private byte copies with hashes, not schema imports.
Only explicit `tasks`/`ctask-interaction` roots are accepted; symlinks, special
files and named native auth/config files are rejected. Do not supply native user
directories or provider configuration. State snapshots can contain private task
material and endpoint capabilities: keep them local with owner-only access,
outside Git and shared memory. The tool does not inspect text for arbitrary
embedded secrets; supply only authorized ctask records, never raw account files.

Restore checks every snapshot hash before copying and writes numbered roots plus
a restore map to a new directory. It never overwrites original paths or rewrites
absolute workflow/hub bindings. This supports inspection and recovery rehearsal,
not automatic activation of a copied project. Cross-machine session migration
and identity rebinding are unsupported.

## Data compatibility and restart

Legacy workflow/phase JSON, origin UUIDs, questions, sourced answers, controller
tables and outboxes stay authoritative. No bulk conversion is necessary. New
deliveries add mailbox IDs; old dispatching/unknown/delivered claims must not be
re-enqueued. Keep original contract hashes/acceptance, STOP files, elapsed budgets,
failure history, generations, reminder counts and recovery attempts/deadlines.
Do not recreate workflows or reset counters as a migration operation.

The new mailbox lives separately under
`CODEX_HOME/ctask-interaction/mailbox/`. Preserve its private endpoint capability
files together with the DB and each explicitly registered native adapter runtime
and transcript. Native runtimes are not auto-discovered or copied by this script;
back them up privately on the same host if an operator later authorizes managed
runtime migration. Do not copy native provider account stores. Missing transcript
or ambiguous pending state requires evidence/reconciliation; it never starts a
blank session under the old ID or automatically replays effects.

`tests/test_upgrade_runtime.py` rehearses staging/restore with temporary old
records, opening the old controller twice with the new source, preventing outbox
resends, and retaining cumulative time, failures, limits, STOP, history and actual
answer sources. The mixed-origin suite additionally exercises live protocol
fixture restart, legacy JSON and stopped/paused/stale fences.

## Later authorized installation and rollback

Before any later activation, the external stable host must pass all seven frozen
criteria, including the independently retained 120-case baseline, full suite and
fresh installed-native local-provider probe against the final source. Retain the
reports, exact source fingerprints, old source path, Python/CLI versions and old
launcher/skill files. No cloud authentication is implied by a local provider pass.

Stop and verify all old owners; take offline backups without resetting state.
Install first to an isolated bin/skills directory, inspect `--help`, and explicitly
choose any final launcher switch. For example, **only when later authorized**:

```sh
python3.12 /new/private/upgrade-stage/runtime/scripts/install.py \
  --bin-dir /new/private/test-bin --skills-dir /new/private/test-skills
/new/private/test-bin/ctask --help
```

The installer writes only the selected launcher/skill; `--force` is needed for an
existing target. An operator must retain those exact old files before replacing
them. No services are enabled, no hub/endpoint is registered, and no task starts
as a side effect. Configure provider authentication separately; the CLI mailbox
pump scrubs ambient credentials, and a trusted host supplies explicit provider
settings. Do not widen tool policy to work around authentication.

For source rollback, stop/verify new owners, retain their latest mailbox,
transcripts, receipts and usage/history, then restore the previous launcher/skill
or explicitly install from the retained old source. The original runtime was not
changed by staging. **Old runtime cannot operate managed Claude/Pi origins or
interpret mailbox guards.** Do not run it against active new managed workflows;
pause/close and reconcile them with the new runtime first. For an existing Codex
workflow with new mailbox deliveries, retain the new mailbox and reconcile all
possible effects before old-source operation; never discard ambiguity to retry.

A historical snapshot is not permission to rewind execution or budgets. If any
work, user answer, failure, reminder or recovery happened after backup, keep the
newest ledger and reconcile it before manually restoring any old record. The tool
deliberately restores only to new directories so this cannot silently overwrite
current totals, authorization or history. Rollback does not grant new budget or
business permission. When safe reconciliation is not possible, keep the task
stopped and request the missing evidence through its verified hub.
