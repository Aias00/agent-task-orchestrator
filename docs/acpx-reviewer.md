# Codex ACP Reviewer pilot

`codex_acpx` is an opt-in Reviewer on macOS and Linux. Each review round opens
a fresh native session. ATO runs the task's independent checks and guardrails
first, validates the structured reviewer verdict, and fingerprints the worktree
before and after review.

## Enablement

Install Node.js and a compatible acpx build. The fork based on acpx 0.23.0
requires Node.js 22.13.0 or later. ATO uses the built `dist/runtime.js` with
the `beforePromptDispatch`, `onOwnedProcessStarted`, and
`onOwnedProcessCleanup` host hooks.

Add an `acpx_reviewer` object to the host's existing `$ATO_HOME/v2/settings.json`
(default `~/.local/share/ato/v2/settings.json`). Use this structure.

```json
{
  "acpx_reviewer": {
    "node": "/opt/ato-tools/node/bin/node",
    "runtime": "/opt/ato-tools/acpx/dist/runtime.js",
    "launcher": "/opt/ato-tools/codex-acp/bin/codex-acp"
  }
}
```

Use trusted absolute paths outside the task worktree. Keep the verified fork's
`dist` tree and its dependencies together, and pin the installed Codex ACP
launcher to a build that implements the built-in ACP v1 `read-only` mode. ATO
runs the launcher directly with a sanitized environment. If a launcher needs
to bind a specific native Codex binary, configure that in the host launcher.
Supply `OPENAI_API_KEY` in the ATO process environment if the launcher uses the
adapter's API-key authentication. Keep credentials out of settings and specs.

`ato agent doctor` reports whether the host paths are present and usable. Each
turn starts with isolated native configuration, requests the built-in
`read-only` mode, and checks the live session's mode before admitting the prompt.
The client disables ACP file and terminal callbacks and denies permission
requests. The bridge is shipped with the ATO package. Node, the fork, and the
launcher are only required when using this Reviewer.

Set `"reviewer": {"agent": {"kind": "codex_acpx"}}` in a task spec, or use
`ato agent reviewer TASK_ID --kind codex_acpx`. This pilot uses
`session_mode: fresh_only` and the host launcher's model and reasoning defaults.

## Inspect dispatch and STOP

`ato status TASK_ID` includes `acpx_reviewer` after a bridge starts. The private
ATO database ties the exact dispatch to the Run, review round, and independently
observed process birth and group. ATO commits this record before sending its
bounded acknowledgement. The public status has only the resulting facts.

| Evidence | Meaning |
| --- | --- |
| `approval_recorded` | ATO recorded authority. The write may still be pending. |
| `unsent` | ATO had no dispatch record, or the final fork receipt proved the prompt stayed unwritten. |
| `finished` or `failed` | A correlated ACP response or RPC error arrived. Independent checks and the Reviewer verdict determine task completion. |
| `unknown` | The prompt may have reached the peer and ATO lacks a correlated outcome. |
| `cancel_acknowledged` | The peer returned a correlated cancellation response. |
| `execution_cleanup_verified` | ATO verified the bridge and recorded child group have no live processes. |

`ato stop TASK_ID` first revokes dispatch authority. It requests ACP
cancellation when the bridge is reachable, then checks local process cleanup.
It can terminate a recorded group after the bridge exits using the original
root's birth identity or a recorded member whose birth and group still match.
If group ownership or exit cannot be verified, status surfaces
`CLEANUP_BLOCKED` or the unresolved delivery reason alongside cleanup status.
These checks cover the recorded local process groups. Use `ato stop TASK_ID`
again to reconcile cleanup after an interruption.

`ACPX_DELIVERY_UNKNOWN` leaves the Task blocked even after verified process
cleanup. Inspect the prior outcome before taking further action. ATO fences
resume, recheck (including `--checks-only`), and worktree cleanup while delivery
or execution cleanup is unresolved. The pilot has no automatic replay or
operator command that clears an unknown delivery record.

## Offline tests

`python3.12 scripts/run_tests.py` runs the deterministic ATO cases. To also run
the same scenarios through the actual fork and a synthetic ACP peer, point
`ATO_TEST_ACPX_RUNTIME` at the verified built module.

```sh
ATO_TEST_ACPX_RUNTIME=/absolute/path/to/acpx/dist/runtime.js python3.12 scripts/run_tests.py
```

Set `ATO_TEST_ACPX_NODE` to an absolute Node executable if it differs from the
Node found on your test PATH.
