# Authorized complete objective

Investigate Archify's actual delayed takeover rejection, then implement and
independently verify the minimum ctask candidate for reasoned rejection,
bounded operational follow-through when no handler exists, and truthful
predecessor/successor delivery queries. Prepare precise rollout/rollback notes.
The user did not authorize deployment of this new candidate in this request.

Authoritative incident: OPERATOR_EVIDENCE/recovery-escalation-20261118/incident.json.
The actual old workflow is STOP+blocked, successor is completed/handoff passed,
and there is no outstanding Archify business blocker. The primary failures were
NOT wall-clock expiry. Main hub resumed at 11:01:33Z, new execution start at
11:01:34Z, then STOP at 11:03:04Z. Origin claims at 11:07:54Z/11:08:03Z occurred
before the 11:29/11:31 expiry times, but were already invalidated. Do not rewrite
any original request, claim a bug in correct STOP/generation rejection, invent a
takeover callback that never happened, or replay Archify to demonstrate this.

## Required behavior and public surface

1. Add read-only public `runner.py takeover-status WORKFLOW --request ID` JSON.
   Expose eligible, precise machine-readable reasons (list of strings), request
   and current generation/status/STOP/handler/deadline facts and safe next action.
   Reason `stopped` must explicitly identify STOP. Unknown request, replaced
   request, generation/status changed, expiry, disabled, binding/contract drift,
   already handled and owner/repository conflict remain distinguishable. Use the
   same validator in actual takeover, preventing diagnostic/claim disagreement.
   A rejected takeover reports its actual cause(s), changes no business state,
   never extends requests or clears STOP. Read-only inspection creates no claims
   or new supervision windows. Preserve backward compatible ValueError behavior.

2. Audit controller/recovery existing detection under user_input_only. Add a
   durable bounded path for unresolved idle workflow + invalid/exhausted/no valid
   recovery handler: explicit pending operational ownership, at most one
   diagnostic-only notification to its verified distinct origin per episode,
   follow matching response/progress, and retain unresolved states when no actual
   recovery follows. A notification is NOT a business repair or authorization:
   no new recovery request/quota/window, no process restart, no hidden resume,
   no new implementation worker. Reconcile live owner, original-origin matching
   turn activity, repo lock, outstanding/unknown delivery, STOP, binding changes,
   successor closure, needs_input and frozen limits before any wakeup. A busy
   handler is not missing. Never blindly replay ambiguous messages, create a new
   ID to resend them, or erase existing diagnostics. Use existing controller,
   not another supervisor/poller. Same hub/origin and user quiet policy must not
   self-queue a progress message. A purely operational dead end stays visible
   as pending until genuinely handled; do not fabricate success.

   If it truly cannot proceed without assigning a handler, a permission/decision,
   missing information or manual acceptance, support one **specific canonical**
   hub question with task, exact blocker, tried steps, immutable evidence and
   recommendation. Do not turn every timeout into an artificial approval. Make
   provenance explicit (controller escalation vs origin question). Require a
   real handler/user decision before any alternate ownership or business work;
   no new general authority. Keep routine status suppressed, Drover project mute
   and once-only notifications. Add a supported verified origin/hub disposition
   API/CLI if needed to record actual diagnosis or specific needs_user context;
   no forged answer, presented, accepted or completed. Explicitly document any
   unavailable/ambiguous-origin case instead of hiding it in quiet routing.

3. Add metadata-only `runner.py workflow-link PREDECESSOR --successor SUCCESSOR
   --evidence LINEAGE_JSON --source TEXT`, independent of accepted user questions.
   Validate verified current hub/origin, actual repository/origin identity,
   authentic contracts/acceptance preservation, original authority and nonexpanded
   permissions, real structured evidence, no cycle/contradiction/unsupported
   branch. Objective wording may differ if full original acceptance and authority
   remain identical; never infer inheritance from filenames. STOP on predecessor
   is a hard EXECUTION fence and must remain untouched; it need not prohibit a
   truthful observation-only link to already completed successor. Never execute
   evidence code. Record evidence SHA, both raw file and semantic contract hashes
   as needed; don't confuse raw SHA256 with core.digest(JSON).

   Evidence schema supports at least predecessor_workflow, successor_workflow,
   predecessor_contract_sha256, successor_contract_sha256, permission_expanded:false,
   origin_thread, hub_thread, acceptance_mapping, source/note. The real historical
   Archify lineage has no successor_contract_sha256/mapping; document an explicit
   enriched evidence wrapper rather than editing its history. Require genuine
   successor completion/independent criteria evidence when projecting completed.
   Reject stripped command/manual acceptance, changed grants/authority, wrong
   origin/repo, tampered/evolving evidence and cycles. Support idempotency.

   Public `status PREDECESSOR` keeps raw `status` unchanged and adds a clearly
   named `delivery` projection with raw_status/effective_status/successor plus
   validation/evidence/historical information. Independent metadata linking must
   also reconcile controller followups without falsely marking underlying
   workflow completed or dropping historical failures, attempts, STOP or elapsed.
   Revalidate links on read; invalid/missing/tampered successor evidence fails
   visibly and never asserts completion. Preserve old followup-link behavior.

## Meaningful verification

Add tests/test_recovery_escalation.py and tests/test_workflow_lineage.py. At least
16 meaningful new unittest cases total, no skips. Exercise real state transitions
and subprocess/CLI paths with isolated fake queue; don't merely assert helpers
return their own constants. Include the actual hub-resume-before-origin sequence,
STOP-before-second-claim, true expiry vs changed generation, exact unknown
delivery once-only behavior across controller restart/concurrent ticks, busy
owner/origin, response-without-progress still unresolved, supervisor exhausted
without handler, quiet internal followup vs real required decision, once-only
canonical question/answer security, accepted-question-independent link,
raw-blocked/effective-completed, idempotent link, tamper/cycle/dropped-acceptance/
grants/origin rejection, retained history and unaffected ACP/legacy behavior.

External host gate verify_acceptance.py categories causes/upgrade/regression/
delivery is immutable. All inherited tests byte-identical; full suite >=316,
0 failures/errors/skips. Use OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python.
Do not call providers, real queue, active controller tick, or actual Archify tests.

Provide docs/recovery-escalation.md, docs/recovery-escalation-migration.md and
build/recovery-escalation/validation-report.md with actual limits, new CLI use,
exact reasoned incident chronology and no false deployment or causal claim.
Prepare scripts/prepare_recovery_candidate.py --check and candidate.json:
runtime_activated:false, baseline_manifest_sha256, incident_sha256, relative files
dict pinning all new/modified implementation, docs and test files plus inherited
ACP integration. Never package task state, credentials or real private logs.
Rollout notes use versioned exact-source copy and safe controller cutover, preserve
current ACP SDK/local paths and Codex queue ingress guard compatibility. Do not
weaken notification policy to implement follow-through. Finish the whole candidate
and host-validation handoff, not merely an incident report.
