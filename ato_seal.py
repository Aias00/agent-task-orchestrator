"""Runtime verification for source-selected public and private entrypoints."""
from contextlib import contextmanager, nullcontext
import hashlib
import os
from pathlib import Path
import sys

from ato_task_store import TaskError


ROOT = Path(__file__).resolve().parent
MANIFEST = ".release-manifest.json"


def runtime_seal(*, required=False, expected=None):
    path = ROOT / MANIFEST
    pinned = os.environ.get("ATO_LAUNCH_SEAL")
    if not path.exists() and not path.is_symlink() and not (required or expected or pinned):
        return None
    try:
        from scripts.release_snapshot import read, verify_snapshot
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if (pinned is not None and pinned != digest) or (expected is not None and expected != digest):
            raise ValueError
        if Path(sys.executable).absolute() != ROOT / ".venv/bin/python":
            raise ValueError
        manifest = read(path)
        verify_snapshot(ROOT, manifest["commit"])
        return digest
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        raise TaskError("RUNTIME_MISMATCH") from None


def installed_boundary(seal):
    """Keep installed mutations outside a concurrent executable cutover."""
    from scripts.release_snapshot import file_record, launcher_bytes, release_lock

    locator = os.environ.get("ATO_LAUNCHER")
    if not locator:
        return nullcontext()

    @contextmanager
    def guard():
        path = Path(locator).absolute()
        try:
            with release_lock(path.parent, shared=True):
                row = file_record(path)
                expected = launcher_bytes(ROOT, Path(sys.executable).absolute(), seal)
                if path.name != "ato" or row is None or row["sha256"] != hashlib.sha256(expected).hexdigest():
                    raise TaskError("RUNTIME_MISMATCH")
                yield
        except BlockingIOError:
            raise TaskError("BUSY") from None

    return guard()
