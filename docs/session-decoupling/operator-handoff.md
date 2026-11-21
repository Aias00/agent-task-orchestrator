# Origin host qualification and reversible delivery

The full frozen goal remains in [task.md](task.md). This handoff authorizes no
activation, installation, Git publication, real session migration, private auth
reading, external provider call or additional recovery allowance. Passing this
worker stage is not overall acceptance. The original workflow retains all
attempts/history and its external frozen checks.

## Exact current candidate and host work

Source root: `OPERATOR_SOURCE/ato-session-decoupling-20261118`.
Interpreter: `python3.12`.
External checker: `OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py`.
All commands below run from the source root. Never run ctask hub/configuration
commands against real identities as part of qualification.

First independently inspect/check the existing manifest, not regenerate it:

```sh
python3.12 scripts/prepare_session_candidate.py --check
```

The assigned delivery gate's exact argv is:

```sh
python3.12 OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py delivery
```

Keep the original frozen regression and required directed checks as full-task
acceptance, with their own isolated ATO/CODEX configuration. Mode names are
`contracts`, `codex`, `transport`, `recovery`, `pi`, `hub`, `regression`. The
complete regression command (including new delivery tests) is:

```sh
python3.12 OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py regression
```

Only origin runs the one bounded actual-Pi/local scripted-model qualification.
Use the frozen `real_pi` mode so the proof is tied to the candidate hash and
successful evidence is reused only for that exact candidate:

```sh
python3.12 OPERATOR_EVIDENCE/ato-session-decoupling-20261118/acceptance.py real_pi
```

Its exact underlying smoke argv is:

```sh
python3.12 scripts/verify_pi_session_local.py --output /private/tmp/ato-session-local-proof.json --local-only
```

These are alternative invocation layers for the same bounded sample, not two
samples to run. The frozen wrapper chooses its own temporary output and imposes
240 seconds. The script bounds its scenario to 210 seconds plus owned cleanup,
uses an empty temporary HOME/native config, literal dummy key, numeric loopback
server, installed Pi 1.5.2 and the read-only pinned adapter. It never invokes a
real provider. Treat denied sockets/nested OS sandbox as host-verifiable limits;
do not disable policy or modify tests to hide them. Do not replay after unknown
delivery. A failed sample needs inspection of its retained safe evidence.

Safe detail is retained in `build/session-decoupling/real-pi-local/UNIQUE_ID/`:
steps.json, native-events-before/after-restart.json, model-receipts.json,
artifact.json, proof.json (or failure.json). Records exclude raw model prompts,
full responses, native auth and credential files. Native runtime/config/rollouts
are temporary and removed. The proof explicitly labels actual CLI with a scripted
model, synthetic user fixture, new-session checkpoint reconstruction, cloud
untested, and workflow acceptance performed by separate checks.

The six model responses are admitted once in a fixed sequence. On each tool-result
request, the server checks the exact prior call ID, function/arguments and expected
file result. Native receipts must include successful tools on every turn and exact
adapter/project/session/canonical question/message/turn/generation correlation,
including the reconstruction turn. The adapter's typed observations must settle
the existing canonical MessageBus to `turn_completed`; native journal success
alone is insufficient. Tool IDs/status survive sanitization, while arguments and
tool/model text do not. Model traffic receipts survive failure too. Codex probes
also cover static compatibility aliases and SQLite opens in the isolated native
namespace; ATO's own MessageBus remains usable.

Finally the original `acceptance.py delivery` independently checks the portable
inventory, baseline hash and handoff documents. Retain the original external
logs as acceptance; a worker self-report or native end_turn cannot replace them.
The successful actual-Pi proof is candidate-hash-bound. Do not change sources,
regenerate the manifest, or take another sample without origin reviewing the
change and its original authorization/history.

## Reproducible review bundle

The source bundle is `build/session-decoupling/candidate.tar.gz`, with its sibling
`candidate.json`. Preparation uses `scripts/prepare_session_candidate.py --archive`;
this was performed by the worker before handoff. It fixes tar ownership/timestamps
and gzip timestamp/name, includes all allowlisted source files plus the manifest,
and preserves recorded file modes. It excludes dependencies, credentials, native
history, runtime/task state and build logs. The existing read-only node_modules
link is a host prerequisite, not an archived package or an installation action.
The seeded source diff and stage checkpoints remain separately under build as
local provenance; their hashes are recorded, never substituted for acceptance.

`--check` independently rebuilds the file/mode inventory and provenance and checks
the existing archive without extracting or executing it. It detects source,
manifest, permissions, archive content/metadata and protected-test drift. Do not
regenerate either artifact during origin qualification to hide a mismatch. Two
independent archives of the same manifest must have identical bytes. The archive
is a review artifact; it grants no activation or new host permissions.

## Proposed activation sequence — requires later explicit authorization

1. Record the reviewed `candidate.json` SHA256, original launcher bytes/hash,
   previous runtime path, selected Python and selected state locators. The seeded
   baseline records actual live runtime hashes; `source-manifest.json` is a
   historical artifact and is not qualification evidence. Require all frozen
   checks and the host sample before considering activation.
2. Quiesce the existing controller/executors using their existing owned lifecycle
   and verify exact process identities. Reconcile pending/unknown delivery before
   changing executable selection. No additional observer/service, widened repair
   allowance, directory rename or reset is introduced.
3. Keep existing state/native roots and hub binding for compatibility. For any new
   neutral-only pilot choose a separate explicit ATO_HOME; create no implicit
   migration of real sessions. Review each state class in migration.md.
4. Under that later authorization, prepare a temporary launcher adjacent to the
   selected launcher with the exact intended interpreter/source, validate its
   contents/mode, then atomically replace only the selected executable pointer.
   The intended launcher payload is `#!/bin/sh` followed by `exec` of the absolute
   interpreter above, the absolute candidate `runner.py`, and literal `"$@"`.
   Do not run scripts/install.py --force as an automatic delivery step. Preserve
   the queue guard and existing environment/policy selection; do not install the
   bundled skill unless separately requested.
5. Start only the previously authorized owners, verify one owner and existing
   STOP/unknown barriers, then inspect read-only status. No real question or model
   call is an activation test without its own authorization.

## Executable rollback — no data rewind

If selection must be reverted, first stop only candidate-owned processes and
verify their termination. Keep all current ledgers and receipts. Confirm the
previous reader understands the affected context; park new v2/Pi contexts if it
does not. Prepare the recorded previous launcher bytes as an adjacent temporary
file, check the recorded hash and executable mode, and atomically restore that
executable pointer. Keep the current queue guard, real session identities,
attempt counters, STOP/closure and state untouched. Never overwrite SQLite/WAL
or JSON with a pre-activation snapshot. Restart only prior authorized owners and
verify status before any effect. These steps are a plan; no switch or rollback
has been executed by this task.
