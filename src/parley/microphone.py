"""Privacy-safe microphone inventory and capture outcome reporting.

Diagnostics in this module never open an audio stream. Device discovery asks
AVFoundation for metadata through ffmpeg's ``-list_devices`` mode; capture
readiness is written only by the normal listener after bytes arrive from its
already-requested stream.

Backend stderr is untrusted. It is used only for exact allow-listed
classification and is never returned, persisted, printed, or logged.
"""

import json
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

    @property
    def selector(self):
        if self.uid:
            return f"uid:{self.uid}"
        if self.serial:
            return f"serial:{self.serial}"
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
    return str(getattr(config, "MIC_DEVICE", os.environ.get("PARLEY_MIC", "0")))


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


def parse_devices(output):
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
            ))
    if not in_audio:
        raise DeviceDiscoveryUnavailable("audio inventory was not available")
    return devices


def enumerate_devices(binary=None):
    """Enumerate metadata without opening, recording, or retaining audio."""
    command = [
        binary or ffmpeg_binary(),
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
    return parse_devices(result.stderr)


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
    if selector.startswith(("uid:", "serial:")):
        command.extend(["-f", "avfoundation", "-audio_device_id", selector,
                        "-i", ":none"])
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
    if document["state"] not in STATES or document["reason"] not in REASONS:
        return False
    if not isinstance(document["selector"], str) or len(document["selector"]) > 240:
        return False
    if not isinstance(document["pid"], int) or document["pid"] < 0:
        return False
    if not isinstance(document["updated_at"], (int, float)):
        return False
    device = document["device"]
    if device is None:
        return True
    return bool(
        isinstance(device, dict)
        and set(device) == {"index", "name", "uid", "serial", "selector"}
        and isinstance(device["index"], int)
        and isinstance(device["name"], str)
        and isinstance(device["selector"], str)
        and (device["uid"] is None or isinstance(device["uid"], str))
        and (device["serial"] is None or isinstance(device["serial"], str))
    )


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
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return document


def read_status(active_pid=None, now=None):
    """Return current capture evidence, never permission inferred from a PID."""
    document = _read_raw_status()
    if document["state"] != "ready":
        return document
    current_time = time.time() if now is None else now
    fresh = current_time - document["updated_at"] <= READY_STALE_SECONDS
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


def prepare_capture(selector):
    """Resolve a selector and reject missing or silently reindexed devices."""
    selector = validate_selector(selector)
    try:
        devices = enumerate_devices()
    except DeviceDiscoveryUnavailable:
        return None
    device = resolve_device(selector, devices)
    if device is None:
        raise CaptureUnavailable("unavailable", "device_missing")

    previous = _read_raw_status()
    previous_device = previous.get("device")
    if (
        selector.isdigit()
        and previous.get("selector") == selector
        and previous_device
        and previous_device.get("uid")
        and device.uid
        and previous_device["uid"] != device.uid
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
