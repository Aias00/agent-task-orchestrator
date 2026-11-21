# Source publication and runtime upgrades

Git is the authoritative source history. Keep development on reviewed branches,
commit the selected source and tests, and verify the exact source that will be
deployed. A deployment directory should be an immutable versioned snapshot;
editing or moving it can affect existing subprocesses and future continuations.

Publishing source or changing the default Git branch does not update an
installed launcher. `scripts/install.py` points a launcher at the selected
checkout and starts no services. Before an authorized runtime upgrade, inspect
the current launcher, controller, active workflows, child processes, queued or
unknown deliveries, policy and source versions. Existing active work may still
depend on its original directory; leave that directory available.

Stage and qualify the new version alongside the old one. Switch only the
explicitly approved entrypoints at a verified safe boundary. Keep one active
controller, stop its predecessor gracefully, and independently read back the
actual command, heartbeat, selected source and preserved user policy. Do not
restart business services, replay uncertain messages or migrate old task state
merely to install source. Any such operation requires its own scope.

Rollback restores the approved executable entrypoints and observation process,
not an old task/SQLite snapshot over newer work. Preserve STOP, attempts,
criteria, accumulated time, native identities and delivery claims throughout.

The execution-efficiency development snapshot contains stronger per-task
runtime pinning work. Its presence in a source branch is not a claim that all
current workflows already use that unfinished feature.
