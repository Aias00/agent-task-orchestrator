# Internal coordination and responsibility stage validation

This stage is ready for the frozen host `internal` check. It does not complete
the total task and does not claim that the host acceptance command has passed.
No live activation or actual-record qualification was performed.

Worker validation used Python from the prescribed isolated interpreter. All
fixtures used private temporary ATO_HOME/CODEX_HOME and synthetic identities.
No real model, native queue, business, provider or user notification calls were
made. Existing node_modules and inherited fixtures were not modified.

The final directed run covers `test_internal_coordination` and
`test_tracking_responsibility`. It includes atomic admission, duplicate and
conflicting requests, crashes before/after commit, unknown dispatch/no replay,
wrong actor/workflow/sender, stale generation, STOP, artifact/snapshot/envelope
and assignment tampering, path escape/symlink/nonregular/binary/oversized inputs,
finite expired claims, private payloads, no fake read/presentation, canonical
questions, workflow-specific process/lease proof, and diagnostic dispositions.
Exact counts and source hashes are in `internal-responsibility-stage-receipt.json`.

A compatibility batch passed 243 tests, zero failures/errors/skips, in 59.753s:

```text
test_internal_coordination test_tracking_responsibility
test_tracking_workspace_binding test_approved_delivery test_control test_alerts
test_notifications test_failure_handler test_interaction
test_session_v2_contracts test_session_v2_recovery
```

A notification/actor compatibility batch passed 75 tests, zero
failures/errors/skips, in 18.643s:

```text
test_neutral_hub test_completion_notifications test_workflow_lineage
test_task_disposition
```

The offline `scripts/demo_internal_coordination.py` example passed through the
actual public neutral CLI. It reported queued/available, explicit read, bounded
assignment, unverified handling progress, unsupported automatic wakeup and no
user notice. Fixture roots and credentials were removed on exit.

Reproduce the directed check without touching real state (the test fixtures
provision their own private roots):

```sh
PYTHONPATH=.:tests python -m unittest test_internal_coordination test_tracking_responsibility
python scripts/demo_internal_coordination.py
```

All 49 protected inherited test/fixture files match the fixed baseline. All five
external launcher/guard/skill/notification-helper/pending-efficiency-manifest
pins still match. This worker did not invoke the external acceptance script,
which writes its own evidence outside this checkout. The scheduler must run:

```sh
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python \
  OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/acceptance.py internal
```

Original host socket/native-sandbox checks remain unchanged. No assertions were
loosened and no known worker restriction was repeated to claim host success.

Remaining total-task work includes physical-delivery completion deduplication,
full frozen regression, reproducible portable candidate packaging and the final
origin-owned bounded read-only actual-record proof. The old independent
efficiency objective and its remaining real-CLI qualification are unchanged.
