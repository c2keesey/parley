"""Privacy and degraded-state coverage for `parley doctor`."""

import json
import os
import stat

import pytest

from parley import config, doctor, hooks


@pytest.fixture(autouse=True)
def isolated_doctor(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(config, "STATE", state)
    monkeypatch.setattr(config, "KEY_FILES", [])
    monkeypatch.setattr(config, "PROVIDER", "macos")
    monkeypatch.setattr(config, "elevenlabs_key_configured", lambda: False)
    monkeypatch.setattr(hooks, "TARGETS", {
        "claude-code": tmp_path / "claude.json",
        "codex": tmp_path / "codex.json",
    })
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")


def _checks(report):
    return {item["id"]: item for item in report["checks"]}


def test_report_never_contains_credential_values(monkeypatch, capsys):
    secret = "sk-do-not-serialize-this-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(config, "PROVIDER", "openai")

    report = doctor.collect()
    doctor.print_report(report)
    human = capsys.readouterr().out
    doctor.print_report(report, as_json=True)
    machine = capsys.readouterr().out

    assert _checks(report)["provider.tts"]["status"] == "pass"
    assert secret not in human
    assert secret not in machine
    assert "OPENAI_API_KEY" not in machine


def test_missing_tools_are_actionable_failures_and_warnings(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)

    checks = _checks(doctor.collect())

    assert checks["output.say"]["status"] == "fail"
    assert checks["output.afplay"]["status"] == "fail"
    assert checks["input.ffmpeg"]["status"] == "warn"
    assert checks["input.whisper"]["status"] == "warn"
    assert "brew install whisper-cpp" in checks["input.whisper"]["action"]


def test_live_target_and_installed_hook_pass(monkeypatch):
    (config.STATE / "listener.pid").write_text(str(os.getpid()))
    (config.STATE / "target").write_text("%42")
    monkeypatch.setattr(doctor, "_pane_available", lambda pane: pane == "%42")
    hooks.TARGETS["codex"].write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": "parley hook", "timeout": 10,
        }]}]},
    }))

    checks = _checks(doctor.collect())

    assert checks["listener.process"]["status"] == "pass"
    assert checks["listener.target"]["status"] == "pass"
    assert checks["input.microphone"]["status"] == "pass"
    assert checks["hook.codex"]["status"] == "pass"
    assert checks["hook.claude-code"]["status"] == "warn"


def test_running_listener_with_stale_target_fails(monkeypatch):
    (config.STATE / "listener.pid").write_text(str(os.getpid()))
    (config.STATE / "target").write_text("%stale")
    monkeypatch.setattr(doctor, "_pane_available", lambda pane: False)

    check = _checks(doctor.collect())["listener.target"]

    assert check["status"] == "fail"
    assert "listen off" in check["action"]
    assert "%stale" not in json.dumps(check)


def test_hook_invalid_json_fails_without_echoing_contents():
    sensitive = "credential-like-content"
    hooks.TARGETS["claude-code"].write_text("not json " + sensitive)

    check = _checks(doctor.collect())["hook.claude-code"]

    assert check["status"] == "fail"
    assert sensitive not in json.dumps(check)


def test_state_permissions_are_reported_without_mutation():
    config.STATE.chmod(0o755)

    check = _checks(doctor.collect())["state.directory"]

    assert check["status"] == "fail"
    assert stat.S_IMODE(config.STATE.stat().st_mode) == 0o755


def test_missing_state_is_reported_without_creating_it(tmp_path, monkeypatch):
    missing = tmp_path / "not-created"
    monkeypatch.setattr(config, "STATE", missing)

    check = _checks(doctor.collect())["state.directory"]

    assert check["status"] == "warn"
    assert not missing.exists()


def test_json_schema_and_order_are_stable(capsys):
    report = doctor.collect()

    doctor.print_report(report, as_json=True)
    first = capsys.readouterr().out
    doctor.print_report(report, as_json=True)
    second = capsys.readouterr().out

    decoded = json.loads(first)
    assert first == second
    assert list(decoded) == ["checks", "overall", "schema_version"]
    assert decoded["schema_version"] == 1
    assert all({"id", "status", "summary"} <= item.keys()
               for item in decoded["checks"])
