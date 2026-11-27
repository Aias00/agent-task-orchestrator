# Deploying the MCP and HTTP entrypoints

ATO can expose its existing engine to a trusted external coordinator, including a bot
running elsewhere. `ato serve` provides REST at `/api/v1` and **MCP Streamable HTTP at
`/mcp` on the same listener**. `ato mcp --stdio` is a local process alternative.
Both call the same authorization/service/journal layer and existing executor, spec,
product and inbox operations. They do not introduce another scheduler.

Controllers should follow the [MCP-first skill](../skills/ato/SKILL.md) and
[client migration guide](mcp-controller.md). HTTP/CLI remain supported for
operators and scripts. Controller transport does not replace native Worker or
Reviewer adapters. New `api`/`mcp` origins do not bind or wake native conversations;
MCP polling is not push delivery. Existing native notification mechanisms remain.

Implementation status: **isolated validation passed on 2026-11-27**: 126 core,
service/journal and real SDK tests, plus actual loopback HTTP/MCP and stdio checks.
See [validation results and limits](mcp-api-validation.md). Optional dependencies
were installed only in the validation worktree's private venv. No installed ATO
service was changed, Grok Bot connected, webhook sent or public service deployed.
Deployment commands below remain instructions for a later deployment environment.

## Host prerequisites and optional installation

Use Python 3.12+, Git, a persistent local filesystem, and the selected native worker
CLI with its own credentials. Keep the service account, allowed repositories and
`ATO_HOME` on the same host. SQLite state is not a distributed queue: do not put it
on NFS, run independent replicas with copied journals, or autoscale stateless workers.
Use one service process/listener and a persistent `ATO_HOME` for deployment v1.

The core CLI remains dependency-free. Serving uses the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), specifically
its 2.3.x low-level Server, StreamableHTTPSessionManager, and stdio transport APIs;
the implementation was tested with official PyPI SDK 2.3.0.
The extra pins that SDK minor series because its API differs from SDK 1.x.
Starlette and uvicorn are also optional server dependencies. No faux MCP protocol
or custom JSON-RPC handshake is implemented.

On the deployment host, from this checkout, choose one installation:

```sh
# Existing project installer, preserving its normal dependency-free behavior unless opted in:
sh ./install.sh --server
# Or a development installation:
sh ./install.sh --editable --server
# Or a conventional dedicated Python environment:
python3.12 -m venv /srv/ato/venv
/srv/ato/venv/bin/python -m pip install '.[server]'
```

The `--server` installer path installs the actual project extra into the launcher
venv. Missing/incompatible dependencies produce a concise install instruction from
`ato serve` / `ato mcp`; ordinary CLI commands do not import the serving SDK.

OS support is the **existing adapter's** support: core Codex workflows can run on
Linux; Claude, Cursor, Pi and AGY sandbox-based paths require macOS
`/usr/bin/sandbox-exec`. Publishing an HTTP endpoint does not make those adapters
Linux-compatible. ACP/acpx requires its separately configured launcher and cleanup
support; see [acpx-reviewer.md](acpx-reviewer.md). Install native tools and provision
worker credentials separately. Preserve workspace-write defaults, isolated Git
worktrees, host checks, reviewer mutation detection and PID-safe STOP cleanup.
The API does not relax any of those rules or the retry/reviewer state machine.

## Explicit configuration and trust boundary

Copy [server-config.example.json](server-config.example.json) to a private host
configuration file and replace repository paths and hostname. Never put token
values into the JSON. Repository aliases are the only client-facing selectors;
roots are canonicalized and pinned at startup. Each principal is allowed specific
aliases and scopes. Duplicate roots and duplicate credentials are rejected.

| Scope | Operations |
| --- | --- |
| `read` | repositories, list, status, diff, trace, task inbox |
| `control` | validate, submit, stop, resume |
| `human` | explicit question answer |

Use independent strong bearer credentials, at least 32 ASCII characters, supplied
by your host's secret manager/environment. Each `token_env` must start with
`ATO_SERVER_TOKEN_`. **All configured token variables must be present at startup**,
including when using stdio. An absent token fails startup, rather than creating an
unauthenticated principal. For example, provision `ATO_SERVER_TOKEN_BOT` and
`ATO_SERVER_TOKEN_HUMAN`; give only the bot variable's value to the coordinator.
Rotate credentials by changing the host environment and restarting the service;
keep principal names stable to preserve idempotency ownership. Do not recycle an
old principal name for an unrelated identity.

`control` and `human` on the same principal is rejected. The bot cannot answer a
human question, record acceptance, or upgrade its scopes. Human answers require
explicit human intent and the separate human token; model-generated text is not
human authorization. Answering does not resume, accept, merge or deploy a task.

**Submission is a trusted-controller capability, not a safe public job form.**
An inline spec authorizes arbitrary task/check execution through the existing
engine. This includes executable acceptance commands and worker instructions.
The allowlist protects API selection and observation, not a multi-tenant OS
security boundary. A controller entrusted with code execution under this service
account can potentially affect other resources that account can access. Separate
OS accounts/hosts are required for untrusted tenants. Do not expose controller
tokens to arbitrary users, public chat messages, or an untrusted agent.

Inline `spec.repo` must equal the requested alias exactly, e.g. `demo`, never a
path, `..` or another repository. Existing spec validation applies to all nested
fields, design documents and shared links. Additional `permissions.writable_roots`
are rejected by the transport. The service does not accept file paths for specs,
evidence, traces or arbitrary reads, shell-dispatch parameters, cleanup, merge,
deploy, reviewer changes, accept, or recheck. **Recheck executes checks and is out
of API v1 scope.** Normal submit/resume still run the spec's configured checks.

The subprocess environment excludes transport token variables, copies containing
their values, and inherited Codex/origin binding variables. New transport tasks
record explicit `api` or `mcp` origin with no Codex session binding. Resume/STOP/
answer record transport-operation events; existing tasks retain their original
submission provenance. CLI origin detection stays unchanged. The service does not
return host configuration or credentials; task output/diffs/check history may
contain repository-sensitive material, so read tokens also require care. Storing
credentials in task prompts/specs is unsupported.

## Listener, TLS proxy and request policy

```sh
export ATO_HOME=/srv/ato/state
export CODEX_HOME=/srv/ato/codex
# Inject ATO_SERVER_TOKEN_BOT and ATO_SERVER_TOKEN_HUMAN from your secret manager.
ato serve --config /srv/ato/server.json
# Defaults: --host 127.0.0.1 --port 8765
```

Use a service supervisor on the deployment host to restart this command; persist
its environment/config and ATO state privately (directory 0700, files 0600).
Changing the port requires updating `allowed_hosts` as well. Binding a non-loopback
address requires an explicit `--host` and appropriate host firewall restrictions.
Use HTTPS for any public connection. A representative nginx server block is:

```nginx
server {
    listen 443 ssl;
    server_name ato.example.com;
    ssl_certificate /etc/letsencrypt/live/ato.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ato.example.com/privkey.pem;
    client_max_body_size 1m;
    client_body_timeout 15s;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host ato.example.com;
        proxy_set_header Authorization $http_authorization;
        proxy_buffering off;
        proxy_read_timeout 120s;
    }
}
```

Configure the proxy's default virtual host to reject unknown names. Keep the
backend inaccessible from public networks. `allowed_hosts` contains exact incoming
Host values (including non-default ports), no wildcard. `allowed_origins` defaults
to empty: requests **without** Origin, such as native bot/SDK clients, are accepted;
requests **with** Origin must match an explicit full origin. Do not use `*` or
`null`. Add a trusted browser origin only deliberately; v1 supplies no browser
CORS/preflight integration. Uvicorn forwarding-header trust is disabled; the proxy
must preserve or set the explicitly allowed Host. Configure proxy rate limits and
connection limits suitable for the installation.

The auth/Host/Origin boundary covers REST, `/health`, the schema and every MCP
request (including initialize). Duplicate sensitive headers are rejected. Request
bodies are bounded for both Content-Length and streamed/chunked input before JSON
parsing, with a 15-second receive timeout and at most 64 active server connections.
Set `max_body_bytes` between 1 KiB and 4 MiB (default 1 MiB); inline specs also have
the existing 1 MiB limit. SDK and HTTP access logging are disabled by the supported
launcher to avoid request/credential disclosure. Do not enable SDK debug logging,
proxy body logging, or Authorization-header logging. `/health` is authenticated
liveness only, not a provider, worker or functional qualification check.

## REST workflow

All business results use the existing envelope:

```json
{"schema_version":1,"ok":true,"data":{"task_id":"task-0123456789abcdef0123456789abcdef","status":"queued"},"error":null}
```

Errors use `ok:false`, `data:null`, and `error:{code,message,task_id}`. Messages do
not echo request text or bearer values. HTTP mappings: 400 invalid input, 401
missing/invalid credential, 403 missing scope/policy denial, 404 unavailable task
(including a wrong repository), 405 method, 409 state conflict/unresolved intent,
413 body too large, 500 internal error. Submit and resume return 202 after local
admission/background launch; no worker execution is awaited by the request thread.
Git worktree setup/admission can take time. Poll the task ID for **current facts**;
a 202, model response or transport delivery is not task completion.

Create `request.json` (your actual objective and acceptance replace these examples):

```json
{
  "repo": "demo",
  "spec": {
    "schema_version": 1,
    "repo": "demo",
    "base_ref": "HEAD",
    "worker_contract": {
      "objective": "Implement the agreed feature",
      "scope": "Only the agreed repository files",
      "authorization": "Worktree changes and the specified check are authorized",
      "acceptance": [{
        "id": "whitespace", "kind": "command",
        "description": "No whitespace errors",
        "checks": [["git", "diff", "--check"]]
      }]
    }
  }
}
```

Then, in the later deployment environment:

```sh
export ATO_URL=https://ato.example.com
curl --fail-with-body -H "Authorization: Bearer $ATO_SERVER_TOKEN_BOT" \
  "$ATO_URL/api/v1/repositories"
curl --fail-with-body -H "Authorization: Bearer $ATO_SERVER_TOKEN_BOT" \
  -H 'Content-Type: application/json' --data-binary @request.json "$ATO_URL/api/v1/validate"
curl --fail-with-body -H "Authorization: Bearer $ATO_SERVER_TOKEN_BOT" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: feature-20261127-1' \
  --data-binary @request.json "$ATO_URL/api/v1/tasks"
# Set TASK_ID to the returned data.task_id, then poll:
curl --fail-with-body -H "Authorization: Bearer $ATO_SERVER_TOKEN_BOT" \
  "$ATO_URL/api/v1/tasks/$TASK_ID?check_history=true"
```

| Method | Path | Arguments besides path IDs |
| --- | --- | --- |
| GET | `/api/v1/repositories` | none |
| POST | `/api/v1/validate` | JSON `repo`, `spec` |
| POST | `/api/v1/tasks` | JSON `repo`, `spec`; Idempotency-Key |
| GET | `/api/v1/tasks` | optional query `repo` alias |
| GET | `/api/v1/tasks/{task_id}` | optional query `check_history=true\|false` |
| GET | `/api/v1/tasks/{task_id}/diff` | optional query `stat=true\|false` |
| GET | `/api/v1/tasks/{task_id}/trace` | none; bounded existing event timeline |
| GET | `/api/v1/tasks/{task_id}/inbox` | none; open questions for this task only |
| POST | `/api/v1/tasks/{task_id}/stop` | empty body or `{}`; Idempotency-Key |
| POST | `/api/v1/tasks/{task_id}/resume` | optional JSON `timeout_seconds` integer 5..7200; Idempotency-Key |
| POST | `/api/v1/tasks/{task_id}/questions/{question_id}/answer` | JSON `answer`; human token and Idempotency-Key |

Path IDs must not also occur in the body; REST mutation keys appear only in the
`Idempotency-Key` header. Unknown fields/query parameters and duplicate JSON keys
are rejected. The authenticated machine-readable OpenAPI 3.1 schema is
`GET /api/v1/openapi.json`, generated from the actual route and operation manifests.

## MCP connection and tools

Configure the external coordinator with transport **Streamable HTTP**, URL
`https://ato.example.com/mcp` (no trailing slash), and header
`Authorization: Bearer <the bot token obtained securely from the environment>`.
The endpoint uses the SDK's stateless JSON-response mode; initialize/tools/list/
tools/call are supported by the SDK. Each request must carry credentials; no MCP
session ID grants authority. Per-principal SDK instances prevent identity crossover.
A JSON-RPC message ID is **not** an operation idempotency key.

The tools are `ato_repositories`, `ato_validate`, `ato_submit`, `ato_list`,
`ato_status`, `ato_diff`, `ato_trace`, `ato_stop`, `ato_resume`, `ato_inbox`,
`ato_answer`. `tools/list` shows only tools permitted by the principal's scopes;
direct calls are independently authorized. Inputs match REST fields, but task and
question IDs are tool arguments and mutations require a `request_id` argument.
Structured results contain the canonical envelope; the text block contains the
same JSON. Business errors have `isError:true` plus the structured error envelope.
SDK protocol errors retain the SDK's JSON-RPC behavior.

Official SDK 2.3.x client example, including initialization (its HTTP client is
`httpx2`, and the stream context returns two streams). Python result attributes use
snake_case (`structured_content`, `is_error`, `server_info`); JSON wire fields
retain their MCP camelCase names. Before any write, durably prepare a private
`private-submit-intent.json` containing `repo`, `spec` and a fresh UUID4-based
`request_id`. Reuse that exact file/key after uncertain delivery; never generate a
key inside a retry loop. The [controller recovery contract](../skills/ato/references/controller.md)
also requires retaining deployment/principal identity, task ID and outcome. This
short SDK example does not implement a durable controller or background poller:

```python
import asyncio
import json
import os
from pathlib import Path
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with httpx2.AsyncClient(headers={
        "Authorization": "Bearer " + os.environ["ATO_SERVER_TOKEN_BOT"]
    }) as http:
        async with streamable_http_client(
            "https://ato.example.com/mcp", http_client=http
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                print(await session.list_tools())
                print(await session.call_tool("ato_repositories", {}))
                intent = json.loads(Path("private-submit-intent.json").read_text())
                request = {key: intent[key] for key in ("repo", "spec")}
                validation = await session.call_tool("ato_validate", request)
                if validation.is_error or not validation.structured_content["ok"]:
                    raise RuntimeError("Validation failed; inspect the error envelope")
                result = await session.call_tool("ato_submit", intent)
                envelope = result.structured_content
                if not envelope["ok"]:
                    raise RuntimeError(envelope["error"]["code"])
                task_id = envelope["data"]["task_id"]
                print(await session.call_tool("ato_status", {"task_id": task_id}))
                # Continue polling; do not treat this reply as completion.

asyncio.run(main())
```

For local stdio, explicitly set `stdio_principal` in the host configuration:

```sh
ato mcp --stdio --config /srv/ato/server.json
```

The launching OS process is the authentication boundary; its configured principal
is fixed for the entire connection and all scopes/repo checks still apply. Do not
bridge stdio to an unauthenticated network listener. There is no client argument
to choose/elevate a principal. CLI errors go to stderr and stdout is reserved for
MCP. Stdio framing is handled by the official SDK; HTTP receive/header limits do
not apply to a local pipe, while service argument-size limits still apply.

## Idempotency and recovery

Every submit, resume, stop and answer requires a key (1..128 characters matching
`[A-Za-z0-9][A-Za-z0-9_.:-]*`). REST and MCP share a private
`$ATO_HOME/v2/transport-requests.db` journal. Its identity is `(principal name,
SHA-256(key))`; the canonical argument digest includes the operation and pinned
repository binding, excludes transport origin, and normalizes default boolean
arguments. It stores no bearer token or raw request arguments. Returned task
results are stored privately to allow byte-equivalent JSON-value replay.

The journal serializes admission with a SQLite transaction and FULL durability,
records an unresolved intent **before** any mutation, then commits the result.
Concurrent duplicates cannot execute twice. A finished duplicate returns the
original result, which may be stale: use status for current execution facts.
Changing operation/arguments with the same key returns `IDEMPOTENCY_CONFLICT`.
Authorization is checked again before serving any cached task result. Inline
validation of a new request occurs before intent persistence; a cached submit does
not re-resolve a moving HEAD. The same key submitted via REST and MCP deduplicates.

A duplicate while the first call is running, a process crash after recording
intent, or an exception after mutation starts returns `REQUEST_UNRESOLVED` (409),
with the reserved/known task ID when available. It is intentionally conservative:
even a rejection from an engine operation after intent creation is unresolved;
the service does not infer that no effect occurred. **There is no exactly-once
claim across process/OS crashes.** There is no lease expiry or blind auto-replay.
A client disconnect does not undo an admitted operation.

Recovery is an operator decision: poll the returned task ID, inspect task trace,
run/worktree/process state and the private journal. A reserved submit ID may not
yet exist in the task store if the crash preceded task creation; a worktree may
already exist if the crash happened between worktree creation and task persistence.
Do not treat 404 as proof of no effect. Wait for any in-flight operation to finish.
If the outcome stays unresolved, reconcile locally before deciding whether a
**new** key is appropriate; do not delete/change journal rows to trigger replay.
There is no remote journal-reset or cleanup endpoint. Keep the journal as long as
clients might retry its keys; backup it together with ATO state. Key retention and
human recovery tooling are not automated in v1.

There is no webhook/push integration yet. Poll task status and task inbox. A model
saying “done”, an accepted request, and successful HTTP/MCP delivery are different
from the engine's durable final status and its independent check/reviewer evidence.

## Repeating isolated validation

All fixtures use temporary ATO_HOME/CODEX_HOME and synthetic Git repositories;
worker dispatch is intercepted and no external provider is contacted. In an
authorized test checkout with Python 3.12+:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[server]'
.venv/bin/python scripts/run_tests.py --server
.venv/bin/python scripts/check_server_transport.py
git diff --check
```

Missing SDK dependencies fail the optional suite rather than silently skipping it;
the runner prints separate discovery counts and rejects skips or empty server
discovery. `python3.12 scripts/run_tests.py` still runs the dependency-free suite.
The optional unit suite uses ASGI in process. The separate transport script uses
a real uvicorn socket bound only to `127.0.0.1` on an ephemeral port and the official
MCP client, then the actual `ato mcp --stdio` CLI in a child process. It has a
90-second operation deadline plus bounded cleanup, asserts child exit and listener
closure, and returns a nonzero exit status on failure. Its synthetic dispatch
records an event and leaves tasks queued; it never executes a provider worker.
Tests cover
credential failures, scope and cross-repo isolation, human separation, inline
specs, async dispatch/origin/environment, persistent collision/restart/concurrent
intents, HTTP routes/errors/limits and real SDK tool calls. Only the recorded local
dependency/platform combination is qualified. Public HTTPS/proxy behavior, Grok Bot,
real model execution and the selected deployment host remain unqualified.
