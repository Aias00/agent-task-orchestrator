# Trusted host setup for ATO

The public CLI uses version 1 of the [unified contract](ato-unified-contract.md).
A trusted local host provisions identities from verified enrollment and records
grants only after actual authorization. Give the controller, interaction
endpoint, and user distinct credentials. Keep proof files out of the checkout,
worktrees, native homes and ATO state. A CLI user's shell selects its private
proof through `ATO_ACTOR_FILE`.

## Profile and private records

From the selected sealed source, the trusted local operator can run
`.venv/bin/python -IB scripts/ato_host_setup.py`. The script issues passive
local credentials under the operator's authority. It starts no sessions.

1. Run `configure --profile FILE --source SOURCE` once to publish the private
   execution profile described below.
2. Run the installed `ato run --repo PATH --spec FILE` as the account that
   owns the private ATO state. The same account can list and control its
   local tasks. For explicitly scoped submission, use
   `submission --repo PATH --spec FILE --source SOURCE` and select the returned
   controller proof in `ATO_ACTOR_FILE` for that invocation.
3. Run `scope TASK_ID --role controller|interaction|user --source SOURCE`
   from the trusted host for the approved role. Give the resulting proof only
   to that role. Reissue scoped proofs after a Run or binding change.
4. With the scoped controller proof, run
   `ato agent ask TASK_ID --kind decision --question TEXT --reason REASON`.
   The actual scoped user can then use `ato inbox` and `ato answer`.
   Use `--continuation resume` when the decision authorizes a frozen
   continuation. For a verified pending host action, use `--kind host_handoff`.
5. For a privileged answer, the trusted operator records the actual event
   with `attest REQUEST_ID --answer TEXT --source SOURCE` and the scoped
   user proof. Add `--performed` only for a host action that actually finished.
   The user records the same answer through `ato answer`.

The host writes JSON files under `$ATO_HOME/unified-v1/host`. Use owner-only
directories and mode `0600` files. Preserve exact Task, Run, repository and
generation values from the ledger for scoped grants. An adapter integration can
call [Host](../ato_host.py) to compose the same services as the CLI.

- `profile.json` contains `worker_executable`, `worker_home`, `proof_roots`, and
  `review_tools` as an array of `[kind, executable, home]` triples. Use
  canonical absolute executables and private homes. Set `proof_roots` to the
  existing, canonical `0700` directories that hold actor proof files. These
  roots must be separate from repositories, ATO state and every configured
  native home. Actor proofs are accepted only inside these roots. Command
  tools and independent checks cannot read the proof roots, state or native
  homes. AGY and Claude can read their own authentication home inside their
  outer sandbox. `ato agent doctor` reports local file
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

For Codex delivery, the observer records a native cursor before dispatch and
keeps the exact endpoint lease while observing a pending attempt. A correlated
terminal event closes the mailbox turn. If the lease is lost or the old
attempt cannot be verified, the observer preserves the existing delivery
disposition without replaying it.

### Acceptance reminders for local Tasks

For a local `ato run` launched from Codex, ATO uses `CODEX_THREAD_ID` to look
up the exact session in the configured native Codex home. It checks the native
index and session header and privately records the verified ID and session
working directory with the Task. The Task's repository still comes from
`--repo`. Different conversations can submit to the same repository and
receive their own acceptance reminders.

With the Observer, managed mailbox and native standalone Codex available,
the Observer verifies the saved session and sends a short reminder there
when the Task first needs human acceptance. An unverified locator or a
saved session that later becomes unavailable leaves the item recorded in ATO.
Project mutes apply. This applies to new Tasks captured at submission. ATO
does not infer earlier origins from repository names or replay reminders.

For a terminal submission without a session locator, a trusted operator can
opt a repository into reminders through an existing Codex control session.
Run this from the selected sealed source with the same native and ATO homes
as the Observer.

```sh
.venv/bin/python -IB scripts/ato_host_setup.py bind-acceptance \
  --repo "$TASK_REPO" --session-id "$CONTROL_THREAD_ID" \
  --session-project "$CONTROL_THREAD_CWD" --source "operator selected control session"
```

Specify the exact session ID and the working directory recorded when that
session was created. The command verifies both in the configured local Codex
home. It uses the native standalone Codex installation and the prepared unified
mailbox. Repeat with another repository to opt it into the same session.
The host stores mailbox capabilities privately and provisions them only once.

The opt-in applies when a Task without a recorded submission session first
needs human acceptance after binding. Tasks already waiting when the
repository was first bound retain their existing state. Consult
`ato status TASK_ID` for the product, checks, and delivery state. An unknown
delivery keeps the original receipt. Both routes grant delivery only.
Once the actual user approves, the local state owner can record the decision
with `ato accept TASK_ID` and optionally attach evidence. A scoped user
uses the exact product evidence for their acceptance.
