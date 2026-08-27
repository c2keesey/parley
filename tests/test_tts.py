import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from parley import config, tts


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body


@pytest.fixture(autouse=True)
def provider_defaults(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "VOICE", "fable")
    monkeypatch.setattr(config, "MODEL", "openai-model")
    monkeypatch.setattr(config, "ELEVENLABS_VOICE", "voice-123")
    monkeypatch.setattr(config, "ELEVENLABS_MODEL", "eleven-model")


def test_auto_selects_elevenlabs_when_its_key_is_present(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "auto")
    monkeypatch.setattr(config, "elevenlabs_api_key", lambda: "el-key")
    assert config.provider() == "elevenlabs"
    assert config.active_voice() == "voice-123"
    assert config.active_model() == "eleven-model"


def test_auto_keeps_openai_without_an_elevenlabs_key(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "auto")
    monkeypatch.setattr(config, "elevenlabs_api_key", lambda: None)
    assert config.provider() == "openai"
    assert config.active_voice() == "fable"


def test_shared_env_file_can_hold_both_provider_keys(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.write_text("OPENAI_API_KEY=openai-key\nELEVENLABS_API_KEY=eleven-key\n")
    monkeypatch.setattr(config, "KEY_FILES", [str(env)])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert config.api_key() == "openai-key"
    assert config.elevenlabs_api_key() == "eleven-key"


def test_elevenlabs_key_falls_back_to_macos_keychain(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(config, "KEY_FILES", [])
    seen = []

    def keychain(service, account):
        seen.append((service, account))
        return "keychain-key"

    monkeypatch.setattr(config, "keychain_secret", keychain)
    assert config.elevenlabs_api_key() == "keychain-key"
    assert seen == [(config.ELEVENLABS_KEYCHAIN_SERVICE,
                     config.ELEVENLABS_KEYCHAIN_ACCOUNT)]


def test_environment_key_wins_without_touching_keychain(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "environment-key")
    monkeypatch.setattr(
        config, "keychain_secret",
        lambda service, account: pytest.fail("keychain should not be read"),
    )
    assert config.elevenlabs_api_key() == "environment-key"


def test_keychain_lookup_never_places_the_secret_in_argv(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="secure-value\n")

    monkeypatch.setattr(config.subprocess, "run", run)
    config.keychain_secret.cache_clear()
    assert config.keychain_secret("test-service", "test-account") == "secure-value"
    assert "secure-value" not in seen["argv"]
    assert seen["argv"] == [
        "security", "find-generic-password", "-s", "test-service",
        "-a", "test-account", "-w",
    ]
    assert seen["kwargs"]["capture_output"] is True


def test_invalid_provider_fails_with_configuration_guidance(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "mystery")
    with pytest.raises(RuntimeError, match="auto, openai, or elevenlabs"):
        config.provider()


def test_elevenlabs_request_uses_current_api_contract(monkeypatch):
    seen = {}
    monkeypatch.setattr(config, "PROVIDER", "elevenlabs")
    monkeypatch.setattr(config, "elevenlabs_api_key", lambda: "secret-key")

    def open_request(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response(b"mp3 bytes")

    monkeypatch.setattr(tts.urllib.request, "urlopen", open_request)
    assert tts.synthesize(
        "hello", "voice with spaces", "eleven_v3", "elevenlabs"
    ) == b"mp3 bytes"

    request = seen["request"]
    assert request.full_url.endswith(
        "/voice%20with%20spaces?output_format=mp3_44100_128")
    assert request.get_header("Xi-api-key") == "secret-key"
    assert json.loads(request.data) == {
        "text": "hello", "model_id": "eleven_v3"}
    assert seen["timeout"] == 90


def test_elevenlabs_error_is_actionable(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "elevenlabs")
    monkeypatch.setattr(config, "elevenlabs_api_key", lambda: "secret-key")
    error = urllib.error.HTTPError(
        "url", 401, "Unauthorized", {}, io.BytesIO(b"bad key"))
    monkeypatch.setattr(tts.urllib.request, "urlopen",
                        lambda request, timeout: (_ for _ in ()).throw(error))
    with pytest.raises(RuntimeError, match="elevenlabs -> 401: bad key"):
        tts.synthesize("hello")


def test_elevenlabs_voices_are_listed(monkeypatch):
    monkeypatch.setattr(config, "elevenlabs_api_key", lambda: "secret-key")
    monkeypatch.setattr(
        tts.urllib.request, "urlopen",
        lambda request, timeout: Response(json.dumps({"voices": [
            {"name": "George", "voice_id": "abc"}
        ]}).encode()),
    )
    assert tts.voices() == [{"name": "George", "voice_id": "abc"}]
