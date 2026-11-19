# Optional Pi ACP profile setup

Core Codex/Claude/Pi routes keep their existing dependencies. `acp_pi` is an
explicit opt-in worker profile; it does not enable another agent's coordinator
or hub. Plans cannot supply commands, module paths or receipts.

Install isolated Python dependencies and the repository-local pinned adapter:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-acp.lock
npm ci --ignore-scripts --no-audit --no-fund --prefix integrations/pi-acp
.venv/bin/python scripts/run_tests.py
```

`derive_pi_acp.py OUTPUT` verifies pi-acp 0.0.34 source SHA256
`324aeb8bba1228937e16b1326fb3e014e2a625b2dea549dbcd49b006cd5df6a2`
before deriving a copy, and resolves its nested or hoisted installed dependencies.
It does not edit upstream code in node_modules or start Pi. The derived adapter
uses an attempt-local state directory, disables npm update probing, carries the
turn nonce and emits typed results from the actual native Pi RPC terminal stream.
Native errors remain sticky; model text and ACP end_turn cannot prove success.

The production profile remains deliberately host-pinned and macOS-only:
`~/.local/bin/pi`, `~/.nvm/versions/node/v24.22.1/bin/node`, and
`/usr/bin/sandbox-exec`. It was validated against Pi 1.5.2 on the original host;
review these code-owned paths and the target configuration before use elsewhere.
There is no arbitrary command override in a plan and no unsandboxed fallback.
Native configuration is read-only; missing prerequisites fail closed. An offline
fixture pass does not establish login, provider limits or model behavior.

The redacted error/end_turn regression under tests/fixtures contains no native
session IDs, credentials or raw provider logs. SDK import remains lazy, so merely
using `ctask status` or a legacy route does not require installing ACP packages.
