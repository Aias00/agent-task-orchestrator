# Neutral CLI human interaction (candidate only)

The built-in `local_cli` adapter provides private, passive CLI hub and origin
endpoints. It starts no service, agent, observer or scheduler. Codex executable,
index and app-server are unnecessary. The existing `session_router`,
`interaction` question ledger and `message_bus` remain the only authorities.
This checkout is an isolated candidate; these instructions do not install it or
change the currently configured global hub. The complete six-step objective and
remaining delivery qualification are preserved in [task.md](../history/session-decoupling/task.md).

## Provisioning and binding

Use a separate explicit `ATO_HOME` for this candidate. Credentials must be kept
outside the model-visible project, owned by the local user, mode 0600, with a
private parent directory. CLI output contains references and credential paths,
never bearer secrets. Environment variables locate state or sessions; they do
not authenticate actors. Trusted CLI provisioning is the local OS-user boundary,
not protection against malicious root or another process with the same user's
private keys. The Pi file policy exposes no shell or administrative command and
cannot read these out-of-project credentials.

The following is a runnable shell sequence after supplying an existing temporary
Git project and its reviewed workflow contract. Use actual instruction/answer
sources when a real human operates it; synthetic examples grant no real task
authority. Run with the candidate's Python 3.12+ interpreter, not the installed
global launcher. `jq` is not required.

```sh
PY=/absolute/path/to/python3.12
CANDIDATE=/absolute/path/to/isolated-candidate
PROJECT=/absolute/path/to/temporary-git-project
PLAN=/absolute/path/to/reviewed-contract.json
PRIVATE=$(mktemp -d /tmp/ato-neutral-cli.XXXXXX)
chmod 700 "$PRIVATE"
export ATO_HOME="$PRIVATE/ato"
export CODEX_HOME="$PRIVATE/unavailable-codex"
ctask_candidate() { "$PY" "$CANDIDATE/runner.py" "$@"; }
ref_from_json() { "$PY" -c 'import json,sys; print(json.load(sys.stdin)["reference"])'; }

ORIGIN=$(ctask_candidate local-session-create --project "$PROJECT" --role origin \
  --source 'Actual user instruction designating this local origin; supply its source' \
  --credential-file "$PRIVATE/origin-root.json" | ref_from_json)
HUB=$(ctask_candidate local-session-create --project "$PROJECT" --role hub \
  --source 'Actual user instruction designating this CLI hub; supply its source' \
  --credential-file "$PRIVATE/hub-root.json" \
  --user-credential-file "$PRIVATE/user-root.json" | ref_from_json)
ctask_candidate hub --session-ref "$HUB" --actor-file "$PRIVATE/hub-root.json" \
  --source 'Actual user instruction choosing this isolated CLI hub; supply its source'
WORKFLOW=$(ctask_candidate workflow-create --repo "$PROJECT" --plan "$PLAN" --origin-ref "$ORIGIN")

for ROLE in origin hub user; do
  ctask_candidate local-session-grant --credential-file "$PRIVATE/$ROLE-root.json" \
    --workflow "$WORKFLOW" --output "$PRIVATE/$ROLE-generation0.json" \
    --source 'Existing reviewed workflow authority; supply actual instruction source'
done
```

Hub and user credentials belong to the same verified endpoint, but are distinct
principals. Origin credentials belong to the exact assigned origin. A root key
can create only grants for its own fixed role and endpoint. Each immutable grant
contains the exact workflow, frozen contract hash, execution revision and full
workflow start-generation checkpoint. A scoped key cannot issue another key or
renew itself. A new execution needs a new explicitly issued grant from its root
key; old questions and old grants cannot be carried into that execution. The
grant command rejects STOP, closure, wrong project and wrong hub/origin binding.

## Canonical question, human answer and foreground continuation

```sh
QUESTION=$(ctask_candidate ask "$WORKFLOW" --actor-file "$PRIVATE/origin-generation0.json" \
  --question 'The concrete decision needed from the user' \
  --recommendation 'The proposed option and reason' \
  --context 'The relevant task context' |
  "$PY" -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ctask_candidate inbox --actor-file "$PRIVATE/hub-generation0.json"
ctask_candidate read-question "$QUESTION" --actor-file "$PRIVATE/hub-generation0.json"
# Record presentation only after actually showing the question to the user:
ctask_candidate read-question "$QUESTION" --actor-file "$PRIVATE/hub-generation0.json" \
  --presented-source 'Actual terminal display/source; supply the real event'
# The human supplies their actual words and source here. Do not use model output:
ctask_candidate respond "$QUESTION" --actor-file "$PRIVATE/user-generation0.json" \
  --answer 'Actual user answer' --source 'Actual user input time/source'
ctask_candidate accept-answer "$QUESTION" --actor-file "$PRIVATE/origin-generation0.json"
ctask_candidate continue-task "$WORKFLOW" --accepted-answer "$QUESTION" \
  --actor-file "$PRIVATE/origin-generation0.json"
```

`read` aliases `read-question`; `respond` aliases `answer`. Reading/listing does
not mark presentation, answer, acceptance or completion. Presentation is recorded
once with the explicit actual display source. Hub and origin credentials cannot
answer as the user, even with an alleged source saying “approved.” The user
credential and a nonempty actual answer source are both required. Question and
answer retain the original canonical question ID, origin, hub and routing source.
The question records non-secret actor audit claims separately from the actual
user text/source. Conflicting duplicate answers are rejected. Repeating the exact
answer reconstructs the same mailbox envelope and never resends an unknown one.

`continue-task` runs **in the foreground**, using the existing repository flock
and execution lease. Under that lock it rechecks the accepted question, actor,
contract, STOP, closure and full generation before executing the existing
workflow runner. It creates no detached worker, supervisor or controller. It
does not remove STOP or change attempts, limits, budgets or acceptance commands.
Acceptance of a routed answer does not itself resume or complete anything.
The runner must independently pass the full frozen command criteria. A manual
criterion remains pending until a user-only `acknowledge --actor-file USER_GRANT`
with an existing evidence file and actual decision note, followed by revalidation.
Hub/origin credentials cannot create a neutral manual acceptance receipt.

An answer accepted in one execution cannot authorize repeated continuation after
the workflow advances. Further decisions need a fresh question in the current
generation. Credentials for that generation must be explicitly issued; do not
edit saved grants or question bindings. Unknown delivery retains its original
barrier and never automatically replays on inbox/read/answer/accept.

## Passive transport semantics and compatibility

The local adapter verifies the exact endpoint and canonical envelope in the
existing mailbox, then records correlated admission/operation-complete receipts
with the actual dispatch, owner, route and generation fences. Its turn describes
the **passive host admission operation**; it does not claim that a human read or
answered the question. No duplicate payload store or independent delivery ledger
is introduced. The transport source explicitly says it is not human receipt or
approval. No native worker is created, and activity is honestly `unknown`.
Native process creation, cancel and load/resume are `unsupported` for local CLI.
Native agent observation is also `unsupported`: mailbox admission cannot be
observed as a human/origin response by recovery. Question reads and human replies
use the authenticated interaction commands and their explicit audit records.
Transport `turn_completed`, ACP `end_turn`, EOF and assistant text never settle
workflow acceptance. The local endpoint needs no native queue-release exception.

Existing UUID/v1 and older trusted `is_actor` integrations retain their public
compatibility shim, including previously registered v2 references using that
older API. An opaque v2 reference alone does not upgrade their authentication
claims. When an explicit proof is supplied, the scoped role API is mandatory.
The actual local CLI and managed Pi adapters always reject the ambient `is_actor`
path, so omitting their proof cannot select compatibility authority. No fixture
registration is installed into production, and no tests or old questions are
rewritten to obtain compatibility.

## Managed Pi origin integration

A managed Pi coordinator remains an origin, never a human/hub principal. Its
trusted host obtains `adapter.origin_actor_proof(identity, workflow_id=...,
contract_hash=..., generation=..., source=ACTUAL_EXISTING_AUTHORITY_SOURCE)` after
the exact workflow has been bound. That existing API grants origin actions only.
The host may pass the proof directly to `interaction.ask(..., actor_proof=proof)`
or write a bounded private CLI actor file with this exact neutral envelope:

```python
from dataclasses import asdict
from ato_adapters.local_session_adapter import private_create
private_create(private_actor_path, dict(version=2, reference=origin_reference,
    workflow_id=str(workflow_directory), proof=asdict(origin_proof)))
```

Then `ask`, `accept-answer` and `continue-task --actor-file` use the same public
CLI as a local origin. The model receives neither this proof nor root/user keys.
The current managed host must be alive on its existing private control endpoint
to receive the canonical answer. A transport error retains unknown/no-replay;
neither a new process nor a native resume is guessed. This is trusted host work,
not a new model-controlled shell/ACP administrative action or an expanded repair
budget. The later final delivery stage owns actual-Pi/local scripted-model smoke
and full candidate qualification; this CLI stage makes no real Pi/cloud claim.
