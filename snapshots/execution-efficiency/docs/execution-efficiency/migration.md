# Migration and rollback boundaries

This delivery is inactive. Preparing or checking the archive does not install a
launcher/skill, move live tasks, change hub/origin, or authorize deployment. The
complete task is [task.md](task.md); the precise host actions are in
[operator-handoff.md](operator-handoff.md). Do not edit the frozen external gates,
53 protected predecessor tests/fixtures, original contracts, STOP files, attempts,
accounting, claims, permissions or historical IDs to make migration appear valid.

## Portable preparation

Run the existing host interpreter with `scripts/prepare_efficiency_candidate.py`.
It snapshots the frozen external baseline and exact completed Linger manifest
into `build/execution-efficiency/provenance/`. It refuses conflicting provenance
or protected-byte drift, then emits `candidate.json` and deterministic
`candidate.tar.gz`. `--check` performs an independent read-only inventory, content,
mode and archive-byte comparison. It does not rely on Git status or tracked files.
The source archive contains no `.codex`, `.git`, virtualenv, node_modules, auth,
private state, native transcript or host-sample receipts. Existing dependency
links are read only and never packaged as dependencies.

Extract only into a fresh private directory. Reject links, duplicate paths,
absolute paths, parent traversal and nonregular members before extracting. Run
`python scripts/prepare_efficiency_candidate.py --check-extracted` there. To also
run ordinary `--check`, copy the original archive into the extracted
`build/execution-efficiency/candidate.tar.gz` location; the archive itself is not
recursively nested. The checker works from bundled provenance when the original
external baseline is unavailable. All source files and modes must agree. Running
this standard-library packaging check requires no model, account, Git repository
or dependency installation. Executing ACP tests separately needs the already
qualified Python and pinned read-only Node dependency environment; portable
source validation is not a dependency-installation or native-platform claim.

## Version matrix

| Record or mode | Candidate behavior | Existing records |
| --- | --- | --- |
| Runtime selection v1 | Trusted new CLI creation pins actual runtime and stages inherit it | Unbound tasks stay legacy; no implicit migration |
| Responsibility/disposition v1 | Additive records in existing control database with bounded verified roles | Original status and notice delivery IDs retained; incomparable legacy evidence unknown |
| Worker session policy v1 | Explicit exact task-owned Codex native continuation | Absent policy remains old cold execution |
| Worker session policy v2 | Explicit checkpoint reconstruction with a new native ID and ancestors | Never reclassifies old continuation as native resume |
| Checkpoint policy v1 | Requires execution policy v2 and bounded worker writes | No historical checkpoint adoption or counter reset |
| Verification policy v1 | Explicit complete offline fingerprints and original evidence | Absent policy reruns unchanged original checks |
| Trace spans v1 | Additive measured intervals and explicit unknown phases | Existing v2 ledger semantics and unknown historical values preserved |

New policies are independently opt-in in a new authorized plan/contract. Do not
edit an existing frozen contract to enable them. A warm native session grants
no broader sandbox, network, capacity or actor authority. Unknown capability,
profile drift, STOP, claimed delivery or unconfirmed cleanup prevents reuse.
A missing receipt is a refusal, never permission to create another native ID.
Manual/external checks remain live and uncached; final integration remains fresh.

## Future activation procedure (requires separate authorization)

1. Review the exact manifest and all original host gates, including actual Codex
   proof bound to its final manifest. Preserve every failed/unknown receipt.
2. Stage the checked archive in a new immutable release directory with its
   already approved dependency/interpreter profile. Do not modify that directory
   after any task is pinned to it. Verify source and modes after staging.
3. Rehearse only on disposable state copies. Keep current ledger and controller
   schema migration additive. This source candidate supplies packaging and
   verification only, without a live migration command. Do not execute historical
   README upgrade-tool examples if that tool is absent. A source archive alone
   does not authorize copying production SQLite or native profile material.
4. Only with explicit activation authority change the launcher for future
   creation. Do not freeze an unrelated global launcher or rebind old tasks.
   Running/pinned work continues using its original verified release. Report
   actual installed bytes and selected runtime separately.

## Rollback

Revert only future runtime selection/launcher choice to the known prior release
under the activation's precise permission. Keep every pinned release needed by
existing tasks. Never roll back live SQLite/controller files over new writes or
restore a stale task/history snapshot. Do not change STOP, unknown-delivery,
claim, grant, native identity or failure accounting records. A task pinned to a
faulty runtime needs explicit operator diagnosis and an authorized successor
plan; model-selected import commands or silent rebinding are forbidden. If
cleanup/delivery is unknown, preserve it for exact origin review. No automatic
extra repair, supervision or sample window is introduced by this migration.
