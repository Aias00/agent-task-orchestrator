# Trusted host setup for ATO

The public CLI uses version 1 of the [unified contract](ato-unified-contract.md).
A trusted local host provisions identities from verified enrollment and records
grants only after actual authorization. Give the controller, interaction
endpoint, and user distinct credentials. Keep proof files out of the checkout,
worktrees, native homes and ATO state. A CLI user's shell selects its private
proof through `ATO_ACTOR_FILE`.

## Profile and private records

The host writes JSON files under `$ATO_HOME/unified-v1/host`. Use owner-only
directories and mode `0600` files. Preserve exact Task, Run, repository and
generation values from the ledger for scoped grants. An adapter integration can
call [Host](../ato_host.py) to compose the same services as the CLI.

- `profile.json` contains `worker_executable`, `worker_home`, and
  `review_tools` as an array of `[kind, executable, home]` triples. Use
  canonical absolute executables and private homes. The bounded Worker profile
  requires the local write sandbox. `ato agent doctor` reports local file
  availability and capability evidence.
- A proof file contains `version: 2`, an opaque `reference`, and
  `proof: {method, credential_ref, challenge}`. Use the local CLI proof
  method `local-cli-token` and a secret challenge. The corresponding
  `credential-<32 hex>.json` in the host registry contains `enabled`,
  `reference`, `role` (`controller`, `interaction`, or `user`), the
  verified v2 session `identity`, and the SHA-256 `token_hash`.
- A submission credential is enrolled as a controller with `submissions`
  entries containing `reference` for the approved authorization, `repo_key`,
  `spec_sha256`, and the exact initial `binding` with `controller_ref`,
  `interaction_ref` and `revision: 1`. Use the normalized digest output by
  `ato project validate`.
- A Task credential contains `scope: {task_id, repo_key, spec_sha256,
  authorization_ref}`, `binding_revision`, `run_id`, `generation`, and the
  granted `operations`. For user questions, an optional `request_ids`
  list further restricts the grant. The enrolled user uses the same
  interaction reference as the active endpoint with a separate proof.
- Each enrolled endpoint has an `agentref-<32 hex>.json` record containing
  `enabled`, `reference`, `repo_key`, `role`, and applicable `revisions`.
  An endpoint may further pin `scope` to a single Task. The optional
  `mailbox` credential comes from the existing trusted managed mailbox.
  Grant changes belong to the enrolled host. A credential can be revoked by
  disabling its registry record.

The authenticated user's explicit invocation is the event for ordinary answers
and acceptance. Privileged answers require the host's exact correlated event
receipt in `event-<digest>.json` with `grant_ref` and `performed`. Use the
verifier in [ato_host.py](../ato_host.py) for the digest and
authority fields. A binding/reviewer transition additionally requires
`decision-<evidence SHA-256>.json`, with the actual user's credential
`actor_id`, `operation`, and `source_ref`. These host receipts describe
verified events and their purpose. They are separate from user-supplied
evidence files.

## Observation

The release tool can preserve authorized observation when the target host
already has an enabled `observation.json`, a `source_ref`, and a prepared
managed mailbox in the selected unified namespace. The optional
`transport_kinds` list selects already registered trusted adapters. Prepare
route credentials and notification policy through the trusted host. Read-only
diagnosis and installation work with the observer absent. The observer routes
compact references. Its reader verifies the current Task, generation,
binding and actor before exposing material.
