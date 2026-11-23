"""Private subprocess bootstrap for a source-selected unified ATO runtime."""
import os
from pathlib import Path
import sys

# The selected interpreter is isolated from ambient PYTHONPATH and user site.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ato_execution import ExecutionHooks, run_child
from ato_task_store import TaskError, TaskStore, _fields, _require, _strict_json


def runtime_hooks(store):
    """Composition point for trusted T4/T5 readers in this exact snapshot."""
    return ExecutionHooks()


def main(argv):
    if len(argv) != 3 or argv[0] != "run-child":
        return 2
    raw = os.environ.pop("ATO_PRIVATE_PIPE_FD", "")
    locator = os.environ.pop("ATO_PRIVATE_ROOTS", "")
    try:
        pipe = int(raw)
        _require(pipe >= 3, "FORBIDDEN")
        import stat
        _require(stat.S_ISFIFO(os.fstat(pipe).st_mode), "FORBIDDEN")
        with os.fdopen(pipe, "rb") as stream:
            message = stream.read(65537)
        _require(len(message) <= 65536, "FORBIDDEN")
        capability = _strict_json(message, "FORBIDDEN")
        _fields(capability, ("task_id", "run_id", "nonce", "gate", "seal"), code="FORBIDDEN")
        roots = _strict_json(locator.encode(), "FORBIDDEN")
        _fields(roots, ("home", "native_home", "native_homes"), code="FORBIDDEN")
        store = TaskStore(**roots, runtime_seal=capability["seal"])
        run_child(store, argv[1], argv[2], capability, runtime_hooks(store))
        return 0
    except TaskError as exc:
        return exc.exit_code
    except (TypeError, ValueError, OSError, KeyError):
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
