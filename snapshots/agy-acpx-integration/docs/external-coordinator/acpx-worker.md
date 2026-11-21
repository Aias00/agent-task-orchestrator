# Reviewed acpx worker profile

Production selects `agent.kind=acpx_codex`; planner, native resume, additional
grants and full-access are unsupported. Optional model belongs in `agent.model`.
No executable/environment/MCP/permission profile may come from task JSON.
ATO owns the attempt, process group, STOP and independent checks. acpx public
runtime owns only this fresh in-memory transport session, with no CLI queue.

Exact pins (no worker install, npx, PATH fallback or download):

| Asset | Reviewed value |
| --- | --- |
| acpx | 0.22.0, commit 4832e4cc43543edd2ac7fcc80d0177b0c36ffd7a |
| acpx tarball SHA256 | ea181d761bf141858e8c92de13c77b40783e9b6189d73f1d654afcc96cb15dd0 |
| Codex ACP | @agentclientprotocol/codex-acp@1.1.5 |
| ACP entry SHA256 | 8ef25bf9e507fb1af3625287b3fb1a9e4aaac539d44834fdef17b7d3cfbe4202 |
| ACP registry integrity | sha512-ZBfl3dJ1Y27M8Rlh9hM/82Dy+EbOwZ1uyhLq9YGdblUoHDgCtimqZnd8ktuLsXTa1sInKRZHCvCs3EjtZ3arsw== |
| Native Codex | 0.144.6, macOS arm64 |
| Native binary SHA256 | 80a3933d11a9d13ef806aa24f7bb8afc9169cfe4e9b09d6da6a92922cbde9cff |
| Node | 24.22.1 |
| Node SHA256 | 6f32069480ec14d52b6d7b1f3c7124cfbbc46203abf5ddf5c65b127435f3f329 |

Origin supplies canonical exact paths in a private owner-only JSON config and
passes its absolute path as `ATO_ACPX_CONFIG`. This is a dependency locator,
never actor proof or a permission grant. The schema has exactly `version=1`,
`node`, `archive`, `deps` (node_modules directory), and `native_home`. All paths
must be absolute/canonical. Config must be a regular non-symlink private file.
Pins and policy remain code-owned even when paths change. Fixture dependency
constants are only defaults for trusted offline fixture code; production
requires private config. The candidate does not contain dependencies or login.

Example shape (replace all paths with reviewed canonical host paths):

```json
{"version":1,"node":"/exact/pinned/node","archive":"/exact/acpx-0.22.0.tgz","deps":"/private/deps/node_modules","native_home":"/private/clean-native-home"}
```

Native home must be private 0700 with no config/skills/plugins/instructions.
Origin provisions the already authorized native login reference in place;
implementation checks names/permissions only. Native account failure is an
explicit prerequisite. Human/root ATO credentials stay under protected ATO_HOME,
never the repository, native home or prompt. Production uses a single native
named sandbox plus restricted app-server gateway, not the old nested outer
sandbox. See [architecture.md](architecture.md) for exact ownership, capabilities,
unknown transport behavior and host qualification still required.

Repository `.git`/`.agents` metadata remains readable for ordinary coding tools;
the native workspace policy protects writes. ATO `.codex` task/actor/control
authority stays unreadable and unwritable. The native tool environment and
shell policy use only the exact qualified Node executable directory, the trusted
launch Python directory, `/usr/bin` and `/bin`, with no inherited PATH/secrets.
Actual native Git reads, Node execution, protected writes and private authority
read denial are a model-free host gate, not established by fixture success.

Each attempt now uses a disposable native home with an authentication path
reference to the operator's clean seed; native-generated config/built-in skills
cannot contaminate the next attempt. Authentication contents are never copied.
The optional private host config field `model_proxy` accepts only a credential-free
HTTP(S) URL at an explicit loopback port. It is passed as HTTPS_PROXY only to the
native model transport. Tool-shell inheritance remains disabled and the native
named tool-network permission remains false. No ambient proxy is inherited.
Failure diagnostics retain only fixed lifecycle/code/dispatch enums, with no
upstream error message, raw stderr, prompt or credentials. Diagnostic metadata
never counts as a valid transport receipt and never unlocks unknown delivery.
