# ATO source maintenance

Keep runtime source, reusable tests and documentation in this repository. Never commit task states, native conversation history, credentials, queue databases or operator-evidence. Preserve workflow authority, STOP, accumulated attempts, independent acceptance and unknown-delivery/no-replay behavior.

Run `python3.12 scripts/run_tests.py` with the pinned optional Python/npm dependencies available. Tests must use temporary ATO/native homes and local synthetic agents, never the operator's live hub or provider. Do not silently skip failures or claim fixture tests establish real-agent/cloud qualification.

Source publication does not activate a runtime or migrate live tasks. Scope and permission changes require the actual user's instruction. Keep active local candidate checkouts intact; development snapshot status is recorded in docs/development-status.md.
