# Runtime binding stage

This inactive candidate implements only fixed per-task execution runtimes from the full task in `task.md`. It does not qualify the responsibility, warm-session, checkpoint, verification-cache, metrics, packaging or actual Codex benchmark stages. Synthetic fixture results do not demonstrate provider speed or native Codex session reuse.

## Creation and compatibility

The actual `create`, `workflow-create` and new delegated-workflow CLI routes issue version 1 `runtime-selection.json` records. The contract/plan format remains unchanged: plans cannot choose an executable, Python, import, bootstrap or runtime path. The trusted agent registry supplies the executing bootstrap; it must be in that runtime's source manifest. The selected Python invocation retains the venv path, even when its executable is a symlink to a system binary.

The existing Python creation APIs remain compatible: `runner.create(repo, plan)` and `workflow.create(repo, plan, origin_thread=None, origin_ref=None)` create legacy-format tasks. Trusted Python callers explicitly select the new format with `pin_runtime=True`. Workflow-generated children inherit a bound parent's complete runtime selection; a legacy parent keeps legacy children. No old JSON is migrated, no historical ID changes, and no attempts, accounting, STOP, grants or delivery records are reset.

The binding is outside the contract. A new state marker records version, an immutable task-incarnation generation, and the exact control-home authority locator. The witnessed binding covers canonical project/task directory, task ID, original contract hash, originating actor/session fields where present, and parent workflow identity. The incarnation marker is distinct from the existing execution revision and execution lease generation: existing ownership/fence checks still control each execution and host action.

## Witness and source validation

Creation writes the exact record once in both the task directory and the private `ATO_HOME/runtime-selections-v1` ledger (legacy configuration falls back through `ato_config`). The ledger belongs to the trusted scheduler, outside normal worker writes; the record is not a bearer permission. Receipt and record files must be regular, single-link, owner-controlled mode 0600 files, and the issuing receipt directory is mode 0700. A changed home, including a home containing a planted duplicate receipt, does not establish authority. This uses the existing local scheduler/control-home trust boundary; it is not a security boundary against an administrator who can rewrite both protected scheduler metadata and its control ledger.

The source manifest enumerates runtime Python/modules, guards, scripts, built-in launch source and trusted test bootstraps, integration metadata/locks, the optional existing Pi dependency tree, the selected interpreter binary and installed supported ACP/Pydantic dependency files and versions. Source links and nested dependency-directory links fail closed. Content, modes, resolved dependency locations, source additions/deletions and interpreter/environment changes are checked. File hash memoization uses device/inode/size/mtime/ctime/mode identities; changed metadata forces a new byte hash. It is not acceptance reuse.

Missing/modified records, receipts, source/dependencies, altered project/contract/actor/incarnation, wrong loaded runtime and wrong parent relationships reject execution. A partial creation can leave an unusable new task with evidence; it cannot silently become legacy. `runtime_selection.load(directory)` is read-only, returns the witnessed record (with `runtime.runner`, `runtime.python`, `runtime.manifest_sha256`), or `None` for genuinely unbound historical data. It never imports selected code.

## Execution routes

Task CLI dispatch validates the witness and source before any execution side effects, then replaces the foreground CLI with exact `[python, runner, original arguments...]` using `execve`, without shell interpolation or dynamic imports. The destination revalidates the Python environment. The mutable default launcher is not part of selection and can independently upgrade; future creations select its new runtime, while bound tasks keep their old runtime directory.

`run`, `_worker`, start/resume launch construction, supervisor resume construction, planner/worker commands, inherited child stages, `host-checks`, `host-recheck` and metered host work all use or enforce the binding. Python execution APIs reject a bound task loaded through another runtime; they do not import/reload it. Registered code-owned external adapters keep their existing APIs. The already-existing checks and permission profiles are unchanged. No new supervisor or observer is introduced.

STOP is a conservative safety request, not an execution route: the existing stop command remains available even if selected source validation fails. Foreground execution still honors STOP; explicit legacy resume semantics remain unchanged. Runtime binding grants no actor role, native session, approval, authority, retry, capacity or broader filesystem access. Unknown capabilities and claimed/unknown deliveries retain their existing fences.

## Operational handoff

Keep finalized selected runtime directories and their interpreters/dependencies intact for the lifetime of bound tasks. Changing a selected directory is detected and refuses work; upgrade the independent default entry for future creation instead. Preserve the associated private creation ledger. Changing the control-home locator does not migrate it. Rebinding a live task, erasing binding metadata or restoring old control databases over newer writes is unsupported.

Rollback for future task selection changes only the default launcher under separate deployment authorization. Existing tasks keep their witnessed selection and all subsequent state/accounting/history. Delegation status, acceptance and dispatcher command construction resolve the same witnessed workflow runtime; no dispatcher is added by this feature. This candidate has not changed the installed launcher, installed skill, predecessor runtime, inherited tests or dependency bytes, and has not deployed or installed anything.

The scheduler must run the original runtime acceptance argv:

```
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py runtime
```

All other full-task gates remain required later, including all six integrated suites, full compatibility/fault regression (at least 759, zero failures/errors/skips), reproducible inactive source bundle and migration/rollback documentation. Once the exact final candidate exists and origin has reviewed it, the bounded actual CLI proof remains origin-only:

```
OPERATOR_SOURCE/acp-process-live-20261119T022710Z/.venv/bin/python OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/acceptance.py host_sample --run-origin
```

That future proof is limited by the recorded handoff to 12 native turns and 1200 seconds with the same configured native profile in temporary Git/file tasks. No automatic additional samples after failure/unknown. This implementation worker has made no real provider/native CLI calls.
