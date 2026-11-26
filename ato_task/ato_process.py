"""Exact local process identities used by the unified execution boundary."""
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys


class _BSDInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32), ("rfu", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64),
    ]


_libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True) if sys.platform == "darwin" else None
if _libproc is not None:
    _libproc.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int)
    _libproc.proc_listpids.argtypes = (ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_int)


def _observe(pid):
    if sys.platform == "darwin":
        row = _BSDInfo()
        size = _libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(row), ctypes.sizeof(row))
        if size != ctypes.sizeof(row) or row.pid != pid or not row.start_sec:
            error = ctypes.get_errno()
            raise OSError(error or errno.EACCES, "process birth unavailable")
        return row.pgid, row.status == 5, f"{row.start_sec}:{row.start_usec}"
    if sys.platform == "linux":
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return int(fields[2]), fields[0] in (b"Z", b"X"), f"{boot}:{fields[19].decode('ascii')}"
    raise OSError("process birth unavailable")


def _birth(pid, stamp):
    return hashlib.sha256(f"{pid}:{stamp}".encode()).hexdigest()


def identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        _, zombie, stamp = _observe(pid)
        return None if zombie else _birth(pid, stamp)
    except (OSError, ValueError, IndexError):
        return None


def _darwin_zombie(pid):
    if sys.platform != "darwin":
        return False
    try:
        status = subprocess.check_output(
            ["/bin/ps", "-p", str(pid), "-o", "stat=,lstart="],
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"},
            text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
        return status.startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return False


def disposition(pid, birth):
    """A mismatch proves only that the current PID belongs to another process."""
    if type(pid) is not int or pid <= 0 or not birth:
        return "unknown"
    observed = identity(pid)
    if observed:
        return "live" if observed == birth else "reused"
    unobserved = False
    try:
        _, zombie, stamp = _observe(pid)
        if zombie:
            return "absent" if _birth(pid, stamp) == birth else "reused"
    except (OSError, ValueError, IndexError):
        unobserved = True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except OSError:
        pass
    return "absent" if unobserved and _darwin_zombie(pid) else "unknown"


def _absent_group(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def group_members(group):
    """Empty only on verified absence, including when enumeration is unavailable."""
    if _absent_group(group):
        return {}
    rows = _group_members(group)
    if rows is None and _absent_group(group):
        return {}
    return rows


def _group_members(group):
    try:
        if sys.platform == "darwin":
            # PROC_PGRP_ONLY returns a byte count when a buffer is supplied.
            # Use a bounded oversized buffer and reject a truncated listing.
            pids = (ctypes.c_int * 16384)()
            size = _libproc.proc_listpids(2, group, pids, ctypes.sizeof(pids))
            if size <= 0 or size >= ctypes.sizeof(pids):
                return None
            candidates = [pid for pid in pids[:size // ctypes.sizeof(ctypes.c_int)] if pid]
        elif sys.platform == "linux":
            candidates = [int(name) for name in os.listdir("/proc") if name.isascii() and name.isdigit()]
        else:
            return None
        rows = {}
        for pid in candidates:
            try:
                if os.getpgid(pid) != group:
                    continue
                pgid, zombie, stamp = _observe(pid)
                if pgid == group and not zombie:
                    rows[pid] = _birth(pid, stamp)
            except OSError as exc:
                if exc.errno not in (errno.ESRCH, errno.ENOENT):
                    try:
                        if os.getpgid(pid) == group:
                            return None
                    except ProcessLookupError:
                        pass
        return rows
    except (OSError, ValueError, IndexError):
        return None
