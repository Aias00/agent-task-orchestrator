# Recovery mechanism source release

This release publishes the already deployed takeover upgrade and its required
Agent registry, session-router and ACP trace/result-validation dependencies.
The separate uncommitted managed-mailbox implementation worktree is not included.

The prior live candidate passed 334 independent host tests, including 34 new
recovery/lineage cases, with zero failures/errors/skips. The actual Codex ingress
wrapper also passed 11 tests plus valid/tampered evidence probes before rollout.
Its controller heartbeat and read-only status/trace checks were verified after
activation. These local observations do not assert a GitHub Actions result or
provider availability on a different machine.

For source distribution, runtime behavior is retained while local packaging
references are replaced: the pi-acp dependency is repository-local and pinned,
nested/hoisted npm dependencies are resolved under that installation, native Pi
paths use the operator's home, and the ingress guard defaults to this source
checkout. Regression samples carry only native/transport outcome fields and use
synthetic session identities. The test driver isolates the local interaction home
so operator time policies and hub records cannot alter fixture expectations.
No running launcher, controller or business workflow is modified by publication.

Private candidate briefs, rollout scripts/state, production session IDs, original
raw native evidence and credentials are omitted. Historical evidence remains on
the original host. Published fixtures and the normal CLI reproduce the relevant
protocol and state transitions without paid model calls or actual queue delivery.

The prepared source release passed all 336 local regression tests, zero failures,
errors or skips, with the repository-local dependency installation and isolated
test driver. The guard preserves the published CTASK_NATIVE_CODEX host configuration override
and now mirrors the live project-mute and once-only ingress rules, with two
additional stateful regressions. All 11 preexisting ingress tests also passed.
The offline demo returned 12 events, one tool call, native_success,
transport_completed and check_passed, with no dropped or truncated events.
Markdown references, skill format and git diff whitespace checks also passed.
These checks do not claim remote CI success; Actions must be observed separately.
