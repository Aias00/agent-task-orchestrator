# MCP controller setup and migration

Templates are examples for a later authorized installation, not active client
configuration. Replace paths, aliases and hostnames; supply secrets through the
client/host's private environment or credential facility, never prompts or Git.
Codex CLI was subsequently qualified in an isolated manual exercise; see the
client matrix below. Other native controller connections remain untested.

## Host and distribution

Core ATO remains dependency-free; MCP serving requires Python 3.12+ and the optional
`.[server]` extra (official MCP SDK 2.3.x). From the selected release checkout:

```sh
# Later host setup only, not a command to update a live installation implicitly:
sh ./install.sh --server --skills-dir /path/to/approved/skills
# Or a private development environment:
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[server]'
```

`--skills-dir` copies SKILL.md plus references/templates. It does not edit client
settings. A wheel includes the same tree under
`share/agent-task-orchestrator/skills/ato`. Copy/import the whole tree when a client
supports skills, or explicitly attach SKILL.md and controller.md as instructions;
these are not a promise that every client auto-discovers Codex skill locations.

For stdio copy `../templates/server-stdio.example.json` to a private host file,
map `demo` to an existing Git root, and provision `ATO_SERVER_TOKEN_BOT` (at least
32 non-whitespace ASCII characters). Set explicit private ATO_HOME/CODEX_HOME.
The local launching process fixes `stdio_principal=coordinator`. Even stdio
requires every token_env declared in its config at startup. This bot-only config
avoids giving the controller a human credential. Do not copy a config containing
human credentials into a bot's process. Human answers use a separate authorized
human process/channel; bots only relay linked questions.

```sh
/srv/ato/venv/bin/ato mcp --stdio --config /srv/ato/server-stdio.json
```

For remote Streamable HTTP, deploy `ato serve --config /srv/ato/server.json` on the
ATO host with persistent state, repository allowlists and separate bot/human
principals. Use [the host-only HTTP config](../templates/server-http.example.json) and
`docs/mcp-api.md` for host/Origin policy, limits and HTTPS proxy setup. Client URL
is `https://ato.example.com/mcp`, no trailing slash; send bearer auth on every
request, including initialization. This is not legacy SSE or OAuth. Remote
clients do not need ATO installed; the ATO host needs its native worker CLIs.
Only the host service holds all configured tokens. Worker environments scrub
transport tokens and copies as well as inherited native origin bindings.

## Client recipes and evidence (checked 2026-11-27)

| Controller | Recipe | Evidence / remaining qualification |
| --- | --- | --- |
| Codex CLI | Merge ONE of `codex-stdio.toml` / `codex-http.toml` into the approved config.toml | Codex CLI 0.171.0 actual stdio and loopback HTTP qualified; exact replay, one real worker and synthetic lifecycle; mutation approval route required |
| Cursor agent | Merge ONE of `cursor-stdio.json` / `cursor-http.json` into the approved `.cursor/mcp.json` | Official docs and installed `cursor-agent mcp --help`; native connection untested |
| AGY / Antigravity | Manual setup below, no guessed secret interpolation | Official fields + installed CLI help; credential injection and native connection pending |
| Grok Bot at x.ai/bot | Manual capability checklist below | Product-specific docs confirm custom MCP context; exact ATO-compatible client schema/auth and connection pending |

Codex supports `command`, `args`, `env`, `env_vars` for stdio and `url` plus
`bearer_token_env_var` for HTTP. See [official MCP configuration](https://developers.openai.com/codex/mcp)
(redirects to [ChatGPT Learn, CLI surface](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)).
Local help also confirms `codex mcp add NAME --url URL --bearer-token-env-var NAME`
and `codex mcp add NAME -- COMMAND ARGS`. The initial bundle qualification read
help only. The subsequent Codex qualification used isolated `codex exec` processes
and these templates for actual stdio and loopback HTTP tool calls. See the source
checkout's `docs/codex-mcp-validation.md` for the report and private evidence paths.

In Codex CLI 0.171.0, default noninteractive `exec` rejected ATO submit because its
approval policy was `never`; no task was admitted. The tested route uses native
`--approve-for-me` automatic approval review with workspace-write sandboxing.
For an approved disposable qualification only:

```sh
codex exec --strict-config --json --ephemeral --approve-for-me --trust -C TEMP_REPO -
```

Use private CODEX_HOME/ATO_HOME and provision only the bot credential first.
`--trust` persists trust, so this example is limited to the temporary client/repo;
it is not an instruction to trust a real project or change global settings.
Interactive setups can use normal human tool approval. An unavailable/rejected
approval route remains a blocker; do not use bypass flags. The harness bounds
controller calls to 240 seconds and the single real worker to 120 seconds.
Qualification does not establish the current Work/app session, public TLS/proxy,
other clients, a real human answer, or native push wakeup. No add/login command or
live client settings mutation was used.

Cursor supports environment interpolation in `env` and `headers`; examples use
`${env:ATO_SERVER_TOKEN_BOT}`. See [official MCP config](https://cursor.com/docs/mcp)
and [CLI usage](https://cursor.com/docs/cli/using). Its local CLI help identifies
`.cursor/mcp.json` / `~/.cursor/mcp.json` and `mcp list-tools NAME`. A future operator
can inspect those tools after setup. Desktop/CLI recipes do not establish hosted
Cloud Agent support; hosted configuration is managed separately.

AGY's [official MCP guide](https://antigravity.google/docs/mcp) documents
`mcpServers`, local `command`/`args`/`env`, remote `serverUrl`/`headers`, and the
Manage MCP Servers → View raw config workflow. Installed `agy mcp add --help`
confirms `--type stdio|http`, `--env KEY=value`, `--header 'Key: Value'`, with flags
BEFORE the name. Do not put bearer values in shell arguments/history. Official
material inspected here does not establish a secret-reference interpolation
syntax, so no `${env:...}` AGY recipe is claimed. In a later approved setup:

1. Open the actual client's MCP manager/config location; verify version/surface.
2. For stdio choose the ATO executable and `mcp --stdio --config` arguments above;
   securely provision only the bot token and the two host directories to the
   launched process. Confirm the client's environment forwarding mechanism.
3. For HTTP use `serverUrl=https://ato.example.com/mcp`, with a privately managed
   Authorization header supplied by a verified secret facility. If none exists,
   leave this integration pending; do not hardcode secrets into shared templates.
4. Initialize/discover and inspect scope, then follow the acceptance checklist.

Grok Bot means [the actual Bot product](https://x.ai/bot), not Grok Build, Grok
chat connectors or an xAI Responses API wrapper. Its [security guide](https://docs.x.ai/grok-bot/security)
describes connected apps reaching MCP under Bot-specific policy; Cursor editor
settings do not establish Bot configuration compatibility. Its [template guide](https://x.ai/bot/guides/templates-for-grok-bot)
warns that custom MCP setup is not automatically carried with a shared Bot template.
These sources do not establish a runnable ATO bearer configuration. **Pending**:
confirm the actual Bot account supports custom Streamable HTTP, secret header
injection, tools/list/call, structured/text error handling and durable request
records. Verify approved HTTPS reachability, Bot-specific network/approval policy
and a read/control-only principal. Never give a Bot the human token or disable
its approval controls. No Bot login, call, script wrapper, tunnel or deployment
was performed. If capabilities are missing, report the gap and retain an already
authorized operator workflow; do not invent a Bot config or silently use an API.

## Migration and future acceptance

1. Retain existing Task IDs, frozen specs, state/journal and native provenance.
   Inventory which tasks are running before changing the controller transport.
   Do not resubmit them; read them through the authorized alias and task ID.
2. Keep Worker kinds `codex`, `claude`, `cursor`, `pi` and optional read-only
   Reviewers `codex`, `claude`, `agy`, `codex_acpx` as currently configured.
   Codex supports macOS/Linux; sandbox-exec adapters require macOS. MCP does not
   turn AGY or Grok Bot into a new worker or replace ACP/native execution.
3. Select one connection, provision read/control only, import controller
   instructions, initialize and inspect actual tools/schemas and repositories.
   Test a read principal separately; missing control tools should stay missing.
4. In a separately authorized synthetic repository, validate and submit with a
   durable key, reconnect/replay the SAME key, then poll status/diff/trace/inbox.
   Verify one task/admission, independent checks and final pending/review facts.
   Exercise STOP cleanup and eligible resume, preserving Task ID. Route a question
   to a real authorized human channel; verify a bot cannot answer it.
5. Verify client restart preserves keys/IDs; inspect unknown delivery handling,
   network/proxy/TLS, credential rotation and future host recovery procedures.
   Record client versions/results separately from official SDK transport tests.

New MCP/API submissions have `origin=mcp/api`, no native session binding, and
completion notification `unsupported`. Polling only supplies facts during an
active controller turn; it is not push delivery and does not wake a conversation.
The existing Observer can still notify verified native Codex origins; unbound
Codex remains unbound, AGY remains unsupported. MCP stop/resume of an existing
native-origin task preserves its origin. Do not forge a binding to recover a
notification, replace native notification mechanisms, or promise background
follow-up when this controller is not running.
