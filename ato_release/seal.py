"""Runtime verification for source-selected public and private entrypoints."""
from contextlib import ExitStack, contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys

from ato_release.support import Refused, file_record, launcher_bytes, release_lock, safe_record, verify_snapshot
from ato_task.ato_task_store import TaskError


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ".release-manifest.json"


def runtime_seal(*, required=False, expected=None, full=False):
    path = ROOT / MANIFEST
    pinned = os.environ.get("ATO_LAUNCH_SEAL")
    if not path.exists() and not path.is_symlink() and not (required or expected or pinned):
        return None
    try:
        data, _ = safe_record(path)
        digest = hashlib.sha256(data).hexdigest()
        if (pinned is not None and pinned != digest) or (expected is not None and expected != digest):
            raise ValueError
        if Path(sys.executable).absolute() != ROOT / ".venv/bin/python":
            raise ValueError
        manifest = json.loads(data)
        verify_snapshot(ROOT, manifest["commit"], full=full)
        return digest
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        raise TaskError("RUNTIME_MISMATCH") from None


def installed_boundary(seal):
    """Keep installed mutations outside a concurrent executable cutover."""
    locator = os.environ.get("ATO_LAUNCHER")
    if not locator:
        return nullcontext()

    @contextmanager
    def guard():
        with ExitStack() as stack:
            try:
                path = Path(locator).resolve(strict=True)
                stack.enter_context(release_lock(path.parent, shared=True))
                row = file_record(path)
                expected = launcher_bytes(ROOT, Path(sys.executable).absolute(), seal)
                if path.name != "ato" or row is None or row["sha256"] != hashlib.sha256(expected).hexdigest():
                    raise TaskError("RUNTIME_MISMATCH")
            except BlockingIOError:
                raise TaskError("BUSY") from None
            except (Refused, OSError, RuntimeError):
                raise TaskError("RUNTIME_MISMATCH") from None
            yield

    return guard()
