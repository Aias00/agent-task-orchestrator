# MCP/HTTP API validation — 2026-11-27

Source baseline: `8097b8231e036b43a09b9be92b9e257860d29ef5`, from
`task-1223e83eb83d43ff88dd228f71f9c863`. Validation and fixes belong to isolated task
`task-6c07373b88724cf389b726d78060d0ee`; the original implementation branch is intact.
This task explicitly authorized tests, superseding the original implement-only instruction.

## Environment and commands

macOS arm64, Python **3.12.15**. Private ignored `.venv`, editable ATO **2.1.0**,
official PyPI **mcp 2.3.0 / mcp-types 2.3.0**, **httpx2 2.16.2**, **httpcore2 2.16.2**,
**anyio 4.16.0**, **starlette 0.52.1**, **uvicorn 0.54.0**, **pydantic 2.14.4**,
**sse-starlette 3.7.1**. `pip check` found no broken requirements. The full resolved
dependency list is in ignored evidence. No SDK substitution, dependency downgrade,
global installation or core runtime dependency was needed.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple -e '.[server]'
.venv/bin/python -m pip check
.venv/bin/python scripts/run_tests.py --server
python3.12 scripts/run_tests.py
.venv/bin/python scripts/check_server_transport.py
git diff --check
```

| Check | Actual outcome |
| --- | --- |
| Baseline full suite | 118 tests: 116 passed, 2 errors, 0 skips |
| Final full suite | **126 passed**, 0 failures/errors/skips; 116 core/service/journal + 10 optional `tests/server` tests |
| Core without `--server`, Python 3.12 | **116 passed**, 0 failures/errors/skips |
| Real loopback HTTP + official MCP client | Passed; 2 synthetic admissions, no native worker/provider invocation |
| Actual stdio CLI + official MCP client | Initialize/list/read calls passed; child exited with code 0 |
| Shutdown / whitespace | Listener closed, child reaped; `git diff --check` passed |

## Findings and fixes

The two baseline SDK tests failed because they read JSON wire names as Python
attributes. Official SDK 2.3 uses `server_info`, `structured_content`, `is_error`.
Corrected the tests and runnable documentation example, preserving all assertions.
The server's Pydantic constructors already accept wire aliases; HTTP and stdio
runtime code needed no change. The SDK pin remains `>=2.3,<2.4`.

Added 8 meaningful regression cases and explicit discovery counts/empty-server
discovery failure. Editable install metadata is now ignored. Existing tests,
STOP rules, retry/reviewer semantics and independent checks remain intact.

The new smoke harness initially shared the same attribute mistake; corrected it.
Its unauthorized-initialize probe also initially expected `HTTPStatusError`, while
SDK 2.3 actually maps HTTP failures to `MCPError(-32603)`. It now asserts that SDK
error **and independently observes HTTP 401 with the canonical `AUTH_REQUIRED`
envelope**. A direct optional unittest invocation initially omitted `PYTHONPATH=tests`;
the corrected focused invocation passed. These harness/invocation errors were not
server defects; original failure logs are retained.

## Coverage and isolation

- Service/journal: inline validation before intent persistence, path/extra-field/
  writable-root rejection, canonical results/errors, principal scopes and repository
  isolation, explicit human answers without resume/accept, asynchronous admission,
  neutral API/MCP origins and scrubbed token copies/origin bindings.
- Durable idempotency: both transport directions, collisions, concurrent requests
  returning unresolved while the first effect runs, persistence after service/journal
  reconstruction, simulated crash and post-mutation exception, no blind replay,
  independent principal keys and reauthorization after scope/repository revocation.
- Optional real SDK tests: initialize/list/call, per-principal concurrent sessions,
  all REST task routes, cross-repository list/status/diff/trace/inbox isolation,
  auth-before-body, malformed/duplicate headers, JSON/UTF-8/nonfinite rejection,
  length mismatch, streamed limits, Host/Origin policy and receive-timeout error mapping.
  Receive-timeout mapping uses an injected timeout, not a timed slow-client socket test.
- `scripts/check_server_transport.py`: actual uvicorn TCP listener and official MCP
  client; REST health/validate/submit/status; initialization auth failures; scoped
  tools and calls; human separation; shared submit/answer replay and key collisions;
  Content-Length and chunked body limits; Host/Origin rejection; actual read-only
  stdio CLI initialization/list/repositories/status/trace/inbox.

All fixtures use temporary ATO_HOME/CODEX_HOME and synthetic repositories and
credentials. The socket harness also replaces HOME and clears inherited environment,
disables proxy inheritance, and binds only `127.0.0.1:0`. Actual spec normalization,
worktree creation, task/run persistence and journal operations execute. Only
`_spawn_background_run` is replaced with a synthetic event recorder; `execute_run`
is guarded against invocation. Admissions remain queued. Stdio uses a read-only
principal and launches the actual CLI; an observer retains its real SDK-spawned
process handle to assert exit. Git subprocesses are waited, the stdio child and
listener are cleaned in `finally` blocks with deadlines, and temporary stores are removed.

## Evidence and limits

Private ignored evidence directory: `.verification/mcp-api-validation/`.
Key files: `baseline.txt`, `install.log`, `dependencies.txt`, `pip-check.log`,
`suite-baseline.log`, `suite-final.log`, `core-python312.log`,
`transport-final.log`, `diff-check.log`. Focused/development logs retain intermediate
failures and reruns. No native history or real credentials are included.

This qualifies the local transport contract and synthetic core regression only.
It does **not** qualify Grok Bot, webhooks, public HTTPS/TLS/proxies, production
load, real provider/model execution, crash recovery across OS/power failure, or
other OS/dependency versions. No installed ATO/skills update, main-branch change,
merge, push or deployment was performed. The host must independently rerun acceptance.
