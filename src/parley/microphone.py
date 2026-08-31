"""Privacy-safe microphone inventory and capture outcome reporting.

Diagnostics in this module never open an audio stream. Device discovery asks
AVFoundation for metadata through ffmpeg's ``-list_devices`` mode; capture
readiness is written only by the normal listener after bytes arrive from its
already-requested stream.

Backend stderr is untrusted. It is used only for exact allow-listed
classification and is never returned, persisted, printed, or logged.
"""

import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass

from parley import config

SCHEMA_VERSION = 1
CONTRACT_VERSION = 1
SYSTEM_SETTINGS_PANE = "System Settings > Privacy & Security > Microphone"
STATES = frozenset({"unknown", "denied", "unavailable", "busy", "ready", "failed"})
REASONS = frozenset({
    "not_checked",
    "checking",
    "permission_denied",
    "device_missing",
    "device_reindexed",
    "device_busy",
    "capture_active",
    "capture_failed",
    "capture_lost",
})
READY_STALE_SECONDS = 3.0
MAX_BACKEND_ERROR_BYTES = 8192

_PREFIX = re.compile(r"^\[AVFoundation indev @ [^\]]+\]\s+")
_DEVICE = re.compile(
    r"\[(?P<index>[0-9]+)\]\s+(?P<name>.*?)"
    r"(?:\s{2}\[uid:(?P<uid>[^\]]+)\])?"
    r"(?:\s+\[serial:(?P<serial>[^\]]+)\])?$"
)

_DENIED_MARKERS = (
    "avfoundation audio device access is denied",
    "not authorized to use capture devices",
    "not permitted to access the microphone",
    "permission denied",
)
_BUSY_MARKERS = (
    "device is in use by another application",
    "device in use by another application",
    "device or resource busy",
)
_UNAVAILABLE_MARKERS = (
    "invalid audio device index",
    "audio device not found",
    "audio capture device with unique id",
    "audio capture device with serial number",
    "no av capture device found",
)


@dataclass(frozen=True)
class MicrophoneDevice:
    index: int
    name: str
    uid: str | None = None
    serial: str | None = None
    supports_stable_selector: bool = False

    @property
    def selector(self):
        if self.supports_stable_selector and self.uid:
            return f"uid:{self.uid}"
        return str(self.index)

    def public(self):
        return {**asdict(self), "selector": self.selector}


class DeviceDiscoveryUnavailable(RuntimeError):
    """Device metadata could not be enumerated without capture."""


class CaptureUnavailable(RuntimeError):
    def __init__(self, state, reason, device=None):
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.device = device


def configured_selector():
    """Use the shared non-secret configuration seam when it is available."""
    configured = str(
        getattr(config, "MIC_DEVICE", os.environ.get("PARLEY_MIC", "0"))
    )
    try:
        return validate_selector(configured)
    except ValueError:
        return "0"


def _clean(value, maximum):
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char for char in value.strip() if ord(char) >= 32)
    return cleaned[:maximum]


def validate_selector(selector):
    cleaned = _clean(str(selector), 240)
    if not cleaned or cleaned != str(selector).strip() or cleaned.startswith("-"):
        raise ValueError("invalid microphone selector")
    return cleaned


def ffmpeg_binary():
    return shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def parse_devices(output, supports_stable_selectors=False):
    """Parse only allow-listed AVFoundation audio inventory records."""
    devices = []
    in_audio = False
    for raw_line in output.splitlines():
        line = _PREFIX.sub("", raw_line, count=1).strip()
        if line == "AVFoundation audio devices:":
            in_audio = True
            continue
        if not in_audio:
            continue
        match = _DEVICE.fullmatch(line)
        if not match:
            continue
        name = _clean(match.group("name"), 120)
        uid = _clean(match.group("uid") or "", 240) or None
        serial = _clean(match.group("serial") or "", 120) or None
        if name:
            devices.append(MicrophoneDevice(
                index=int(match.group("index")),
                name=name,
                uid=uid,
                serial=serial,
                supports_stable_selector=supports_stable_selectors,
            ))
    if not in_audio:
        raise DeviceDiscoveryUnavailable("audio inventory was not available")
    return devices


def enumerate_devices(binary=None):
    """Enumerate metadata without opening, recording, or retaining audio."""
    executable = binary or ffmpeg_binary()
    command = [
        executable,
        "-hide_banner",
        "-f", "avfoundation",
        "-list_devices", "true",
        "-i", "",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeviceDiscoveryUnavailable("device discovery could not run") from exc
    return parse_devices(
        result.stderr,
        supports_stable_selectors=_supports_stable_selectors(executable),
    )


def _supports_stable_selectors(binary):
    """Trust only the exact option exposed by the installed AVFoundation backend."""
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-h", "demuxer=avfoundation"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    help_text = f"{result.stdout}\n{result.stderr}"
    return bool(re.search(r"(?m)^\s*-audio_device_id\s", help_text))


def resolve_device(selector, devices):
    selector = validate_selector(selector)
    if selector.startswith("uid:"):
        wanted = selector[4:]
        return next((item for item in devices if item.uid == wanted), None)
    if selector.startswith("serial:"):
        wanted = selector[7:]
        return next((item for item in devices if item.serial == wanted), None)
    if selector.isdigit():
        wanted = int(selector)
        return next((item for item in devices if item.index == wanted), None)
    exact = [item for item in devices if item.name == selector]
    return exact[0] if len(exact) == 1 else None


def capture_command(selector, binary=None):
    selector = validate_selector(selector)
    command = [binary or ffmpeg_binary(), "-hide_banner", "-loglevel", "error"]
    if selector.startswith("uid:"):
        command.extend(["-f", "avfoundation", "-audio_device_id", selector[4:],
                        "-i", ":none"])
    elif selector.startswith("serial:"):
        raise ValueError("serial microphone selection is not supported")
    else:
        command.extend(["-f", "avfoundation", "-i", f":{selector}"])
    command.extend(["-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
    return command


def classify_capture_error(raw):
    """Map untrusted backend output to a bounded state and reason only."""
    if isinstance(raw, bytes):
        text = raw[:MAX_BACKEND_ERROR_BYTES].decode("utf-8", errors="replace")
    else:
        text = str(raw)[:MAX_BACKEND_ERROR_BYTES]
    lowered = text.lower()
    if any(marker in lowered for marker in _DENIED_MARKERS):
        return "denied", "permission_denied"
    if any(marker in lowered for marker in _BUSY_MARKERS):
        return "busy", "device_busy"
    missing_identity = (
        "not found" in lowered
        and any(marker in lowered for marker in _UNAVAILABLE_MARKERS[2:4])
    )
    if any(marker in lowered for marker in _UNAVAILABLE_MARKERS[:2]) or (
        missing_identity or _UNAVAILABLE_MARKERS[4] in lowered
    ):
        return "unavailable", "device_missing"
    return "failed", "capture_failed"


def _status_path():
    return config.STATE / "microphone-status.json"


def _default_status(selector=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "unknown",
        "reason": "not_checked",
        "selector": selector or configured_selector(),
        "device": None,
        "pid": 0,
        "updated_at": 0.0,
    }


def _valid_status(document):
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "state", "reason", "selector", "device", "pid", "updated_at",
    }:
        return False
    if document["schema_version"] != SCHEMA_VERSION:
        return False
    combinations = {
        "unknown": {"not_checked", "checking"},
        "denied": {"permission_denied"},
        "unavailable": {"device_missing", "device_reindexed"},
        "busy": {"device_busy"},
        "ready": {"capture_active"},
        "failed": {"capture_failed", "capture_lost"},
    }
    if (
        document["state"] not in STATES
        or document["reason"] not in combinations[document["state"]]
    ):
        return False
    if not _valid_selector_text(document["selector"]):
        return False
    if (
        not isinstance(document["pid"], int)
        or isinstance(document["pid"], bool)
        or not 0 <= document["pid"] <= (1 << 31) - 1
    ):
        return False
    if (
        not isinstance(document["updated_at"], (int, float))
        or isinstance(document["updated_at"], bool)
        or not math.isfinite(document["updated_at"])
        or document["updated_at"] < 0
    ):
        return False
    device = document["device"]
    if device is None:
        return True
    if not isinstance(device, dict):
        return False
    expected_selector = (
        f"uid:{device['uid']}"
        if device.get("supports_stable_selector") and device.get("uid")
        else str(device.get("index"))
    )
    return bool(
        set(device) == {
            "index", "name", "uid", "serial", "supports_stable_selector",
            "selector",
        }
        and isinstance(device["index"], int)
        and not isinstance(device["index"], bool)
        and 0 <= device["index"] <= (1 << 31) - 1
        and isinstance(device["supports_stable_selector"], bool)
        and _valid_text(device["name"], 120)
        and _valid_selector_text(device["selector"])
        and device["selector"] == expected_selector
        and (device["uid"] is None or _valid_text(device["uid"], 240))
        and (device["serial"] is None or _valid_text(device["serial"], 120))
    )


def _valid_text(value, maximum):
    return bool(
        isinstance(value, str)
        and value
        and len(value) <= maximum
        and all(ord(char) >= 32 for char in value)
    )


def _valid_selector_text(value):
    if not _valid_text(value, 240):
        return False
    try:
        return validate_selector(value) == value
    except ValueError:
        return False


def _read_raw_status():
    try:
        document = json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_status()
    return document if _valid_status(document) else _default_status()


def write_status(state, reason, selector, device=None, pid=None):
    if state not in STATES or reason not in REASONS:
        raise ValueError("invalid microphone status")
    selector = validate_selector(selector)
    document = {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "reason": reason,
        "selector": selector,
        "device": device.public() if device else None,
        "pid": os.getpid() if pid is None else pid,
        "updated_at": time.time(),
    }
    if not _valid_status(document):
        raise ValueError("invalid microphone status document")
    config.private_directory(config.STATE)
    path = _status_path()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return document


def read_status(active_pid=None, now=None):
    """Return current capture evidence, never permission inferred from a PID."""
    document = _read_raw_status()
    if document["state"] != "ready":
        return document
    current_time = time.time() if now is None else now
    age = current_time - document["updated_at"]
    fresh = 0 <= age <= READY_STALE_SECONDS
    same_writer = bool(active_pid and document["pid"] == active_pid)
    if fresh and same_writer:
        return document
    stale = dict(document)
    stale["state"] = "failed"
    stale["reason"] = "capture_lost"
    return stale


def public_status(active_pid=None):
    document = read_status(active_pid)
    return {
        "state": document["state"],
        "reason": document["reason"],
        "selector": document["selector"],
        "device": document["device"],
    }


def mark_stopped():
    """Mark capture inactive while retaining identity for drift detection."""
    previous = _read_raw_status()
    device = previous.get("device")
    retained = None
    if device:
        retained = MicrophoneDevice(
            index=device["index"],
            name=device["name"],
            uid=device["uid"],
            serial=device["serial"],
            supports_stable_selector=device["supports_stable_selector"],
        )
    return write_status(
        "unknown",
        "not_checked",
        previous["selector"],
        retained,
        pid=0,
    )


def prepare_capture(selector):
    """Resolve a selector and reject missing or silently reindexed devices."""
    selector = validate_selector(selector)
    try:
        devices = enumerate_devices()
    except DeviceDiscoveryUnavailable:
        if selector.startswith(("uid:", "serial:")):
            raise CaptureUnavailable("unavailable", "device_missing") from None
        return None
    device = resolve_device(selector, devices)
    if device is None:
        raise CaptureUnavailable("unavailable", "device_missing")
    if (
        selector.startswith("serial:")
        or (
            selector.startswith("uid:")
            and not device.supports_stable_selector
        )
    ):
        raise CaptureUnavailable("unavailable", "device_missing")

    previous = _read_raw_status()
    previous_device = previous.get("device")
    if (
        selector.isdigit()
        and previous.get("selector") == selector
        and previous_device
        and (
            (
                previous_device.get("uid")
                and device.uid
                and previous_device["uid"] != device.uid
            )
            or previous_device.get("name") != device.name
        )
    ):
        raise CaptureUnavailable("unavailable", "device_reindexed", device)
    return device


def status_text(status):
    state = status["state"]
    device = status.get("device")
    name = device.get("name") if isinstance(device, dict) else None
    summaries = {
        "unknown": "microphone permission and capture have not been confirmed",
        "denied": "microphone access was denied",
        "unavailable": "the selected microphone is unavailable or was reindexed",
        "busy": "the selected microphone is busy in another application",
        "ready": f"capture is ready{f' on {name}' if name else ''}",
        "failed": "microphone capture failed unexpectedly",
    }
    return summaries[state]


def recovery_text(status):
    state = status["state"]
    if state == "denied":
        return f"Review {SYSTEM_SETTINGS_PANE}, then restart the listener."
    if state == "unavailable":
        return (
            "Run `parley mic devices`, then restart with "
            "`parley listen on --device SELECTOR`."
        )
    if state == "busy":
        return (
            "Release the microphone in the other application, then restart "
            "the listener."
        )
    if state == "failed":
        return "Restart the listener; if it fails again, run `parley mic status`."
    if state == "unknown":
        return (
            "Start the listener to verify capture; respond to any macOS prompt "
            "yourself."
        )
    return "No recovery action is needed."
