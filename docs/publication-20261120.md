# Source consolidation, 2026-11-20

The previous remote baseline was `dbb46817254469a8a6d0196724625a0e6f64152b`.
Local runtime work had continued without a corresponding Git publication.
This release exports the deployed compact-handoff controller source, keeps the
legacy CLI surface and records incomplete candidates separately.

`releases/source-baseline-20261120.json` records original source hashes and the
reviewed distribution changes. Core workflow/authority/queue logic is exported
without behavioral rewrites. Distribution changes make fixed Pi/Node paths
relative to the operator home, keep the exact version/receipt guards, use the
already-public redacted Pi regression fixture, support repository-local npm
dependencies, synchronize the current skill and provide optional helper
installation. Tests retain their assertions; real local session identifiers
are replaced consistently with explicit synthetic UUIDs.

The default test command is `python3.12 scripts/run_tests.py` with the pinned
optional Python/npm dependencies. It runs offline fixture regressions under
temporary state roots. The added distribution tests verify helper installation,
pre-write conflict rejection and private-path removal. The final check result
is **602 tests, 0 failures, 0 errors and 0 skips** in the macOS host environment.
The final generic private-path assertion was also checked by rerunning the three
distribution tests after the full suite; no runtime source changed afterward.
This is local offline evidence, not a claim that GitHub Actions or a cloud model
completed successfully.

Historical `prepare_*_candidate.py` utilities are not release installation
commands. They take local `operator-evidence` inputs that are deliberately not
published; missing historical proof stays missing. The current reusable source
inventory is independent of those private acceptance records.

No installed launcher/controller is switched; no task contract, STOP, attempt,
queue claim or native conversation is migrated. Publication uses a new Git
worktree, leaving the original dirty checkout and active development writers
intact. The current real-agent/model and original pending acceptance boundaries
are not changed by Git commits.
