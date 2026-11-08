import pytest

from parley import relay


@pytest.fixture(autouse=True)
def isolate_relay(tmp_path, monkeypatch):
    """Keep a developer machine's real relay setup out of every test."""
    monkeypatch.setattr(relay, "RELAY_FILE", tmp_path / "relay.json")
    for name in ("PARLEY_RELAY", "PARLEY_RELAY_SELF", "PARLEY_REMOTE_BIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(relay, "_focus", {})
    monkeypatch.setattr(relay, "_agents", {})
