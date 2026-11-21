# Explicit legacy workspace association (candidate only)

This stage fixes the exact parent-workspace / direct-child Git project mismatch
for one legacy workflow. It does not complete the five-fix maintenance objective
or activate this candidate. Approved-delivery links, internal receipts,
responsibility, completion deduplication, the portable bundle and the final
origin-only qualification remain separate stages.

`workspace-association` is an observation API. It neither edits a native header
nor creates an actor credential, permission grant, task restart or business
execution. `verify_session`, `is_actor`, v2 verification/authentication and the
transport project checks retain their existing rules. A UUID locator is still
only the existing legacy actor compatibility proof, never v2 authentication.

## Qualification and proof

Version 1 supports only an explicitly selected canonical parent directory and
its direct child Git repository with a regular `.git` directory. It does not
support arbitrary ancestor matching, path-prefix matching, symlink aliases,
Git-directory indirection, or linked worktrees. Supporting linked worktrees
later requires its own explicit repository identity proof; v1 rejects them.

Inspection obtains native data exclusively through the trusted Codex adapter:
exact canonical ID, configured home, exact read-only SQLite index lookup,
regular rollout inside that home, original first-line session header/hash and
cwd. Both the origin and current configured hub are pinned. Only the existing
verified origin or hub can inspect, register or revoke; registration does not
let the hub ask as the origin or let the origin answer as the hub.

The evidence JSON binds the exact workflow path and ID, origin file checksum,
semantic and byte contract hashes, canonical project, native home/header/cwd,
hub configuration, execution revision/start generation and actual Git root,
Git/common directory and physical directory device/inode. Branch/HEAD progress
and appended native messages do not invalidate the proof. A new workflow start,
changed execution revision, contract/origin/home/header/hub drift, replaced Git
directory, changed evidence or revoked record fails closed. Even returning a
registered workflow to the ordinary exact-cwd branch cannot bypass its pinned
record or revocation.

The registry is exclusively ATO-owned metadata at
`interaction.directory()/workspace-associations/<workflow-path-sha256>.json`.
Each workflow has up to 64 registrations with checksummed history. Revocation
retains the original proof, source, actor and timestamp. Re-registration after
revocation appends a new identity; active records are never silently replaced.
Evidence files are bounded to 256 KiB; ledgers to 2 MiB. Keep the evidence file
at its registered canonical path because every use rechecks its bytes. These
checks detect drift; the registry is trusted local ATO state, not a substitute
for OS access control against a writer who already controls that state.

## Candidate CLI

Use this checkout's `runner.py` with the existing Python 3.10+ environment.
Do not invoke an installed launcher to test the candidate. During fixtures use
private `ATO_HOME` and `CODEX_HOME` and synthetic indexed IDs. Real-record use is
reserved for the separately authorized final origin probe; these instructions
do not authorize live registration or activation.

From the bound legacy origin or configured hub, set `WORKFLOW` to the exact
workflow and `WORKSPACE` to its original canonical header cwd. `PROOF` must be a
private, regular JSON file. These shell variables are illustrative only.

```sh
python runner.py workspace-association inspect "$WORKFLOW" --workspace "$WORKSPACE" > "$INSPECTION"
python -c 'import json,sys; json.dump(json.load(open(sys.argv[1]))["expected_evidence"], open(sys.argv[2], "w"), indent=2)' "$INSPECTION" "$PROOF"
python runner.py workspace-association register "$WORKFLOW" --workspace "$WORKSPACE" --evidence "$PROOF" --source 'Actual explicit association instruction/evidence source'
python runner.py workspace-association inspect "$WORKFLOW" --workspace "$WORKSPACE"
python runner.py read-question "$QUESTION_ID"
```

Inspection emits `expected_evidence`, the latest registration and a truthful
`absent`, `valid` or `invalid` validation result. Review that exact evidence
before registration. Additional fields, including purported execution grants,
are rejected. Repeating an identical registration is idempotent. There is no
`--actor-file` shortcut; neutral grants cannot impersonate a native actor.

```sh
python runner.py workspace-association revoke "$WORKFLOW" --source 'Actual revocation reason'
```

Revocation can follow generation drift and never clears STOP, changes old
questions, answers or acceptance, restarts execution, or alters notification
preferences. If a changed workflow contract is invalid, inspection/registration
and revocation reject it rather than legalizing the contract. Preserve existing
history; diagnose the invalid contract through its original owner.

## Integration and rollback boundary

`interaction.binding` passes the exact workflow context through the neutral
router. Only the trusted adapter resolves the precise legacy mismatch through
this registry; direct unscoped legacy binding still rejects the mismatch.
Actual `read-question`, `ask` and controller reconciliation use that same path.
Existing STOP/completed/closed and question-generation checks remain in force.
Reading a question still creates no approval, presentation or acceptance.

For this inactive candidate, rollback means not selecting its source and/or
revoking a synthetic association. No native/state migration is required.
Never delete or rewrite original workflow files to make a proof pass. Any
future live activation or rollback needs separate authorization and must retain
running pinned runtimes, all five-fix acceptance and existing notice history.
The earlier efficiency candidate and its remaining qualification stay separate.
