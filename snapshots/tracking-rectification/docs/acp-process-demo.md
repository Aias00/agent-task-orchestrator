# Reproducing the offline ACP timeline

The retained demo is a real foreground run in a separate scratch Git repository,
created through the candidate runner and its trusted offline fixture bootstrap.
It launches a local ACP subprocess, writes a fixture file, runs an independent
host command, and saves the resulting run and trace. It does not contact a
provider or modify the assigned task's scheduler-owned directory.

The run path is recorded relative to this repository in
`build/acp-process/demo.json`. From the candidate root, print its public
timeline with:

```sh
RUN=$(python -c 'import json; print(json.load(open("build/acp-process/demo.json"))["run_dir"])')
python runner.py trace "$RUN" --json
python runner.py status "$RUN"
```

The JSON has `events` and `summary` keys. It should show one tool ID across
pending, in-progress, and completed states, followed by scheduler-owned check
events and `tool_call_count: 1`. The trace is metadata, not the model's body or
an authorization to execute anything. `scripts/create_acp_process_demo.py`
creates a new demo only when `build/acp-process/demo-repo` does not exist; it
refuses to overwrite the retained run. Use the Python ACP SDK interpreter to
run it. To make a fresh copy, choose a clean candidate workspace rather than
deleting the retained evidence.

The failure fixture separately feeds the immutable recorded Pi native auth
error into a local Pi RPC subprocess and passes it through a derived copy of
pi-acp. ACP returns `end_turn`, but the bridge rejects a success checkpoint.
This is an offline replay of a recorded error, not a fresh authentication or
provider validation. No automatic replay of a user task is performed.
