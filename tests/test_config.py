import os
import stat
import subprocess
import sys

from parley import config


def test_log_and_state_directory_are_private(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(config, "STATE", state)
    monkeypatch.setattr(config, "LOG", state / "speak.log")

    config.log("a privacy-safe event")

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.LOG.stat().st_mode) == 0o600
    assert "a privacy-safe event" in config.LOG.read_text()


def test_invalid_configuration_is_one_line_without_traceback(tmp_path):
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "PARLEY_STATE": str(tmp_path / "state"),
        "PARLEY_TTS_PROVIDER": "macos",
        "PARLEY_SPEED": "definitely-not-a-number",
    })
    env.pop("OPENAI_API_KEY", None)
    env.pop("ELEVENLABS_API_KEY", None)

    result = subprocess.run(
        [sys.executable, "-m", "parley", "status"],
        capture_output=True, text=True, env=env, check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert result.stderr.startswith(
        "parley: configuration error: PARLEY_SPEED must be"
    )
    assert "Traceback" not in result.stderr
    assert "definitely-not-a-number" not in result.stderr
