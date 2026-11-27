"""Exact local process identities and PID-reuse-safe process-group cleanup."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


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
    _libproc.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    _libproc.proc_listpids.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)


def observe(pid: int) -> tuple[int, bool, str]:
    if sys.platform == "darwin":
        row = _BSDInfo()
        size = _libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(row), ctypes.sizeof(row))
        if size != ctypes.sizeof(row) or row.pid != pid or not row.start_sec:
            raise OSError(ctypes.get_errno() or errno.EACCES, "process birth unavailable")
        return row.pgid, row.status == 5, f"{row.start_sec}:{row.start_usec}"
    if sys.platform == "linux":
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return int(fields[2]), fields[0] in (b"Z", b"X"), f"{boot}:{fields[19].decode('ascii')}"
    raise OSError("process birth unavailable")


def _birth(pid: int, stamp: str) -> str:
    return hashlib.sha256(f"{pid}:{stamp}".encode()).hexdigest()


def identity(pid: int | None) -> str | None:
    if type(pid) is not int or pid <= 0:
        return None
    try:
        _, zombie, stamp = observe(pid)
        return None if zombie else _birth(pid, stamp)
    except (OSError, ValueError, IndexError):
        return None


def darwin_zombie(pid: int) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        status = subprocess.check_output(
            ["/bin/ps", "-p", str(pid), "-o", "stat=,lstart="],
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"},
            text=True, stderr=subprocess.DEVNULL, timeout=2,
        ).strip()
        return status.startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return False


def probe_exited(process: subprocess.Popen) -> bool:
    """Detect child exit WITHOUT reaping so killpg stays PID-reuse-safe."""
    if isinstance(process, subprocess.Popen):
        if process.returncode is not None:
            return True
        try:
            _, zombie, _ = observe(process.pid)
            return zombie
        except (OSError, ValueError, IndexError):
            if darwin_zombie(process.pid):
                return True
            return process.poll() is not None
    return process.poll() is not None


def disposition(pid: int | None, birth: str | None) -> str:
    if type(pid) is not int or pid <= 0 or not birth:
        return "unknown"
    observed = identity(pid)
    if observed:
        return "live" if observed == birth else "reused"
    unobserved = False
    try:
        _, zombie, stamp = observe(pid)
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
    return "absent" if unobserved and darwin_zombie(pid) else "unknown"


def _absent_group(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def group_members(group: int) -> dict[int, str] | None:
    if _absent_group(group):
        return {}
    try:
        if sys.platform == "darwin":
            pids = (ctypes.c_int * 16384)()
            size = _libproc.proc_listpids(2, group, pids, ctypes.sizeof(pids))
            if size <= 0 or size >= ctypes.sizeof(pids):
                return {} if _absent_group(group) else None
            candidates = [p for p in pids[:size // ctypes.sizeof(ctypes.c_int)] if p]
        elif sys.platform == "linux":
            candidates = [int(name) for name in os.listdir("/proc") if name.isascii() and name.isdigit()]
        else:
            return None
        rows: dict[int, str] = {}
        for pid in candidates:
            try:
                if os.getpgid(pid) != group:
                    continue
                pgid, zombie, stamp = observe(pid)
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
        return {} if _absent_group(group) else None


def stop_pid_group(pid: int, birth: str, *, grace_seconds: float = 2.0) -> bool:
    """Stop a verified process group without signaling a reused PID."""
    state = disposition(pid, birth)
    if state in ("absent", "reused"):
        return True
    if state != "live":
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if disposition(pid, birth) != "live" and not group_members(pid):
            return True
        time.sleep(0.05)
    if disposition(pid, birth) == "live":
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            if disposition(pid, birth) != "live" and not group_members(pid):
                return True
            time.sleep(0.05)
    return disposition(pid, birth) != "live" and not group_members(pid)


def terminate_popen(process: subprocess.Popen, *, grace_seconds: float = 2.0) -> int | None:
    """Safely terminate a Popen created with start_new_session=True and sweep descendants before reaping."""
    if isinstance(process, subprocess.Popen) and process.returncode is not None:
        return process.returncode
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if probe_exited(process):
            break
        time.sleep(0.05)
    if not (isinstance(process, subprocess.Popen) and process.returncode is not None):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return process.wait()
