# Capabilities and evidence levels

Capability registration describes a concrete implementation, not successful
qualification on every host. Every unsupported/unknown capability fails before
its possible effect. Evidence levels must remain distinct: offline protocol
fixtures, actual installed CLI with a scripted local model, real cloud model,
and real user decision. This candidate makes no cloud or real-user-approval claim.

| Adapter/route | Verified interface implementation | Explicit limits |
| --- | --- | --- |
| Codex compatibility session | Exact UUID index/header verification, queue receipt, bounded native observation/activity, app-server creation/readback | v2 actor authentication unknown; cancellation unknown; native load/resume unsupported. Activity may be unknown. Original v1 actor compatibility remains within its old API. |
| `pi_acp` managed session | Exact native identity/project; create, transport, observe, activity, cancel; private actor proofs and origin-only proof | Runtime requires Pi 1.5.2, pinned pi-acp 0.0.34, macOS sandbox and an explicitly owned foreground host. This profile only accepts loopback model data/dummy key. Native load unsupported; checkpoint reconstruction creates a new identity. |
| `local_cli` passive endpoint | Exact local identity/project, transport receipts, distinct private actor roles | No native model process, native create/cancel/load or observer. Activity remains unknown. A passive receipt is not proof that a human read or answered. |
| `codex`, `claude`, `pi`, `acp_pi` worker profiles | Existing worker/planner capabilities and policy checks | Worker success/session metadata does not imply coordinator support. Claude coordinator remains unsupported. |
| `fixture_*` adapters | Explicitly labelled synthetic subprocess behavior and fault injection | Cannot register themselves as real Pi or establish production credentials/authority. Never proof of cloud or actual CLI behavior. |

## Actors and boundaries

Hub may list/read/present. Only the distinct user principal can record a sourced
answer or manual acceptance; origin can ask/accept/continue for its exact workflow.
Session-host proof manages only its owned native lifecycle. Origin proof cannot
cancel or gain user authority. Root local keys mint only their fixed role;
workflow grants bind project, contract, revision and full workflow-start generation.
Endpoint secrets remain outside the model workspace and are never printed in
receipts. Environment variables locate those files; they authenticate nothing.
The trust model is a private local host, not isolation from a malicious root user.

All behavior suites remain in tests/. The required directed suites cover
contracts, Codex boundary, transport, recovery, managed Pi and neutral hub.
Final integration faults additionally cover account-context policy isolation,
terminal foreground revalidation versus recovery, stale notification no-send,
package tampering, bounded local-model protocol and safe evidence filtering.
No inherited test or fixture is edited or skipped to claim compatibility.

## Actual-Pi local-only host sample

The prepared [smoke script](../../scripts/verify_pi_session_local.py) is origin
host work, never a worker fallback or provider probe. It records installed Pi
version/binary hash and original/derived adapter hashes. The model server binds
numeric loopback only and responds with a fixed six-response file-tool transcript.
The native OS profile allows only that loopback port; model tools have no shell
or network capability. All homes are fresh, native auth is absent, provider key
is a literal dummy, and Codex command/index/app-server access fails closed.

It demonstrates two correlated turns on one native session, real read/write
file tools, CLI hub question/display/synthetic-user answer/origin receipt,
explicit new-session reconstruction, a third correlated turn, independent file
checks and workflow checks. Sanitized per-step, event, tool/model and artifact
receipts survive outside the deleted temporary private runtime. It does not test
model intelligence, cloud reliability, private auth, native load, malicious-root
isolation, arbitrary tools, production side effects or user decisions.

Successful qualification requires exact native tool-call/result linkage and a
successful file-tool status on each original/reconstruction turn. Each turn's
typed native observations must also settle its original canonical mailbox entry;
unrelated old tool output, failed tool events or native end_turn cannot replace
those receipts. The host records both bounded tool status and independent file
checks without retaining model/tool bodies or credentials.

Current observed counts and host-pending state are in [validation.md](validation.md).
The external verifier binds a successful host proof to the exact candidate
manifest hash. Source changes after that sample require origin review; they do
not silently grant another sample or permission to replay any uncertain action.
