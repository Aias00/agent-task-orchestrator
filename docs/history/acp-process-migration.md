# ACP candidate staging and migration

Nothing here installs a launcher, activates a runtime, migrates a task or
changes a session binding. Existing Codex, Claude, Pi and legacy plans keep
their adapter selections. Explicitly selecting `acp_pi` is required for a new
candidate plan; deployment and any real provider run are separate decisions.

The only Pi adapter source supported by this candidate is pi-acp 0.0.34, SHA256
`324aeb8bba1228937e16b1326fb3e014e2a625b2dea549dbcd49b006cd5df6a2`, from
the local validation package. `scripts/derive_pi_acp.py OUTPUT` verifies that
hash before producing a new local adapter directory and dependency symlinks.
It does not edit or activate the original. The patch replaces the hard-coded
session-map directory with a required per-attempt directory; disables the
upstream npm update probe; forwards a client
turn nonce into the Pi session; observes native RPC assistant `message_end`
stop reasons; emits a typed, correlated receipt after settling; and records
native RPC prompt failure as a typed error. Native errors remain sticky. It
does not parse public model text for errors or infer proof from a transcript.

The bridge trusts receipt metadata only for a code-owned profile running this
derived adapter. A generic third-party ACP server cannot be substituted as a
trusted native evidence producer. Its ACP `end_turn`, tool output, model text,
and old session records cannot prove a native result. Each attempt uses a new
nonce and session; unknown delivery is never automatically replayed to obtain
evidence. Native failure or missing proof prevents a successful checkpoint.

The current pinned paths and Pi executable are local to this candidate and
must be reviewed for another machine. The runtime uses the OS sandbox and the
existing file-tool guard, with no unsandboxed fallback. The optional Python SDK
must be installed in the invoking interpreter separately. The profile grants
no write access to the native user configuration; configurations that require
such writes fail rather than relaxing the boundary. Never copy credentials into
the package or demo. Validate permissions and availability on the target host
before any separately authorized activation. Offline fixture success does not
prove authentication, model availability, or provider behavior.
