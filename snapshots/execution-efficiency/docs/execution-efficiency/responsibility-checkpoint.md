# Responsibility stage checkpoint

This is the inactive, isolated responsibility stage of `task.md`, not full-task completion. The implementation adds explicit `responsibility_policy: {"version": 1}` for new workflow contracts. It does not mutate or silently migrate historical contracts or notices. Legacy Python creation and old IDs/delivery claims retain their previous semantics; new-mode actions carry a comparable `failure_episode`. No deployment, installation, provider/native CLI call, recovery enablement, business retry or counter reset occurred.

The existing controller database stores `responsibility` actions independently from failure notices and diagnostic operational actions. Claims bind workflow, canonical project, contract, execution generation, origin/hub role and authenticated actor, evidence, next action and fixed expiry (maximum 900 seconds). Repeating an identical claim returns the original deadline. Claims cannot be replaced or renewed for the same semantic episode. Claims are diagnostic responsibility records, not execution leases or grants.

New-mode handling ignores general origin active/unknown activity. It distinguishes verified task processes, exact existing execution leases, bounded exact recovery requests and task-specific claims. Unverified process metadata remains a cleanup fence and cannot establish successful handling. Dispositions record actual subsequent execution, a specific external condition and finite recheck trigger (at most one day from the obligation), an existing exact canonical question, or independently verified completion/closure. External waits cannot be renewed in the same episode. Diagnostic delivery/reply/heartbeat never satisfies disposition or task completion. The existing controller tick performs these observations; no extra service or scheduler was added.

`failure_attention.failure_episode(directory, state)` includes the original contract and real task/attempt/check/criterion signatures, excludes elapsed/update/heartbeat/accounting fields, and is persisted on new actions. State checksum drift is tolerated only with equal semantic evidence. Immutable origin/contract evidence remains checked. Historical records without comparable semantics remain unknown on mismatched checksums; their claimed/presented records are not re-notified. Each notice validity decision records its exact reason/predicate. Staling a notice leaves the separate responsibility obligation pending. A new failed attempt creates a new episode; actual verified subsequent progress, valid completed successor or genuine closure may resolve the obligation.

The VPS source reference was read at `OPERATOR_EVIDENCE/ato-vps-followup-gap-assessment-20261119/{reproduce.py,assessment.json,reproduction-results.json}`. Its historical reproduction intentionally asserts the old busy-session/checksum behavior. New-mode fixtures reproduce the same idle/active/unknown/metadata scenarios using comparable episode actions, while preserving the old-format behavior for protected compatibility tests. The frozen external `Responsibility` cases pass in a read-only loader; the actual external acceptance command remains scheduler-owned.

Current verified local evidence: 43 new responsibility tests pass (including real controller DB, read-only CLI, role proof, concurrent claims, expiry/late reply, canonical ask and cleanup/no-replay cases); four frozen external Responsibility cases pass; 172 related inherited tests pass, followed by 138 final compatibility/runtime tests. These counts overlap and are not added to a full-discovery count. Logs are in `evidence/responsibility-*.log`; final responsibility evidence uses `responsibility-qualified-worker.*` and compatibility uses `responsibility-final-compatibility.log`. Kernel identity positive fixtures inject a synthetic start identity because `/bin/ps` is unavailable in this worker sandbox; unknown identities fail closed. This is fixture qualification, not native or provider proof. The final combined run is 47 tests (43 new plus four external), zero failures/errors/skips. Its exact source hashes and frozen preservation result are in `evidence/responsibility-qualified-worker.json`; the stage handoff is recorded in `responsibility-validation.json`.

Remaining stage gate (run by scheduler on host, unchanged argv):

```
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py responsibility
```

The complete objective still requires the remaining worker-session/checkpoint/verification/metrics implementation stages, fresh integrated compatibility/fault regression, reproducible inactive candidate and migration/rollback delivery. After origin reviews the exact final manifest, the origin-only native sample remains:

```
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py host_sample --run-origin
```

The recorded sample authorization is bounded to 12 native turns/1200 seconds, the same currently configured Codex model/auth/permission profile, and temporary Git/file tasks only. Do not repeat failure/unknown samples automatically. A passing responsibility stage does not complete the total task or establish a speedup.

## Operator interface (inactive until explicitly selected)

Add `"responsibility_policy": {"version": 1}` only to a new workflow's reviewed contract. There is no migration command for frozen old contracts. `responsibility-status WORKFLOW` and `task-summary WORKFLOW --format json` expose the separate unresolved obligations and all pending follow-ups, even when `inbox` has no questions. The ordinary text task summary includes the actor, claim validity/deadline, reason and next action. These queries do not start a controller, write schema or accept any result.

The existing verified origin/hub can record one finite diagnostic claim:

```
ctask responsibility-claim WORKFLOW --role origin --next-action 'Inspect the exact independent failure' --evidence DIAGNOSIS_FILE --source 'Actual diagnosis source' --seconds 300
```

A v2 role must also supply its existing task-scoped `--actor-file`; a locator environment variable is insufficient. Worker/user/session-host roles cannot claim responsibility, and role authentication cannot approve user decisions. Changing project, workflow, contract, generation, origin/hub or evidence invalidates a claim. Same-episode reissue does not renew expiry, even after it expires.

Record an actual diagnostic outcome with `responsibility-disposition WORKFLOW --role origin --outcome OUTCOME --next-action TEXT --evidence FILE --source SOURCE` (and the scoped `--actor-file` for v2). The supported outcomes are:

- `authorized_action`: requires actual subsequent bound execution and verified owner/child process identity. It records evidence; independent observation must still verify progress. It does not execute the proposed action or authorize a retry.
- `external_wait`: also requires `--condition TEXT --recheck-at UNIX_TIME`. The deadline must be finite, in the future and within one day of the original obligation. This records a review trigger; it starts no polling job and cannot be renewed within the episode.
- `human_question`: also requires `--question-id` for a current exact canonical `ask`. It does not ask again, answer, present or accept the question. An accepted answer still requires followthrough.
- `closed`: requires existing independently completed acceptance, verified completed successor or genuine user closure. It does not create closure or manual acceptance.

Notice validity and obligation state are separate. Codes include `claim_expired`, `claim_binding_changed`, `claim_evidence_changed`, `semantic_failure_episode_changed`, `immutable_failure_evidence_changed`, `execution_start_generation_changed`, `claimed_recovery_delivery_no_replay`, `external_wait_due`, `diagnostic_disposition_overdue` and `diagnostic_reply_requires_disposition`. Legacy checksum drift reports `legacy_failure_checksum_uncomparable` plus the exact evidence path/predicate; it is unknown, not resolved. New waiting/claim/diagnostic records are silent. Only canonical human questions and the existing once-only unattended failure route may notify according to the existing policy.
