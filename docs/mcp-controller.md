# MCP-first ATO controllers

The distributed [ATO skill](../skills/ato/SKILL.md) now prefers MCP for controller
operations. It uses the existing API/MCP engine from baseline `9ab0bd5`; Worker and
Reviewer execution remains native. No new provider, scheduler, poller or webhook
was added. CLI and HTTP remain supported for operations and scripts.

Use the portable [setup/migration guide](../skills/ato/references/setup.md) for
host prerequisites, local stdio and remote Streamable HTTP, per-client official
sources (checked 2026-11-27), and future acceptance. The
[contract reference](../skills/ato/references/controller.md) supplies exact tool
arguments, envelopes, durable mutation recovery and authorized CLI mapping.

## Distributed artifacts

| Path under `skills/ato/` | Purpose |
| --- | --- |
| `SKILL.md` | Discover capability/aliases, submit once, inspect independent evidence, STOP/resume and route human input |
| `references/controller.md` | Tool/response contract, private durable intents, ambiguity and CLI fallback |
| `references/setup.md` | Client migration, official sources and future acceptance checklist |
| `templates/controller-calls.example.json` | Contract examples tested through the real service/SDK; replace sample IDs, keys and spec before use |
| `templates/server-stdio.example.json` | Local bot-only principal, avoiding human credentials in the controller process |
| `templates/server-http.example.json` | Host-only config with separate coordinator/human principals; never pass human token to clients |
| `templates/codex-stdio.toml`, `templates/codex-http.toml` | Official-field Codex configuration examples |
| `templates/cursor-stdio.json`, `templates/cursor-http.json` | Official-field Cursor agent configuration examples |

`install.sh --skills-dir PATH` copies the entire bundle for either normal or
editable installs; wheel data-files include the same files. Nothing auto-edits
client settings. `--server` / `.[server]` remain opt-in; the core dependency list
is empty. AGY secret injection and Grok Bot configuration are explicitly pending
manual checks in setup.md, not speculative runnable JSON.

## Qualification matrix

| Layer | Evidence | What is not established |
| --- | --- | --- |
| Existing native Worker/Reviewer adapters | Prior implementation retained; [Codex qualification](codex-mcp-validation.md) adds one bounded real Codex worker and independent marker check | Other providers/reviewers are not newly qualified |
| Service + official MCP SDK | Prior 126-test report plus [current regression](mcp-controller-validation.md); actual SDK in-process calls including packaged examples | Does not establish any native client UI/tool approval behavior |
| Actual loopback + stdio | Bounded HTTP/MCP socket and CLI stdio harness with temporary stores, synthetic credentials and intercepted dispatch | Public network, reverse proxy/TLS, deployment load, OS/power crash recovery |
| Codex CLI 0.171.0 controller | [Native qualification](codex-mcp-validation.md): actual stdio + loopback HTTP tool calls, exact replay, one real worker, synthetic STOP/resume and offline evidence verification | Requires an available mutation approval route; does not qualify Work/app, public TLS/proxy or push wakeup |
| Cursor controller config | Official schema + installed help; template syntax, install and SDK contract tests | Actual client connection not exercised |
| AGY controller | Official transport fields + installed help; manual setup checklist | Secure token reference/forwarding and actual connection not exercised |
| Grok Bot (`https://x.ai/bot`) | Product-specific official docs; manual capability checklist | Exact ATO-compatible client config/auth, durable keys, network access and actual connection not established |
| Native notification delivery | Existing origin-aware mechanisms preserved | New MCP/API task origin does not bind/wake a native session; polling is not push |

## Operating boundaries

Native `codex exec` defaults can reject mutation tools when approval policy is `never`.
The isolated qualification used supported `--approve-for-me` automatic review with
workspace-write sandboxing and temporary project trust; it did not bypass approvals.
See the [exact scope and failed initial attempt](codex-mcp-validation.md).

Controller credentials have read/control only. Human credentials stay in a
separate human-controlled channel; `ato_answer` records only actual authorized
input linked to the Task ID and question ID. Pending questions are not acceptance
and cannot be synthesized by bots. There are no MCP accept/recheck/cleanup/merge/
deploy tools. No secrets belong in specs, worker subprocesses or committed examples.

An RPC timeout never proves a write failed. Persist each new operation key before
sending, replay only that key and exact arguments, then poll fresh task facts.
`REQUEST_UNRESOLVED` requires host reconciliation, even if a reserved task ID
currently returns 404. Do not submit again through CLI or change principal/store
on denial or ambiguity. CLI fallback requires both unavailable MCP and existing
CLI authorization; explain the fallback and preserve task/evidence semantics.

Future migration: choose an approved deployment and client version, provision
aliases/credentials privately, import the bundle, verify discovery under read and
control scopes, then authorize a synthetic lifecycle exercise including replay,
STOP, resume and human routing. Keep existing task IDs/provenance and worker
configuration. Record actual client/network evidence before claiming deployment.
