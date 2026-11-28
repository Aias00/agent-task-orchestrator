# Codex CLI MCP qualification — 2026-11-27

Baseline: `a6cd1ec9eaedd11a88c73464976b4e8f6465c843`, the distributed
MCP-first bundle from `task-8701f5b6776d44999c67f1bad0dbd8b8`.
Qualification task: `task-95224f831c654fa096b616c941b5bbff`.
The user explicitly authorized actual Codex calls and one tiny native worker.
This authorization does not extend to other providers or future test runs.

## Scope and method

`scripts/check_codex_mcp.py --run` is an opt-in manual integration exercise,
not part of unit-test discovery. It uses installed Codex CLI **0.171.0**, the
installed default **gpt-6.2-astra** through its existing configured native
provider, Python **3.12.15**, and this checkout's private `.[server]` virtualenv.
Official MCP/mcp-types **2.3.0**, Starlette **0.52.1** and uvicorn
**0.54.0** were used. Exact executable/source hashes are in the private manifest. No SDK-only result is counted as a native controller call.

The harness substitutes private paths/loopback URL into the distributed Codex
and host templates. Stdio launches the actual `ato mcp --stdio` executable;
HTTP launches actual `ato serve` on an ephemeral loopback port. The controller
must emit successful native `mcp_tool_call` events with successful ATO envelopes.
Saved events are corroborated against the task, run, check, event and mutation
journal databases, rather than the model's final prose.

Four bounded controller processes cover initial stdio submission, stdio final
inspection, a new HTTP client replaying the persisted exact submission, and a
synthetic HTTP lifecycle. Each has a 240-second deadline and zero configured
provider request/stream retries. Only one real worker is admitted, with a
120-second timeout, one attempt, no reviewer, and the adapter's workspace-write
sandbox. The worker writes `marker.txt`; an independent host command checks its
exact bytes. Both transports then expose the final check/diff facts.

The synthetic lifecycle intercepts only `_spawn_background_run` in a separate
HTTP fixture process. Admission, isolated worktree creation, journaling, STOP,
resume and tool transport remain real. It checks unchanged task/spec identity,
two runs, a 15-second resume override and final STOP. It is **not** another real
worker or a claim about stopping a live provider. Separate official SDK probes
check read-only discovery, denied read-principal submission, denied bot answers,
and HTTP 401 without a valid bearer. These authority probes are synthetic, not
native user interaction. No real human answer or native push wakeup is claimed.

## Approval finding

The first native stdio session successfully called repositories and validate,
but submit failed with `MCP tool call requires approval, but approval policy is
never`. Both server task and journal tables contained zero rows: no mutation
was delivered. Its failure evidence is retained, and its offline verifier exits
nonzero. The harness does not convert a denial into successful qualification.

The second campaign uses the installed native `--approve-for-me` facility,
which retains workspace-write sandboxing and automatic approval review. It does
not use a sandbox/approval bypass flag or set tool approvals to bypass review.
Trust is recorded only inside the temporary CODEX_HOME for the disposable
controller repository. A future unattended `codex exec` controller needs an
available, authorized approval route for ATO mutations; merely importing the
MCP template is insufficient. Interactive clients may use their normal approval
UI instead. An approval rejection remains a blocker.

## Credentials, isolation and cleanup

The harness reads the existing default model/provider configuration in-process
and copies only those native settings plus `auth.json` into private 0700
CODEX_HOME directories, with files 0600. Credential bytes are never placed in
prompts, command arguments, committed configuration or reports. Native
credential-file copying is documented in the [Codex authentication guide](https://developers.openai.com/codex/auth);
MCP fields follow the [official MCP guide](https://developers.openai.com/codex/mcp).
No new login, credential refresh of the original file, custom provider client,
global installation or live client configuration edit is performed.

Client environments use private HOME, CODEX_HOME and ATO_HOME; unrelated MCPs,
hooks, plugins, user instructions and origin/session variables are not imported.
Only a generated read/control bearer reaches the controller; no human token is
created. HTTP uses the template's `bearer_token_env_var`, never a literal header
in client configuration. The server strips transport tokens from worker env.
Native workers retain the existing adapter and notification behavior: MCP origin
is unbound, with completion notification `unsupported`.

Finally blocks stop only birth-verified owned process groups, snapshot necessary
private raw event/state artifacts, delete credential copies and temporary homes,
and check listener closure and unchanged original config/auth hashes. A final
credential-value scan removes any contaminated artifact and fails verification.
The private native evidence includes synthetic repository paths and session IDs;
it must remain ignored and must not be committed or shared as a public report.

## Observed results

| Check | Result |
| --- | --- |
| Initial default exec approval route | Repositories/validate succeeded; submit denied before delivery; zero tasks/journal rows |
| Native stdio, automatic approval review | 8 successful calls across two processes: repositories, validate, submit, list, status, diff, trace, inbox |
| Native loopback HTTP, environment bearer | 8 successful calls including same-key exact submit replay and final checks/diff |
| Replay / real worker | One task, one journal submit, one admission, one completed native worker run, exact marker host check passed |
| Native HTTP synthetic lifecycle | 7 successful calls: submit, STOP, resume, STOP, status, trace, inbox; one task, two stopped runs, unchanged digest |
| Separate synthetic authority probes | Missing/wrong bearer 401; reader has exactly six read tools; reader submit and bot/reader answer return FORBIDDEN |
| Core suite | 128 passed, zero failures/errors/skips |
| Core plus official SDK suite | 139 passed, zero failures/errors/skips |
| Synthetic socket/stdio smoke | Passed; two synthetic admissions, zero native providers |
| Offline native evidence verifier | Passed in the harness and again from an independent process, without model calls |
| Cleanup | Seven owned process identities exited, two listeners closed, temporary credentials/homes removed, original config/auth unchanged, credential scan clean |
| Whitespace | `git diff --check` passed |

The successful campaign contains **23 native MCP tool calls**, four controller
turns and one real worker turn. The earlier denied campaign adds one controller
turn; it admitted no worker. Eight new provider-free tests exercise evidence
rejection (prose, approval failure, business denial, incomplete turn, missing or
stale evidence, missing integrity coverage and altered events) and read-only
database access. No runtime/adapter/transport code or distributed template was
changed. The compatibility finding is the native mutation approval requirement;
setup documentation now distinguishes that from template syntax support.

Source identity covers every runtime Python file, script, dependency declaration
and distributed template, including the exact harness used for the live calls.
Documentation/test-only additions are recorded separately in Git. The successful
manifest was independently verified after the final code changes; no post-hoc
success flag or source-hash rewrite was used. The separate orchestrator host's
acceptance rerun is not represented as a result of this worker.

## Reproduction and independent acceptance

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple -e '.[server]'
.venv/bin/python -m pip check
python3.12 scripts/run_tests.py
.venv/bin/python scripts/run_tests.py --server
.venv/bin/python scripts/check_server_transport.py
# Explicitly authorized live exercise only; never overwrite an existing campaign:
.venv/bin/python scripts/check_codex_mcp.py --run \
  --evidence .verification/codex-mcp/native-auto-review
# Offline, no further model calls:
.venv/bin/python scripts/check_codex_mcp.py \
  --verify-evidence .verification/codex-mcp/native-auto-review
git diff --check
```

The offline verifier checks required steps, successful native calls per transport,
exact replay arguments and journal digest, one real admission/run, independent
marker check, final controller observations, synthetic lifecycle, authority
separation, source/client identity, artifact hashes and owned-process cleanup.
It reads evidence databases without initializing or migrating them. Hashes
protect against missing/stale/accidentally altered evidence; they are not a
cryptographic attestation against a malicious host rewriting all artifacts.

Private evidence is under ignored `.verification/codex-mcp/`: initial denial in
`native/`, subsequent campaign in `native-auto-review/`, plus suite and transport
logs. The host can independently run the full SDK suite, synthetic transport,
offline verifier and whitespace gate without repeating live model calls.

### Attempt 2: host evidence path repair

The first orchestrator acceptance failed before reading any evidence: it passed
`.verification/codex-mcp/evidence.json`, which did not exist. The verifier accepts
a **campaign directory**, then reads its `manifest.json`; the successful campaign
was already present at `native-auto-review/` and still passes source/client and
artifact integrity checks.

The private handoff now includes a relative directory symlink named `evidence.json`
pointing to `native-auto-review`. Despite its suffix, this entry is a directory
alias, not a JSON document. It preserves the existing host acceptance command:

```sh
.venv/bin/python scripts/check_codex_mcp.py \
  --verify-evidence .verification/codex-mcp/evidence.json
```

This repairs the artifact handoff without altering the recorded manifest, native
events, databases, executable sources or verification rules. No additional native
model calls or workers are needed. For a new campaign, pass its actual directory
to `--verify-evidence`; the alias is specific to this private host handoff and is
not created automatically by the harness. When transferring this evidence, retain
both the alias and its target (or use the target directory directly).

Attempt 2 reran the exact host evidence command successfully, plus the 128-test
core suite, 139-test core/official SDK suite, 8 focused evidence negative controls,
synthetic loopback/stdio smoke, dependency consistency and whitespace checks.
All passed with no skips. Logs are private `attempt-2-*.log` files alongside the
campaign; `attempt-2-handoff.json` records their hashes and the original manifest
hash. These are worker-side independent process checks; the orchestrator's next
acceptance run remains separate.

## Limitations

This exercise applies only to this CLI, native provider configuration, source
fingerprint and local transport setup. It does not qualify the current Work/app
session, Cursor, AGY, Grok Bot, public HTTPS/TLS/proxies, production load, power
loss, credential rotation, or real human-answer routing. No main-branch merge,
push, public deployment, live skill/runtime installation or external message was
performed. Existing test suites remain provider-free and retain every prior test.
