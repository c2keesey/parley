import json
import stat
from types import SimpleNamespace

import pytest

from parley import config


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path / "state")
    for setting in config.SETTINGS.values():
        monkeypatch.delenv(setting.env, raising=False)
    return config.STATE


def test_log_and_state_directory_are_private(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(config, "STATE", state)
    monkeypatch.setattr(config, "LOG", state / "speak.log")

    config.log("a privacy-safe event")

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.LOG.stat().st_mode) == 0o600
    assert "a privacy-safe event" in config.LOG.read_text()


def test_missing_settings_preserve_environment_only_behavior(isolated_settings):
    assert config.resolved("speed").value == 1.2
    assert config.resolved("speed").source == "default"
    assert not config.settings_path().exists()


def test_non_secret_customization_is_private_typed_and_persistent(
        isolated_settings):
    config.set_values({
        "provider": "macos",
        "voice": "nova",
        "model": "speech-model",
        "wake": "hello parley",
        "send": "ship this",
        "cancel": "discard this",
        "microphone": "2",
        "mic-threshold": "720",
        "macos-rate": "240",
        "speed": "1.4",
        "cue-wake": "off",
    })

    assert config.resolved("provider").value == "macos"
    assert config.resolved("microphone").value == "2"
    assert config.resolved("mic-threshold").value == 720
    assert config.resolved("speed").value == 1.4
    assert config.resolved("wake").source == "persisted"
    document = json.loads(config.settings_path().read_text())
    assert document["version"] == 1
    assert document["settings"]["cancel"] == "discard this"
    assert stat.S_IMODE(isolated_settings.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.settings_path().stat().st_mode) == 0o600


def test_environment_override_wins_without_changing_persisted_value(
        isolated_settings, monkeypatch):
    config.set_values({"speed": "1.4"})
    monkeypatch.setenv("PARLEY_SPEED", "1.8")

    effective = config.resolved("speed")

    assert effective.value == 1.8
    assert effective.source == "environment (PARLEY_SPEED)"
    assert json.loads(config.settings_path().read_text())["settings"]["speed"] == 1.4


def test_invalid_multi_value_update_is_atomic(isolated_settings):
    config.set_values({"speed": "1.4", "wake": "hello parley"})
    before = config.settings_path().read_bytes()

    with pytest.raises(config.ConfigurationError, match="speed.*must be a number"):
        config.set_values({"wake": "new phrase", "speed": "racing"})

    assert config.settings_path().read_bytes() == before
    assert config.resolved("wake").value == "hello parley"


def test_credentials_are_rejected_before_any_file_is_written(isolated_settings):
    with pytest.raises(config.ConfigurationError, match="credentials cannot be stored"):
        config.set_values({"OPENAI_API_KEY": "not-a-real-key"})

    assert not config.settings_path().exists()


def test_malformed_settings_have_actionable_recovery(isolated_settings):
    config.private_write(config.settings_path(), "{broken")

    with pytest.raises(config.ConfigurationError, match="reset --all"):
        config.all_settings()

    config.reset()
    assert config.resolved("provider").value == "auto"


def test_malformed_numeric_environment_is_actionable_not_an_import_crash(
        isolated_settings, monkeypatch):
    monkeypatch.setenv("PARLEY_SPEED", "very-fast")

    with pytest.raises(
            config.ConfigurationError,
            match=r"speed.*PARLEY_SPEED.*must be a number"):
        config.validate()


def test_missing_custom_cue_is_rejected_without_replacing_settings(
        isolated_settings):
    config.set_values({"cue-wake": "off"})
    before = config.settings_path().read_bytes()

    with pytest.raises(config.ConfigurationError, match="file does not exist"):
        config.set_values({"cue-wake": "/missing/private-tone.wav"})

    assert config.settings_path().read_bytes() == before


def test_device_discovery_parses_audio_section_and_ignores_video(
        isolated_settings, monkeypatch):
    output = """
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime Camera
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x1] [1] Studio Display Microphone
"""
    monkeypatch.setattr(config.shutil, "which", lambda name: "/opt/ffmpeg")
    monkeypatch.setattr(
        config.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stderr=output, stdout=""),
    )

    result = config.discover_devices()

    assert result.warning is None
    assert result.items == (
        {"id": "0", "name": "MacBook Pro Microphone"},
        {"id": "1", "name": "Studio Display Microphone"},
    )


def test_device_discovery_degrades_safely_without_ffmpeg(
        isolated_settings, monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda name: None)

    result = config.discover_devices()

    assert result.items == ()
    assert "ffmpeg is unavailable" in result.warning


def test_elevenlabs_voice_discovery_degrades_without_credentials(
        isolated_settings, monkeypatch):
    monkeypatch.setattr(config, "elevenlabs_api_key", lambda: None)

    result = config.discover_voices("elevenlabs")

    assert result.items == ()
    assert "needs a configured credential" in result.warning


def test_macos_voice_is_validated_before_settings_are_replaced(
        isolated_settings, monkeypatch):
    config.set_values({"speed": "1.4"})
    before = config.settings_path().read_bytes()
    monkeypatch.setattr(
        config, "discover_voices",
        lambda provider: config.Discovery((
            {"id": "Samantha", "name": "Samantha"},
        )),
    )

    with pytest.raises(config.ConfigurationError, match="not available for macos"):
        config.set_values({"macos-voice": "nova"})

    assert config.settings_path().read_bytes() == before
