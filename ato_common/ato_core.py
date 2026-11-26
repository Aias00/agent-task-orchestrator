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
import stat
import subprocess
import time
import uuid

__all__ = ('now', 'read', 'save', 'digest', 'process_identity', 'Halt',
           'locked', 'alive', 'pid_alive', 'private_save', 'ensure_private_dir')


def now():
    return time.time()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    _save_json(Path(path), data, indent=2)


def _save_json(path, data, *, indent=None, allow_nan=True):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=indent, allow_nan=allow_nan)
            if indent is not None:
                stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_private_dir(path, *, private_from=None):
    """Create missing components privately and validate the managed subtree."""
    path = Path(path).absolute()
    boundary = path if private_from is None else Path(private_from).absolute()
    if path == Path('/') or not path.is_relative_to(boundary) or '..' in path.parts:
        raise ValueError('invalid private directory boundary')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        current = Path('/')
        for part in path.parts[1:]:
            current /= part
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                next_fd = os.open(part, flags, dir_fd=fd)
            else:
                next_fd = os.open(part, flags, dir_fd=fd)
                try:
                    os.fchmod(next_fd, 0o700)
                    os.fsync(fd)
                except BaseException:
                    os.close(next_fd)
                    raise
            os.close(fd)
            fd = next_fd
            if current.is_relative_to(boundary):
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError('private directory must be owned with mode 0700')
    finally:
        os.close(fd)
    return path


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
    path = Path(path)
    if not path.parent.exists():
        ensure_private_dir(path.parent)
    _save_json(path, value, allow_nan=False)
