"""Microphone diagnostics use metadata and mocked capture outcomes only."""

import json
from types import SimpleNamespace

import pytest

from parley import config, microphone


@pytest.fixture(autouse=True)
def isolated_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path / "state")


def inventory(*devices):
    lines = [
        "[AVFoundation indev @ 0x123] AVFoundation video devices:",
        "[AVFoundation indev @ 0x123] [0] FaceTime Camera  [uid:camera]",
        "[AVFoundation indev @ 0x123] AVFoundation audio devices:",
    ]
    lines.extend(
        f"[AVFoundation indev @ 0x123] [{index}] {name}  [uid:{uid}]"
        for index, name, uid in devices
    )
    return "\n".join(lines)


def test_device_inventory_is_metadata_only_and_allow_listed(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=1,
            stderr=inventory(
                (0, "Built-in Microphone", "builtin-uid"),
                (1, "USB Mic", "usb-uid"),
            ),
        )

    monkeypatch.setattr(microphone.subprocess, "run", run)

    devices = microphone.enumerate_devices("/mock/ffmpeg")

    assert [item.selector for item in devices] == [
        "uid:builtin-uid", "uid:usb-uid",
    ]
    command, kwargs = calls[0]
    assert command == [
        "/mock/ffmpeg", "-hide_banner", "-f", "avfoundation",
        "-list_devices", "true", "-i", "",
    ]
    assert kwargs["stdout"] is microphone.subprocess.DEVNULL
    assert "-t" not in command


def test_inventory_ignores_video_backend_noise_and_control_characters():
    output = inventory((0, "Desk\x00 Mic", "uid-0")) + "\nuntrusted private text"

    devices = microphone.parse_devices(output)

    assert [item.name for item in devices] == ["Desk Mic"]
    assert "private" not in json.dumps([item.public() for item in devices])


@pytest.mark.parametrize(
    ("backend_error", "expected"),
    [
        (b"AVFoundation audio device access is denied", "denied"),
        (b"The device is in use by another application", "busy"),
        (b"Invalid audio device index", "unavailable"),
        (b"Audio device not found", "unavailable"),
        (b"Audio capture device with unique ID 'gone' not found", "unavailable"),
        (b"Failed to create AV capture input device: private backend detail", "failed"),
        (b"permissionish words that are not allow-listed", "failed"),
    ],
)
def test_capture_errors_map_only_to_allow_listed_states(backend_error, expected):
    state, _ = microphone.classify_capture_error(backend_error)

    assert state == expected
    assert "private backend detail" not in microphone.status_text({
        "state": state,
        "device": None,
    })


def test_ready_requires_fresh_capture_evidence_from_current_listener(monkeypatch):
    device = microphone.MicrophoneDevice(0, "Built-in", "built-in-uid")
    monkeypatch.setattr(microphone.time, "time", lambda: 100.0)
    microphone.write_status(
        "ready", "capture_active", "0", device=device, pid=4242,
    )

    assert microphone.read_status(active_pid=4242, now=102.0)["state"] == "ready"
    assert microphone.read_status(active_pid=9999, now=102.0)["state"] == "failed"
    assert microphone.read_status(active_pid=4242, now=104.0)["reason"] == (
        "capture_lost"
    )


def test_live_pid_without_capture_evidence_remains_unknown():
    assert microphone.read_status(active_pid=4242)["state"] == "unknown"


def test_numeric_device_reindex_is_rejected(monkeypatch):
    old = microphone.MicrophoneDevice(0, "Old USB Mic", "old-uid")
    new = microphone.MicrophoneDevice(0, "New USB Mic", "new-uid")
    microphone.write_status(
        "ready", "capture_active", "0", device=old, pid=1,
    )
    monkeypatch.setattr(microphone, "enumerate_devices", lambda: [new])

    with pytest.raises(microphone.CaptureUnavailable) as caught:
        microphone.prepare_capture("0")

    assert caught.value.state == "unavailable"
    assert caught.value.reason == "device_reindexed"
    assert caught.value.device == new


def test_stopping_retains_last_device_identity_for_future_drift_detection(
    monkeypatch,
):
    device = microphone.MicrophoneDevice(0, "Old USB Mic", "old-uid")
    monkeypatch.setattr(microphone.time, "time", lambda: 100.0)
    microphone.write_status(
        "ready", "capture_active", "0", device=device, pid=123,
    )

    microphone.mark_stopped()

    status = microphone.read_status(active_pid=None, now=101.0)
    assert status["state"] == "unknown"
    assert status["device"]["uid"] == "old-uid"
    assert status["pid"] == 0


def test_stable_uid_survives_index_change(monkeypatch):
    moved = microphone.MicrophoneDevice(4, "USB Mic", "stable-uid")
    monkeypatch.setattr(microphone, "enumerate_devices", lambda: [moved])

    assert microphone.prepare_capture("uid:stable-uid") == moved
    assert microphone.capture_command("uid:stable-uid", "/mock/ffmpeg")[:8] == [
        "/mock/ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-audio_device_id", "uid:stable-uid",
    ]


def test_status_document_never_persists_backend_text():
    private = "private backend failure text"
    state, reason = microphone.classify_capture_error(private)
    microphone.write_status(state, reason, "0", pid=7)

    serialized = (config.STATE / "microphone-status.json").read_text()

    assert private not in serialized
    assert json.loads(serialized)["reason"] == "capture_failed"
