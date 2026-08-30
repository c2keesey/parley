"""Versioned, privacy-safe runtime status shared by every Parley surface.

The snapshot is an operational contract, never an event log.  Writers update it
under a private flock and publish with ``os.replace``; readers therefore see a
complete old or new generation.  Listener and speech writers own independent
sections.  Reclaiming a section gives it a new instance token, so a delayed
writer from an older process cannot overwrite the replacement.

Version 1 intentionally contains only bounded enums, counts, timestamps,
process ownership, and a tmux pane id.  It must never contain dictated or
spoken text, trigger features, credentials, exception strings, filesystem
paths, voice/model names, or tmux session labels.
"""

import contextvars
import ctypes
import fcntl
import functools
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from parley import config

SCHEMA = "parley.runtime-status"
VERSION = 1
COMPONENTS = frozenset({"listener", "speech"})
LISTENER_STATES = frozenset({"off", "ready", "capturing", "sending", "degraded"})
SYNTHESIS_STATES = frozenset({"idle", "active", "degraded"})
PLAYBACK_STATES = frozenset({"idle", "active", "paused", "degraded"})
PROVIDERS = frozenset({"auto", "openai", "elevenlabs", "macos", "unknown"})
ACTIVE_PROVIDERS = PROVIDERS - {"auto"}
FALLBACK_PROVIDERS = frozenset({"openai", "elevenlabs", "macos"})
ERROR_CODES = frozenset({
    "listener_process_lost",
    "speech_process_lost",
    "snapshot_invalid",
    "target_unavailable",
    "transcription_failed",
    "synthesis_failed",
    "playback_failed",
    "provider_fallback",
})
ERROR_COMPONENTS = frozenset({
    "runtime", "listener", "target", "speech", "provider",
})
ERROR_STAGES = frozenset({
    "status", "listen", "submit", "transcribe", "synthesize", "play",
})
PANE = re.compile(r"^%[0-9]+$")
MAX_ERRORS = 8
MAX_TIMESTAMP = 4_102_444_800  # 2100-01-01; bounds corrupt numeric channels.
MAX_GENERATION = (1 << 63) - 1
MAX_QUEUE_DEPTH = 1_000_000
TOP_LEVEL_KEYS = frozenset({
    "schema", "version", "generation", "updated_at", "health", "writers",
    "listener", "queue", "speech", "target", "provider", "errors",
})
_CURRENT_WRITER = contextvars.ContextVar("parley_runtime_writer", default=None)


@dataclass(frozen=True)
class Writer:
    """Capability for one claimed component generation."""

    component: str
    instance: str
    pid: int
    birth: str


class _ProcBsdInfo(ctypes.Structure):
    """Darwin proc_bsdinfo subset used to distinguish reused PIDs."""

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


def _process_identity(pid):
    """Kernel-backed process birth identity for stale and reused PID defense.

    This is the isolated branch's integration seam.  The PID-safety branch
    should replace it with ``parley.processes.process_identity`` rather than
    retaining two implementations after merge.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "darwin":
        libproc = _darwin_libproc()
        if libproc is None:
            return None
        info = _ProcBsdInfo()
        size = ctypes.sizeof(info)
        result = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
        if result != size or info.pbi_pid != pid:
            return None
        return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
    if sys.platform.startswith("linux"):
        try:
            boot = Path(
                "/proc/sys/kernel/random/boot_id"
            ).read_text(encoding="ascii").strip()
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            start_ticks = stat.rsplit(")", 1)[1].split()[19]
        except (IndexError, OSError):
            return None
        return f"linux:{boot}:{start_ticks}"
    return None


def _paths():
    return config.STATE / "runtime-status.json", config.STATE / "runtime-status.lock"


def _default():
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "generation": 0,
        "updated_at": 0.0,
        "health": "ok",
        "writers": {"listener": None, "speech": None},
        "listener": {"state": "off"},
        "queue": {"depth": 0},
        "speech": {"synthesis": "idle", "playback": "idle"},
        "target": {"kind": "tmux-pane", "id": None, "available": False},
        "provider": {
            "configured": (
                config.PROVIDER if config.PROVIDER in PROVIDERS else "unknown"
            ),
            "active": (
                config.PROVIDER
                if config.PROVIDER in PROVIDERS - {"auto"}
                else "unknown"
            ),
            "fallback": False,
            "fallback_from": None,
        },
        "errors": [],
    }


def _valid_owner(owner):
    if owner is None:
        return True
    return bool(
        isinstance(owner, dict)
        and set(owner) == {"pid", "birth", "instance", "heartbeat_at"}
        and isinstance(owner.get("pid"), int)
        and 0 < owner["pid"] <= (1 << 31) - 1
        and isinstance(owner.get("birth"), str)
        and bool(re.fullmatch(
            r"(?:darwin:[0-9]+:[0-9]+|linux:[0-9a-f-]{36}:[0-9]+)",
            owner["birth"],
        ))
        and isinstance(owner.get("instance"), str)
        and re.fullmatch(r"[0-9a-f]{32}", owner["instance"])
        and _valid_timestamp(owner.get("heartbeat_at"))
    )


def _valid_error(error):
    if not isinstance(error, dict):
        return False
    allowed_keys = {"code", "component", "stage", "at", "provider"}
    return bool(
        set(error) in (allowed_keys, allowed_keys - {"provider"})
        and error.get("code") in ERROR_CODES
        and error.get("component") in ERROR_COMPONENTS
        and error.get("stage") in ERROR_STAGES
        and _valid_timestamp(error.get("at"))
        and (
            "provider" not in error
            or error["provider"] in PROVIDERS - {"auto"}
        )
    )


def _valid_timestamp(value):
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= MAX_TIMESTAMP
    )


def _valid(snapshot):
    try:
        return (
            isinstance(snapshot, dict)
            and set(snapshot) == TOP_LEVEL_KEYS
            and snapshot["schema"] == SCHEMA
            and type(snapshot["version"]) is int
            and snapshot["version"] == VERSION
            and isinstance(snapshot["generation"], int)
            and not isinstance(snapshot["generation"], bool)
            and 0 <= snapshot["generation"] < MAX_GENERATION
            and _valid_timestamp(snapshot["updated_at"])
            and snapshot["health"] in {"ok", "degraded"}
            and set(snapshot["listener"]) == {"state"}
            and snapshot["listener"]["state"] in LISTENER_STATES
            and set(snapshot["queue"]) == {"depth"}
            and isinstance(snapshot["queue"]["depth"], int)
            and not isinstance(snapshot["queue"]["depth"], bool)
            and 0 <= snapshot["queue"]["depth"] <= MAX_QUEUE_DEPTH
            and set(snapshot["speech"]) == {"synthesis", "playback"}
            and snapshot["speech"]["synthesis"] in SYNTHESIS_STATES
            and snapshot["speech"]["playback"] in PLAYBACK_STATES
            and set(snapshot["target"]) == {"kind", "id", "available"}
            and snapshot["target"]["kind"] == "tmux-pane"
            and (
                snapshot["target"]["id"] is None
                or PANE.fullmatch(snapshot["target"]["id"])
            )
            and isinstance(snapshot["target"]["available"], bool)
            and (
                not snapshot["target"]["available"]
                or snapshot["target"]["id"] is not None
            )
            and set(snapshot["provider"]) == {
                "configured", "active", "fallback", "fallback_from",
            }
            and snapshot["provider"]["configured"] in PROVIDERS
            and snapshot["provider"]["active"] in ACTIVE_PROVIDERS
            and isinstance(snapshot["provider"]["fallback"], bool)
            and (
                snapshot["provider"]["fallback_from"] is None
                or snapshot["provider"]["fallback_from"] in FALLBACK_PROVIDERS
            )
            and (
                snapshot["provider"]["fallback"]
                == bool(
                    snapshot["provider"]["fallback_from"]
                    and snapshot["provider"]["fallback_from"]
                    != snapshot["provider"]["active"]
                )
            )
            and isinstance(snapshot["errors"], list)
            and len(snapshot["errors"]) <= MAX_ERRORS
            and all(_valid_error(error) for error in snapshot["errors"])
            and set(snapshot["writers"]) == COMPONENTS
            and all(
                _valid_owner(snapshot["writers"][component])
                for component in COMPONENTS
            )
        )
    except (KeyError, OverflowError, TypeError, ValueError):
        return False


def _owner(writer):
    return {
        "pid": writer.pid,
        "birth": writer.birth,
        "instance": writer.instance,
        "heartbeat_at": time.time(),
    }


def _owns(snapshot, writer):
    if not isinstance(writer, Writer):
        return False
    owner = snapshot["writers"].get(writer.component)
    return bool(
        owner
        and owner.get("pid") == writer.pid
        and owner.get("birth") == writer.birth
        and owner.get("instance") == writer.instance
    )


def _owner_alive(owner):
    try:
        return _process_identity(owner["pid"]) == owner["birth"]
    except (KeyError, TypeError):
        return False


def _queue_depth():
    try:
        queue = config.STATE / "queue"
        return sum(1 for path in queue.iterdir() if path.suffix == ".json")
    except OSError:
        return 0


def _target_available(pane):
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        return False
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and (result.stdout or "").strip() == pane


def _append_error(snapshot, code, component, stage, provider=None):
    error = {
        "code": code,
        "component": component,
        "stage": stage,
        "at": time.time(),
    }
    if provider is not None:
        error["provider"] = provider
    previous = snapshot["errors"][-1] if snapshot["errors"] else None
    comparable = {key: value for key, value in error.items() if key != "at"}
    if previous is None or {
        key: value for key, value in previous.items() if key != "at"
    } != comparable:
        snapshot["errors"].append(error)
        snapshot["errors"] = snapshot["errors"][-MAX_ERRORS:]


def _reconcile(snapshot, external=True):
    changed = False
    if external:
        for component in COMPONENTS:
            owner = snapshot["writers"].get(component)
            if owner is None or _owner_alive(owner):
                continue
            snapshot["writers"][component] = None
            if (
                component == "listener"
                and snapshot["listener"]["state"] != "off"
            ):
                snapshot["listener"]["state"] = "degraded"
                _append_error(
                    snapshot, "listener_process_lost", "listener", "status")
            elif component == "speech":
                active = (
                    snapshot["speech"]["synthesis"] != "idle"
                    or snapshot["speech"]["playback"] != "idle"
                )
                if active:
                    snapshot["speech"]["synthesis"] = "degraded"
                    snapshot["speech"]["playback"] = "degraded"
                    _append_error(
                        snapshot, "speech_process_lost", "speech", "status")
            changed = True

        depth = _queue_depth()
        if snapshot["queue"]["depth"] != depth:
            snapshot["queue"]["depth"] = depth
            changed = True

        pane = snapshot["target"]["id"]
        available = _target_available(pane)
        if snapshot["target"]["available"] != available:
            snapshot["target"]["available"] = available
            changed = True

    listener_busy = snapshot["listener"]["state"] in {"capturing", "sending"}
    if listener_busy and snapshot["speech"]["playback"] == "active":
        snapshot["speech"]["playback"] = "paused"
        changed = True
    elif not listener_busy and snapshot["speech"]["playback"] == "paused":
        snapshot["speech"]["playback"] = "active"
        changed = True

    health = "degraded" if (
        snapshot["listener"]["state"] == "degraded"
        or snapshot["speech"]["synthesis"] == "degraded"
        or snapshot["speech"]["playback"] == "degraded"
    ) else "ok"
    if snapshot["health"] != health:
        snapshot["health"] = health
        changed = True
    return changed


def _load_unlocked():
    path, _lock = _paths()
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return snapshot if _valid(snapshot) else None


def _write_unlocked(snapshot):
    path, _lock = _paths()
    config.private_directory(path.parent)
    snapshot["generation"] += 1
    snapshot["updated_at"] = time.time()
    descriptor, temporary = tempfile.mkstemp(
        prefix=".runtime-status.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(snapshot, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _locked_update(mutator, recover=True):
    path, lock_path = _paths()
    config.private_directory(path.parent)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(lock_path, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        snapshot = _load_unlocked()
        invalid = snapshot is None
        if invalid:
            snapshot = _default()
            if path.exists():
                _append_error(snapshot, "snapshot_invalid", "runtime", "status")
        changed = _reconcile(snapshot) if recover else False
        result = mutator(snapshot)
        if recover and result is not False:
            changed = _reconcile(snapshot, external=False) or changed
        if invalid or changed or result is not False:
            _write_unlocked(snapshot)
        return snapshot, result


def _best_effort_update(mutator, recover=True):
    """Keep status I/O failures from taking down listener or speech work."""
    try:
        return _locked_update(mutator, recover)
    except OSError:
        return None, False


def snapshot():
    """Return a coherent snapshot, recovering dead owners before publishing it."""
    value, _result = _best_effort_update(lambda _snapshot: False)
    if value is not None:
        return value
    value = _default()
    value["health"] = "degraded"
    _append_error(value, "snapshot_invalid", "runtime", "status")
    return value


def claim(component):
    """Claim one component and invalidate every older writer capability."""
    if component not in COMPONENTS:
        raise ValueError(f"unknown runtime component: {component}")
    pid = os.getpid()
    birth = _process_identity(pid)
    if birth is None:
        return None
    writer = Writer(component, secrets.token_hex(16), pid, birth)

    def mutate(value):
        value["writers"][component] = _owner(writer)
        return True

    _value, accepted = _best_effort_update(mutate)
    return writer if accepted else None


@contextmanager
def writer_context(writer):
    """Make a capability available to nested provider code in this context."""
    token = _CURRENT_WRITER.set(writer if isinstance(writer, Writer) else None)
    try:
        yield
    finally:
        _CURRENT_WRITER.reset(token)


def current_writer(component):
    writer = _CURRENT_WRITER.get()
    if isinstance(writer, Writer) and writer.component == component:
        return writer
    return None


def heartbeat(writer):
    def mutate(value):
        if not _owns(value, writer):
            return False
        value["writers"][writer.component]["heartbeat_at"] = time.time()
        return True

    _value, accepted = _best_effort_update(mutate)
    return accepted


def set_listener(writer, state):
    if state not in LISTENER_STATES - {"off"}:
        raise ValueError(f"invalid listener state: {state}")
    if not isinstance(writer, Writer) or writer.component != "listener":
        return False

    def mutate(value):
        if not _owns(value, writer):
            return False
        value["listener"]["state"] = state
        value["writers"]["listener"]["heartbeat_at"] = time.time()
        return True

    _value, accepted = _best_effort_update(mutate)
    return accepted


def set_speech(writer, *, synthesis=None, playback=None):
    if synthesis is not None and synthesis not in SYNTHESIS_STATES:
        raise ValueError(f"invalid synthesis state: {synthesis}")
    if playback is not None and playback not in PLAYBACK_STATES:
        raise ValueError(f"invalid playback state: {playback}")
    if not isinstance(writer, Writer) or writer.component != "speech":
        return False

    def mutate(value):
        if not _owns(value, writer):
            return False
        if synthesis is not None:
            value["speech"]["synthesis"] = synthesis
        if playback is not None:
            value["speech"]["playback"] = playback
        value["writers"]["speech"]["heartbeat_at"] = time.time()
        return True

    _value, accepted = _best_effort_update(mutate)
    return accepted


def release(writer, **final):
    """Release only the still-current writer, optionally with a final state."""
    if not isinstance(writer, Writer) or writer.component not in COMPONENTS:
        return False

    def mutate(value):
        if not _owns(value, writer):
            return False
        if writer.component == "listener":
            state = final.get("state", "off")
            if state not in LISTENER_STATES:
                raise ValueError(f"invalid listener state: {state}")
            value["listener"]["state"] = state
        else:
            synthesis = final.get("synthesis", "idle")
            playback = final.get("playback", "idle")
            if synthesis not in SYNTHESIS_STATES or playback not in PLAYBACK_STATES:
                raise ValueError("invalid final speech state")
            value["speech"].update(
                synthesis=synthesis,
                playback=playback,
            )
        value["writers"][writer.component] = None
        return True

    _value, accepted = _best_effort_update(mutate)
    return accepted


def manager_listener_off():
    """Invalidate the current listener writer and publish an explicit stop."""
    def mutate(value):
        value["writers"]["listener"] = None
        value["listener"]["state"] = "off"
        return True

    _best_effort_update(mutate)


def _target_mutation(value, pane, available):
    safe_pane = (
        pane if isinstance(pane, str) and PANE.fullmatch(pane) else None
    )
    if available is None:
        available = _target_available(safe_pane)
    value["target"] = {
        "kind": "tmux-pane",
        "id": safe_pane,
        "available": bool(available and safe_pane),
    }


def manager_set_target(pane, available=None):
    """Set pre-launch routing and invalidate an older listener capability."""
    def mutate(value):
        value["writers"]["listener"] = None
        value["listener"]["state"] = "off"
        _target_mutation(value, pane, available)
        return True

    _best_effort_update(mutate)


def set_target(writer, pane, available=None):
    """Update routing only for the current listener owner."""
    if not isinstance(writer, Writer) or writer.component != "listener":
        return False

    def mutate(value):
        if not _owns(value, writer):
            return False
        _target_mutation(value, pane, available)
        value["writers"]["listener"]["heartbeat_at"] = time.time()
        return True

    _value, accepted = _best_effort_update(mutate)
    return accepted


def refresh_queue():
    """Publish queue depth without inspecting any queued content."""
    def mutate(value):
        depth = _queue_depth()
        if value["queue"]["depth"] == depth:
            return False
        value["queue"]["depth"] = depth
        return True

    _best_effort_update(mutate)


def set_provider(writer, active, fallback_from=None):
    if not isinstance(writer, Writer) or writer.component != "speech":
        return False
    active = (
        active
        if isinstance(active, str) and active in ACTIVE_PROVIDERS
        else "unknown"
    )
    fallback_from = (
        fallback_from
        if isinstance(fallback_from, str)
        and fallback_from in FALLBACK_PROVIDERS
        else None
    )

    def mutate(value):
        if not _owns(value, writer):
            return False
        value["provider"].update(
            active=active,
            fallback=bool(fallback_from and fallback_from != active),
            fallback_from=(fallback_from if fallback_from != active else None),
        )
        value["writers"]["speech"]["heartbeat_at"] = time.time()
        return True

    _value, accepted = _best_effort_update(mutate)
    return accepted


def record_error(writer, code, component, stage, provider=None):
    """Record an allow-listed error; arbitrary details are intentionally impossible."""
    if code not in ERROR_CODES:
        raise ValueError(f"unknown runtime error code: {code}")
    if component not in ERROR_COMPONENTS:
        raise ValueError(f"unknown runtime error component: {component}")
    if stage not in ERROR_STAGES:
        raise ValueError(f"unknown runtime error stage: {stage}")
    if (
        provider is not None
        and (
            not isinstance(provider, str)
            or provider not in PROVIDERS - {"auto"}
        )
    ):
        provider = "unknown"
    owned_components = {
        "listener": {"listener", "target"},
        "speech": {"speech", "provider"},
    }
    if (
        not isinstance(writer, Writer)
        or writer.component not in owned_components
        or component not in owned_components[writer.component]
    ):
        return False

    def mutate(value):
        if not _owns(value, writer):
            return False
        _append_error(value, code, component, stage, provider)
        value["writers"][writer.component]["heartbeat_at"] = time.time()
        return True

    _value, accepted = _best_effort_update(mutate)
    return accepted
