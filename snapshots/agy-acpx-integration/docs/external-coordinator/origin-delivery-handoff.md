# Origin-delivery review and remaining host work

This is a worker handoff, not an actual installation/demo receipt. No worker
model admissions, dependency installation, entry activation, credential reads,
native configuration changes, old task migration or global binding changes
were performed. Only origin may execute the explicit final-delivery contract.
The external `origin-host-next.md` review identified the three changes below;
the prior qualification logs and original failures remain intact.

## Reviewed source corrections

- The worker no longer puts repository `.git`/`.agents` in its no-read authority
  roots. Native workspace write protection remains. The actual native boundary
  regression now requires Git status and metadata reads to succeed while
  `.git`/`.agents` writes and private ATO/alias reads fail, in both supported modes.
- Native tool PATH comes from the reviewed Node path and trusted launch Python,
  plus system directories. Shell inheritance remains disabled. The production
  fixture checks secret/PATH injection resistance and executes Node by name;
  the actual native host test also executes Node without a provider/model.
- Smoke emergency cleanup records birth identities and rechecks both identity
  and group leadership before each signal. It never signals missing/reused
  leaders. Mocked regressions cover reuse between TERM/KILL, absent leaders and
  changed groups. Readback still rejects remaining owned numeric groups; it
  does not turn uncertain cleanup into success. Helpers import correctly both
  as CLI scripts and as modules.

New source invalidates the old candidate fingerprint. Exact current archive,
manifest, helper fingerprints and log hashes are recorded in the unbundled
`build/external-coordinator/origin-handoff.json`; the archive SHA sidecar is
`build/external-coordinator/candidate.tar.gz.sha256`. The source archive never
includes state, auth, dependencies, logs or symlinks.

## Exact qualification before the remaining model admission

Use the existing approved interpreter and frozen checker, without changing it:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python \
  OPERATOR_EVIDENCE/ato-agy-acpx-integration-20261119/acceptance-v2.py regression
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python \
  OPERATOR_EVIDENCE/ato-agy-acpx-integration-20261119/acceptance-v2.py bundle
```

The prior candidate passed 636 host tests; the revised suite adds two cleanup
tests. A partial worker suite is not frozen full acceptance. Host must run the
changed actual native filesystem policy test, including Git/Node and authority
checks, as part of the complete suite with zero failures/errors/skips. Do not
rerun native sandbox tests inside the restricted worker or weaken assertions.

## Bounded actual demo and additive entry

1. Review manifest/archive hashes, current source and fixture logs. Read existing
   external dependency/preflight receipts. Verify existing exact private pins;
   missing/changed dependencies are origin prerequisites, never worker npx or
   fallback. Recheck native zero-model handshake if necessary after this change.
2. Extract the verified archive into a fresh immutable versioned runtime outside
   the source checkout. Retain the original archive beside the extracted manifest.
   Run that runtime's `scripts/prepare_external_candidate.py --check`. Keep all
   credentials/state/dependencies outside the source/runtime archive.
3. From that exact extracted runtime use `scripts/external_host_smoke.py prepare`
   with a new unused private demo directory, existing reviewed private dependency
   config and the actual authorization source. It creates a fresh Git repository,
   reviewed greeting contract, scoped actor and canonical ATO workflow, no model.
   The receipt binds that exact runtime and manifest; do not prepare from one
   runtime and run/readback from another.
4. After successful host qualification and review, reserve/admit **one** actual
   Codex ACP turn via `scripts/external_host_smoke.py run --directory DEMO
   --confirm-host-regression-passed --admit-real-codex`. It uses the existing
   fresh workflow with max_attempts=1 and <=300s per model turn, foreground only.
   No replay after unknown delivery, timeout, interruption or failed checks.
   The original incidental admission already counts: total may not exceed three,
   <=2 Codex and <=1 optional AGY. Only one new Codex admission remains.
5. Run `scripts/external_host_readback.py --directory DEMO`; inspect actual
   greeting.py, canonical workflow/stage/attempt correlation, schema-valid native
   completion, independent command checks and zero owned processes. Model text,
   end_turn and EOF alone do not establish acceptance.
6. Install only the unused additive `OPERATOR_HOME/.local/bin/ato-control`, using
   an exact reviewed interpreter/runtime and fixed private ATO_HOME/CODEX_HOME/
   dependency config. Preserve source modes; do not use the development wrapper's
   ATO_PYTHON override as a production trust boundary. Read back the same demo's
   init/status/result via the actual installed entry. Preserve global ctask,
   controller/hub/bindings, native state and all old tasks. Do not overwrite an
   unrelated existing entry. A rollback removes only the newly owned entry.
7. If useful, reserve one fresh AGY print invocation <=300s to inspect this SAME
   prepared/accepted workflow through only scoped ATO commands and report its
   evidence. No old AGY conversation, second Codex task, direct acpx management,
   bootstrap/grant/approval command, detached process or automatic replay. Record
   exact executable/version, actual admission and cleanup, or precise unavailability.
   Give the user the installed scoped command and Chinese AGY prompt in usage.md.
8. Origin writes the external `origin-delivery.json` from actual observations
   and hashed evidence, including additive entry readback, preserved global
   bindings, real Codex ACP, independent ATO checks, cleanup zero and cumulative
   admissions including the prior one. The worker must not fabricate this file.

Finally origin executes the frozen final gate:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python \
  OPERATOR_EVIDENCE/ato-agy-acpx-integration-20261119/acceptance-v2.py host
```

This stage returns `needs_host`. Total delivery remains incomplete until those
actual actions and the final frozen gate succeed. No ordinary global ATO/
controller/hub switch, business deployment, Git publication or extra admission
is implied by this handoff.
