import subprocess

import pytest

from parley import config, hooks, listen, relay


@pytest.fixture(autouse=True)
def isolate_machine_state(tmp_path, monkeypatch):
    """Keep a developer machine's real Parley and relay state out of tests."""
    state = tmp_path / "parley-state"
    monkeypatch.setattr(config, "STATE", state)
    monkeypatch.setattr(config, "LOG", state / "speak.log")
    monkeypatch.setattr(config, "OUTPUT_MODE", state / "output-mode")
    monkeypatch.delenv("PARLEY_OUTPUT_MODE", raising=False)
    # Installation must never reach the developer's real harness configs.
    monkeypatch.setattr(hooks, "CODEX_CONFIG", tmp_path / "codex" / "config.toml")
    monkeypatch.setattr(hooks, "_claude", lambda: None)
    monkeypatch.setattr(listen, "TARGET", state / "target")
    monkeypatch.setattr(listen, "LISTEN_PID", state / "listener.pid")
    monkeypatch.setattr(relay, "RELAY_FILE", tmp_path / "relay.json")
    for name in ("PARLEY_RELAY", "PARLEY_RELAY_SELF", "PARLEY_REMOTE_BIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(relay, "_focus", {})
    monkeypatch.setattr(relay, "_agents", {})


_AUDIO = {"afplay", "say"}
_RealPopen = subprocess.Popen


class _SilentPopen(_RealPopen):
    """Run audio players as `true`, so the suite never makes a sound."""

    def __init__(self, args, *rest, **kwargs):
        argv = [args] if isinstance(args, (str, bytes)) else list(args)
        if argv and str(argv[0]).rsplit("/", 1)[-1] in _AUDIO:
            args = ["true"]
        super().__init__(args, *rest, **kwargs)


@pytest.fixture(autouse=True)
def silence_audio(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _SilentPopen)
