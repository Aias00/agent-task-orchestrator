# Origin operator handoff — not deployment evidence

Qualification supplies source, fixtures, deterministic candidate and bounded
host helpers. It did not install packages/entries, access login content, invoke
models, bind a hub, migrate tasks or modify live launchers. Passing this stage
is not completion of the total task. Only origin may perform the existing
explicit final-delivery contract. Preserve the stopped predecessor, original
failure artifacts, original frozen plans and harness-isolation correction.

The origin-delivery worker's subsequent review and exact host sequence are in
[origin-delivery-handoff.md](origin-delivery-handoff.md). That review changes the
candidate after the recorded 636-test host pass, so origin must repeat frozen
regression/bundle and the changed native filesystem test before admitting a model.

## Gates and remaining admissions

1. Review this exact candidate and run the frozen `acceptance-v2.py regression`
   and `bundle` commands on the host. All tests must pass, with no skips. Worker
   nested Seatbelt/socket failures are host checks, never a reason to weaken or
   patch inherited tests. New native filesystem tests exercise the generated
   named policy; gateway fixtures exercise its actual subprocess path.
2. Verify private exact pins/integrity from [acpx-worker.md](acpx-worker.md) and
   the external dependency-prepared receipt. Origin already installed private
   dependencies; qualification does not reinstall them. Missing/changed assets
   are explicit prerequisites, with no npx/network/native fallback.
3. Provision an owner-only host dependency-path config, canonical paths, and a
   clean native home. Existing origin native-home-provisioning reference can be
   consumed in place by trusted native Codex only. Never read/copy/log auth or
   expose human/root ATO credentials to AGY/model tools. Preserve global native
   config, skills and account state.
4. The total cap is three real model admissions, <=2 Codex and <=1 AGY. One
   incidental native validation was already consumed. **At most one additional
   Codex ACP and one optional fresh AGY admission remain.** Counts/history do
   not reset on process failure, timeout, unknown delivery or new demo names.
   Do not replay a prompt or silently substitute a different provider/profile.
5. Review zero-model host policy qualification before using a real admission.
   An unsupported effective sandbox/profile/metadata capability must fail
   closed. Fixtures do not establish native login/provider/model readiness.

## Prepared bounded Codex smoke

These are origin-only commands. `PYTHON`, `RUNTIME`, `CONFIG` and `DEMO` are
reviewed absolute paths. DEMO must be a fresh unused directory outside runtime,
private dependency/authority homes and user/business repositories. The helper
creates a new Git repository; it never uses the earlier live demo or any task.

```sh
"$PYTHON" "$RUNTIME/scripts/prepare_external_candidate.py" --check
"$PYTHON" "$RUNTIME/scripts/external_host_smoke.py" prepare \
  --directory "$DEMO" --dependency-config "$CONFIG" \
  --source '2026-11-19 实际用户 AGY 经 ATO 控制 Codex 的隔离 demo 授权；仅原有范围'
```

Prepare creates and freezes a reviewed demo contract, scoped actor and canonical
ATO workflow; **it does not call a model**. Inspect `$DEMO/contract.json` and
`smoke.json`, exact candidate hash, canonical scope and policy. `native-locator`
is a separate compatibility namespace, not the authenticated native home.
Only after full host regression and policy qualification passed:

```sh
"$PYTHON" "$RUNTIME/scripts/external_host_smoke.py" run --directory "$DEMO" \
  --confirm-host-regression-passed --admit-real-codex
"$PYTHON" "$RUNTIME/scripts/external_host_readback.py" --directory "$DEMO"
```

Run reserves an exclusive durable admission before launch. An existing barrier
refuses another call, even after interruption; no helper deletes it. It uses the
canonical external CLI/Workflow/Runner, max_attempts=1, one stage and <=300s
attempt. The wrapper has a 390s total cleanup bound; STOP/history survive failure.
It records process groups and kernel birth identities from this new workflow.
Emergency cleanup rechecks the live leader's identity/group before every signal;
vanished/reused leaders are never signalled by a stale numeric PGID. Uncertain
residual processes fail readback instead of being adopted or blindly killed.
Readback never invokes models or mutates task state. It checks original command
criteria again, exact manifest/scope/correlation/schema/attempt history and
owned process cleanup. `readback.json` is evidence; assistant text or CLI exit
is not acceptance. A failure remains failure; inspect it without replay.

The smoke reports pinned versions and hashed native session identity/correlation,
not login data. Global process inspection uses PID/group numbers only. Raw model
wire/tool bodies and native stderr are not captured. Evidence remains in the
fresh owned demo and external origin ops, outside candidate source/archive.

## Optional fresh AGY validation and user use

Origin may spend the one remaining optional AGY `--print` admission, <=300s,
only after verifying that installed CLI's exact executable/version/options and
remaining cumulative admissions. Use a fresh native CLI invocation, never an
existing AGY conversation/TUI/PID. Give it only the scoped entry/actor, SAME
prepared contract/repository/workflow and [usage.md](usage.md) instructions.
For the already completed Codex smoke, ask AGY to call init/status/events/result
and report the existing independent evidence; do not create another workflow or
request another Codex turn. Bound the foreground process/group, reserve the
admission durably before launch, keep the receipt and do not auto-retry unknown
execution. If native AGY account/CLI is unavailable, record that fact precisely;
it does not permit installation/login/model fallback by this worker.

## Additive entry and final origin proof

After exact candidate and smoke review, origin may copy the verified archive to
an unused immutable versioned runtime, preserving source modes. Run its portable
`prepare_external_candidate.py --check --archive /exact/original/candidate.tar.gz`
again after extraction. Do not package
or copy credentials, dependency node_modules, state, symlinks, old tasks or hubs.
When running host helpers from an extracted runtime, retain the exact original
archive at `build/external-coordinator/candidate.tar.gz` beside the manifest;
their internal check uses that location. The archive cannot contain itself.
Use a private host config for exact paths. Keep the review archive and SHA256.

Install only the new `OPERATOR_HOME/.local/bin/ato-control`, with no replacement of
`ctask`, controller, skills, shell aliases, hub bindings or old runtime. If an
unrelated entry already exists, do not overwrite it. Production wrapper fixes
an exact reviewed Python/runtime, private ATO_HOME/CODEX_HOME and ATO_ACPX_CONFIG;
it forwards argv only to external_coordinator.py. It must not expose the
human bootstrap or allow model-selected Python/config/executable overrides.
The repository's `bin/ato-control` is a development source entry, not a claim
that the additive production entry has been installed.

Read back the actual installed entry via scoped init/status/result against the
same demo. Verify baseline global launcher/bindings unchanged and no owned
processes remain. Rollback removes only the newly owned additive entry/runtime
reference; retain canonical state/history and native account data unchanged.
No business deployment or Git publication is authorized.

Origin writes external `origin-delivery.json` only from actual observations:
`actual_additive_entry_verified`, `global_binding_preserved`, `ato_checks_passed`,
`real_codex_acp_verified`, `active_owned_processes_after_cleanup`, conservative
`model_admissions` including the prior admission, and `evidence_files` with exact
absolute paths/SHA256. Detailed smoke/readback/entry/global-baseline receipts must
support each claim. Do not synthesize this file from fixture results. Then run
the original external host gate. Only that final stage may report overall delivery.
