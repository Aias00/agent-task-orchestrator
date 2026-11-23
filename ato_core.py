"""Business-independent filesystem, time, identity and locking primitives.

Only the Python standard library belongs here. Importing this module performs no
registration, configuration lookup, runtime activation or state migration.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

__all__ = ('now', 'read', 'save', 'digest', 'process_identity', 'Halt',
           'locked', 'alive', 'pid_alive')


def now():
    return time.time()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def process_identity(pid):
    """Best-effort UTC/C process start identity, independent of caller settings."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        data = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                              env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'},
                              capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        return hashlib.sha256(data.encode()).hexdigest() if data else None
    except (OSError, subprocess.SubprocessError):
        return None


# Keep the genuine callable separate from the patchable public facade. A test
# replacing process_identity must never trigger a real ps through the fallback.
_PROCESS_IDENTITY = process_identity


def _legacy_localtime_identity(pid):
    """Read an old host-local start hash, ignoring the caller's TZ and locale.

    Deliberately omit TZ so macOS uses /etc/localtime. This is a comparison-only
    migration bridge; new records always use process_identity's canonical UTC.
    """
    if type(pid) is not int or pid <= 0:
        return None
    try:
        data = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                              env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                              capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        return hashlib.sha256(data.encode()).hexdigest() if data else None
    except (OSError, subprocess.SubprocessError):
        return None


class Halt(Exception):
    pass


@contextlib.contextmanager
def locked(path, blocking=True):
    with Path(path).open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Unknown ownership must not cause another execution.


def pid_alive(info, *, alive_fn=None, identity_fn=None):
    """Optional callables preserve compatibility facade injection points."""
    check_alive = alive if alive_fn is None else alive_fn
    identity = process_identity if identity_fn is None else identity_fn
    if not check_alive(info.get('pid')):
        return False
    if 'pid_identity' not in info:
        return True
    if identity(info['pid']) == info['pid_identity']:
        return True
    return (identity is process_identity and identity is _PROCESS_IDENTITY
        and _legacy_localtime_identity(info['pid']) == info['pid_identity'])


def private_save(path, value):
    import json
    import os
    import secrets
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name+'.'+secrets.token_hex(8)+'.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
