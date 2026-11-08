import pytest

from parley import config, listen, relay


@pytest.fixture(autouse=True)
def isolate_machine_state(tmp_path, monkeypatch):
    """Keep a developer machine's real Parley and relay state out of tests."""
    state = tmp_path / "parley-state"
    monkeypatch.setattr(config, "STATE", state)
    monkeypatch.setattr(listen, "TARGET", state / "target")
    monkeypatch.setattr(listen, "LISTEN_PID", state / "listener.pid")
    monkeypatch.setattr(relay, "RELAY_FILE", tmp_path / "relay.json")
    for name in ("PARLEY_RELAY", "PARLEY_RELAY_SELF", "PARLEY_REMOTE_BIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(relay, "_focus", {})
    monkeypatch.setattr(relay, "_agents", {})
