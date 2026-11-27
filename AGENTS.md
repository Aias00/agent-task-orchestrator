# ATO v2 maintenance guidelines

Keep runtime source inside `ato/`, concise docs in `docs/`, and fast isolated tests in `tests/`.
Never commit task state, native conversation history, credentials, or operator evidence.
Preserve isolated Git worktrees, independent host checks, opt-in guardrails, read-only reviewer mutation tripwires, PID-reuse-safe process-group cleanup, and AGY/CLI default `workspace-write` Codex execution.

Run `python3.12 scripts/run_tests.py` after changes. All tests must use temporary `ATO_HOME` / `CODEX_HOME` and synthetic local fixtures, never the live user state or external providers.
