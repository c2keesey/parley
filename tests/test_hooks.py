import json

import pytest

from claude_speak import config, hooks


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(config, "DEFAULT", tmp_path / "default")
    monkeypatch.setattr(hooks, "SETTINGS", tmp_path / "settings.json")


def test_install_is_idempotent():
    hooks.SETTINGS.write_text(json.dumps({"hooks": {}}))
    assert hooks.install() == ["SessionStart", "Stop"]
    assert hooks.install() == []


def test_install_leaves_existing_hooks_alone():
    hooks.SETTINGS.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "some-other-tool"}]}]}}))
    hooks.install()
    data = json.loads(hooks.SETTINGS.read_text())
    commands = [h["command"] for h in data["hooks"]["Stop"][0]["hooks"]]
    assert "some-other-tool" in commands
    assert hooks.COMMAND in commands


def test_uninstall_removes_only_our_hooks():
    hooks.SETTINGS.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "some-other-tool"}]}]}}))
    hooks.install()
    hooks.uninstall()
    data = json.loads(hooks.SETTINGS.read_text())
    commands = [h["command"] for h in data["hooks"]["Stop"][0]["hooks"]]
    assert commands == ["some-other-tool"]


def test_session_start_stays_silent_when_voice_is_off(capsys):
    hooks.handle(_payload({"session_id": "s1", "hook_event_name": "SessionStart"}))
    assert capsys.readouterr().out == ""


def test_session_start_injects_context_when_on(capsys):
    hooks.turn_on("s1")
    hooks.handle(_payload({"session_id": "s1", "hook_event_name": "SessionStart"}))
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["additionalContext"] == config.PROMPT


def test_default_on_enables_new_sessions(capsys):
    config.DEFAULT.parent.mkdir(parents=True, exist_ok=True)
    config.DEFAULT.touch()
    hooks.handle(_payload({"session_id": "brand-new", "hook_event_name": "SessionStart"}))
    assert hooks.is_on("brand-new")
    assert "additionalContext" in capsys.readouterr().out


def _payload(data):
    import io

    return io.StringIO(json.dumps(data))
