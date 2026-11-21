# Operator handoff: inactive tracking candidate

Implementation SHA-256: `0f2caac929606b375922ee9cd796ce335eb1032d1a44853674620d4da607a8e4`

The exact manifest/archive SHA-256 pair is in
`build/tracking-rectification/hashes.json`; verify it with
`python3 -B scripts/prepare_tracking_candidate.py --check`. The candidate is
inactive. This packaging stage returns `ready_for_checks`, not overall complete
or `needs_host`. Only the final dedicated handoff stage may request the origin
probe after the scheduler's unchanged regression/bundle gates pass.

## Final origin action

Authorization is the 2026-11-19 explicit user repair instruction and its bounded
read-only host handoff, reproduced in the fixed task. The final origin inspects
this exact candidate and performs **one probe of at most 120 seconds**. No
live activation, global install, business/model/queue send, deployment, retry,
attempt/STOP reset, metadata edit or new credential is authorized.

The origin prepares a private plan from `origin-plan.example.json`. The example
is intentionally incomplete; it is not a proof or a record of production
execution. Do not fill an approval mapping by parsing answer prose. Inspect the
actual accepted question, original acceptance definitions, separate delivery
contract/authority and completed evidence, then explicitly select the approved
criterion mapping and all deferred criteria. Keep all old external-source and
measurement gaps. Worker fixture evidence says nothing about the real records.

The allowed primary records are:

- `question-9c3eb5e4a646e8ffd1b5` and its original answer/source/acceptance.
- `OPERATOR_PROJECT/vps-picks/.codex/tasks/workflows/20261119-020710-0609b14c`
  and separately completed `20261119-051218-c82c16cf` in that same directory.
- Exact generic workflows whose canonical result IDs are
  `result-dbf8134af146aa5ff0a2` and `result-8610e94538370b8ce6e5`, located from
  `OPERATOR_EVIDENCE/generic-platform-hub-followthrough-20261119/duplicate-completion-8610e945.json`.
- Their exact origin/hub native metadata, original controller notice/outbox rows,
  policy/mute files, actual contract/evidence and existing compatible lineage or
  shared operation receipt. Native lookup stays inside the trusted adapter:
  exact indexed ID and first header only, no conversation-body scan.

The plan must enumerate exact canonical files and SHA-256s; it cannot provide a
directory-wide read grant. Include named workflow states/contracts/origins,
all evidence and manual acknowledgements actually referenced by validators,
existing lineage records/proof files (including bounded sibling lineage
metadata read by the canonical conflict checker), original exact controller
SQLite file, policy/mute/hub/question, and selected native `state_*.sqlite`
indexes. For each SQLite file, explicitly pin or record absence of `-wal` and
`-shm`. For each named workflow pin or record absence of STOP, closure and
lineage. Include any original attempt/check files whose preservation is part
of the qualified scope. Pin rollout **header SHA-256**, ID and path in
`native_headers`; do not copy or hash its conversation body as identity.

The native index and original control database are opened with `mode=ro` under
an OS write fence. If an actively changing database or unavailable read-only
WAL mapping prevents qualification, report that exact failure; do not relax the
fence, remove sidecars, rewrite metadata or silently regenerate approved hashes.
The audit fence rejects unlisted reads, all sockets, nonidentity subprocesses,
native activity/body reads and writes outside the isolated observation root.
Only Git `rev-parse` identity queries are allowed, with networking denied.

Use the already bound origin/hub conversation and existing configured native
home. The probe requires `CODEX_THREAD_ID` to equal the pinned hub. It does not
set another session ID, obtain a credential or upgrade actor authority. Use a
fresh private canonical observation path under `/private/tmp` (or the canonical
OS temporary root), with an existing parent and a fresh
external proof filename; symlink paths and reused roots/proofs are refused.

After reviewing/filling the plan and sealing it to the exact manifest and
source fingerprint, run from the candidate root with the prescribed interpreter:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python -B scripts/verify_tracking_readonly.py \
  --plan /absolute/private/reviewed-origin-plan.json \
  --observation-root /private/tmp/ato-tracking-readonly-UNIQUE \
  --proof OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/origin-readonly-proof.json
```

The script launches one foreground `sandbox-exec` child, denies all networking
and all writes except the new isolated observation ledger, and enforces the
120-second deadline. The parent writes the exact external proof only after
successful immutable before/after comparison. No daemon or controller loop is
started. It performs actual candidate `read_question`, controller question
reconciliation, residual diagnostic-scope and completion-dedup projections;
it does not call `tick` or a transport. Existing exact completion actions and
unknown outbox claims are imported verbatim into the disposable ledger solely
to test the existing physical-delivery barrier. No new presentation is made.

The generic pair must already have qualified original compatible lineage, or
the exact canonical shared operation receipt must already occur in both
completed evidence sets. Equal verification bytes alone are insufficient.
Never synthesize a retrospective operation receipt, add it to historical
state, or create a live lineage to make the probe pass. If original proof does
not satisfy the candidate, retain the failed result and report the precise
remaining gap in the final stage.

For the exact frozen source fingerprint, the origin may import the prepared
helper without reading a live record:

```sh
python3 -B -c 'import sys; sys.path.insert(0,"scripts"); import verify_tracking_readonly as p; print(p.source_fingerprint())'
```

Successful proof asserts all six frozen projection/preservation booleans,
actual old `blocked` and delivery `completed`, exact source/manifest hashes,
unchanged live-file/header/absence pins, zero business/model requests and zero
notifications. It does not claim activation or overall task completion. The
origin then runs the unchanged final gate:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/acceptance.py host
```

Keep the original proof/plan private and external to the source archive. Do not
repackage source after binding the proof unless the final stage repeats the
review under the existing bounded authorization. Do not claim a failed or
unperformed host check passed. Migration/rollback and the still-independent
efficiency qualification are detailed in `migration.md`; that old manifest,
sample claim, failed native-profile evidence and full objective stay intact.
