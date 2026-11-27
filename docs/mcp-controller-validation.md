# MCP controller qualification — 2026-11-27

Task: `task-8701f5b6776d44999c67f1bad0dbd8b8`, isolated baseline
`9ab0bd5aaf26d2b93a34d3cb0c52941ffd026173`. This continues implementation task
`task-1223e83eb83d43ff88dd228f71f9c863` and validation task
`task-6c07373b88724cf389b726d78060d0ee`. Reviewed main's docs-only `39daf17` for
supported-agent terminology; did not merge it. Current task explicitly authorized
private dependencies and synthetic tests. No global runtime/client/skill install,
provider call, notification, deployment, push or merge was performed.

## Change and evidence

Controller behavior is implemented in the distributed skill/references/templates;
existing runtime transports, Worker/Reviewer adapters, retries, environment
scrubbing, notification machinery and PID-safe cleanup were not changed. Installer
and wheel manifests now ship the complete bundle. The qualification matrix and
future client migration steps are in [mcp-controller.md](mcp-controller.md).

Four new core cases check call examples against real service validation, bot-only
stdio and separate HTTP principals, client template parsing/secret references,
and wheel manifest/link completeness. The existing installer regression now
compares every bundled file after both editable and normal isolated installs.
One new official SDK case executes the distributed examples through MCP:
initialize/discover, repositories, validate, submit, reconnect/same-key replay,
list/status/diff/trace, STOP, linked inbox, bot answer denial, separate synthetic
human answer and eligible resume. It verifies one submission, no implicit resume,
unchanged spec digest, timeout override, neutral MCP origin and no native binding.
Dispatch is intercepted and never invokes a provider.

## Environment and actual commands

macOS arm64, Python 3.12.15; this worktree's ignored `.venv`, editable ATO 2.1.0.
Official PyPI mcp/mcp-types 2.3.0, httpx2 2.16.2, Starlette 0.52.1, uvicorn 0.54.0,
anyio 4.16.0, pydantic 2.14.4. Existing pip caches were available; dependency
installation used official PyPI. `pip check` found no broken requirements.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple -e '.[server]'
.venv/bin/python -m pip check
python3.12 scripts/run_tests.py
.venv/bin/python scripts/run_tests.py --server
.venv/bin/python scripts/check_server_transport.py
git diff --check
```

| Final check | Result |
| --- | --- |
| Core/service/journal | 120 passed, 0 failures/errors/skips |
| Core plus official SDK suite | 131 passed: 120 core/service/journal + 11 optional SDK tests; 0 failures/errors/skips |
| Actual loopback REST/MCP | Passed, 2 synthetic admissions, 0 native provider/worker calls |
| Actual stdio CLI | Initialize/list/read passed, child exit 0; HTTP listener closed |
| Installer distribution | Editable and normal installs in temporary directories include all 10 bundle files byte-for-byte |
| Wheel distribution | Built with `pip wheel --no-deps`; all 10 bundle files matched archive contents byte-for-byte |
| Whitespace | `git diff --check` passed |

The first suites passed (120/131). A final host-template addition and its assertion
were followed by final suite reruns. An initial wheel comparison detected a stale
setup.md because it was edited immediately after that wheel build; rebuilt from
final sources and passed the comparison. No assertion was weakened and no tests
were skipped or removed. No runtime defect was found in this change.

All tests use temporary ATO_HOME/CODEX_HOME, synthetic credentials/repositories and
local fixtures. The unchanged transport harness uses bounded ephemeral loopback,
90-second operation deadlines plus bounded cleanup, intercepted dispatch, actual
stdio child handling and listener closure assertions. No new listener or poller
was added. The host independently reruns server suite, transport and whitespace
acceptance; those future host results are not claimed here.

Private ignored evidence: `.verification/mcp-controller-unification/` contains
`install.log`, `dependencies.txt`, `core.log`, `server.log`, `core-final.log`,
`server-final.log`, `transport.log`, `diff-check.log`, native CLI help captures,
`wheel-build-final.log`, `wheel-check.log`, `wheel-initial-note.txt` and the built
wheel. These are operator evidence and are not included in Git.

## Exact changed paths

```text
README.md
docs/mcp-api.md
docs/mcp-controller.md
docs/mcp-controller-validation.md
install.sh
pyproject.toml
skills/ato/SKILL.md
skills/ato/references/controller.md
skills/ato/references/setup.md
skills/ato/templates/codex-http.toml
skills/ato/templates/codex-stdio.toml
skills/ato/templates/controller-calls.example.json
skills/ato/templates/cursor-http.json
skills/ato/templates/cursor-stdio.json
skills/ato/templates/server-http.example.json
skills/ato/templates/server-stdio.example.json
tests/test_controller_bundle.py
tests/test_pi_and_install.py
tests/server/test_http_mcp.py
```

Official documentation and installed CLI help establish config fields, not client
interoperability. Actual Codex, Cursor, AGY and Grok Bot integration was not
exercised. AGY secret injection and Grok Bot's exact ATO-compatible client/auth
capabilities remain pending. Public networking, TLS/proxy behavior, deployment
load, other platforms/dependency versions, real provider execution and OS/power
crash recovery remain unqualified. Read the [migration checklist](../skills/ato/references/setup.md)
before a separately authorized client/host rollout. MCP/API submissions still do
not bind or wake native conversations; MCP polling is not push delivery.
