"""Fail-closed ownership records for Parley runtime processes.

A PID says where a process is now, not which process Parley launched.  Pair it
with the kernel's process birth identity before using it for status or signals.
Legacy PID-only files are deliberately invalid: trusting them after a PID has
been reused is how an unrelated process can be mistaken for Parley's.
"""
import ctypes
import fcntl
import functools
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from parley import config


@dataclass(frozen=True)
class Ownership:
    path: Path
    pid: int
    birth: str
    kind: str


class _ProcBsdInfo(ctypes.Structure):
    """Darwin's proc_bsdinfo; start time has microsecond resolution."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@functools.lru_cache(maxsize=1)
def _darwin_libproc():
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
    except (AttributeError, OSError):
        return None
    return libproc


def _darwin_identity(pid):
    libproc = _darwin_libproc()
    if libproc is None:
        return None
    info = _ProcBsdInfo()
    size = ctypes.sizeof(info)
    result = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    if result != size or info.pbi_pid != pid:
        return None
    return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"


def _linux_identity(pid):
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Fields after the final ')' begin at field 3; starttime is field 22.
        start_ticks = stat.rsplit(")", 1)[1].split()[19]
    except (IndexError, OSError):
        return None
    return f"linux:{boot}:{start_ticks}"


def process_identity(pid):
    """Return a kernel birth identity, or None when ownership cannot be proven."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "darwin":
        return _darwin_identity(pid)
    if sys.platform.startswith("linux"):
        return _linux_identity(pid)
    return None


@contextmanager
def _locked(path):
    config.private_directory(path.parent)
    lock_path = path.parent / ".ownership.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    lock = os.fdopen(descriptor, "r+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _payload(ownership):
    return {
        "version": 1,
        "pid": ownership.pid,
        "birth": ownership.birth,
        "kind": ownership.kind,
    }


def _read(path):
    try:
        payload = json.loads(path.read_text())
        pid = payload["pid"]
        birth = payload["birth"]
        kind = payload["kind"]
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if (payload.get("version") != 1 or not isinstance(pid, int) or pid <= 0
            or not isinstance(birth, str) or not birth
            or not isinstance(kind, str) or not kind):
        return None
    return Ownership(path, pid, birth, kind)


def claim(path, pid, kind):
    """Publish ownership only while *pid* still has the observed identity."""
    path = Path(path)
    birth = process_identity(pid)
    if birth is None:
        return None
    ownership = Ownership(path, pid, birth, kind)
    with _locked(path):
        if process_identity(pid) != birth:
            return None
        config.private_write(path, json.dumps(_payload(ownership), sort_keys=True))
    return ownership


def claim_in(directory, pid, kind):
    """Create a unique marker for one of several concurrent owned children."""
    directory = Path(directory)
    path = directory / f"{kind}-{pid}-{time.time_ns()}.json"
    return claim(path, pid, kind)


def owned(path, kind=None):
    """Return proven ownership and recover invalid, exited, or reused state."""
    path = Path(path)
    with _locked(path):
        ownership = _read(path)
        if (ownership is not None
                and (kind is None or ownership.kind == kind)
                and process_identity(ownership.pid) == ownership.birth):
            return ownership
        path.unlink(missing_ok=True)
    return None


def owned_pid(path, kind=None):
    """Return a proven owned PID for display/status, never for signaling."""
    ownership = owned(path, kind)
    if ownership is not None:
        return ownership.pid
    return 0


def owned_pids(directory, kind=None):
    """Return live owned PIDs for display/status while pruning stale markers."""
    directory = Path(directory)
    try:
        markers = list(directory.glob("*.json"))
    except OSError:
        return []
    return [pid for marker in markers if (pid := owned_pid(marker, kind))]


def send_signal(target, sig, kind=None):
    """Signal only a still-current, birth-qualified ownership record.

    ``target`` may be a path or a previously read ``Ownership`` capability.
    The record and kernel birth identity are both revalidated while the
    ownership lock is held, immediately before the only stored-PID signal site
    in Parley. A replaced capability can therefore never signal its successor.
    """
    expected = target if isinstance(target, Ownership) else None
    path = expected.path if expected is not None else Path(target)
    with _locked(path):
        current = _read(path)
        if (current is None
                or (expected is not None and current != expected)
                or (kind is not None and current.kind != kind)
                or process_identity(current.pid) != current.birth):
            if expected is None or current == expected:
                path.unlink(missing_ok=True)
            return False
        # Recheck at the signal boundary so an identity change observed after
        # record validation fails closed without invoking os.kill.
        if process_identity(current.pid) != current.birth:
            path.unlink(missing_ok=True)
            return False
        try:
            os.kill(current.pid, sig)
        except OSError:
            if process_identity(current.pid) != current.birth:
                path.unlink(missing_ok=True)
            return False
    return True


def signal_all(directory, sig, kind=None):
    """Signal each independently birth-qualified child marker in a directory."""
    directory = Path(directory)
    try:
        markers = list(directory.glob("*.json"))
    except OSError:
        return 0
    return sum(bool(send_signal(marker, sig, kind)) for marker in markers)


def release(ownership):
    """Remove a marker only if it still describes this exact ownership claim."""
    if ownership is None:
        return False
    with _locked(ownership.path):
        if _read(ownership.path) != ownership:
            return False
        ownership.path.unlink(missing_ok=True)
    return True


def clear(path):
    """Explicitly discard a marker without trusting or signaling its PID."""
    path = Path(path)
    with _locked(path):
        existed = path.exists()
        path.unlink(missing_ok=True)
    return existed
