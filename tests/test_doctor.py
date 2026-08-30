"""Privacy and degraded-state coverage for `parley doctor`."""

import json
import os
import stat
from types import SimpleNamespace

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


def test_verified_listener_and_live_target_pass_without_claiming_microphone(
    monkeypatch,
):
    (config.STATE / "listener.pid").write_text(str(os.getpid()))
    (config.STATE / "target").write_text("%42")
    monkeypatch.setattr(doctor, "_listener_process", lambda path: "owned")
    monkeypatch.setattr(doctor, "_pane_available", lambda pane: pane == "%42")
    hooks.TARGETS["codex"].write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": "parley hook", "timeout": 10,
        }]}]},
    }))

    checks = _checks(doctor.collect())

    assert checks["listener.process"]["status"] == "pass"
    assert checks["listener.target"]["status"] == "pass"
    assert checks["input.microphone"]["status"] == "warn"
    assert "not safely verifiable" in checks["input.microphone"]["summary"]
    assert checks["hook.codex"]["status"] == "pass"
    assert checks["hook.claude-code"]["status"] == "warn"


def test_running_listener_with_stale_target_fails(monkeypatch):
    (config.STATE / "listener.pid").write_text(str(os.getpid()))
    (config.STATE / "target").write_text("%stale")
    monkeypatch.setattr(doctor, "_listener_process", lambda path: "owned")
    monkeypatch.setattr(doctor, "_pane_available", lambda pane: False)

    check = _checks(doctor.collect())["listener.target"]

    assert check["status"] == "fail"
    assert "listen off" in check["action"]
    assert "%stale" not in json.dumps(check)


def test_unrelated_live_pid_is_not_a_parley_listener(monkeypatch):
    marker = config.STATE / "listener.pid"
    marker.write_text("4242")
    monkeypatch.setattr(doctor.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{os.getuid()} sleep 300\n",
        ),
    )

    assert doctor._listener_process(marker) == "foreign"
    checks = _checks(doctor.collect())
    assert checks["listener.process"]["status"] == "fail"
    assert "verified Parley listener" in checks["listener.process"]["summary"]
    assert checks["input.microphone"]["status"] == "warn"


def test_listener_process_requires_same_user_and_structured_command(monkeypatch):
    marker = config.STATE / "listener.pid"
    marker.write_text("4242")
    monkeypatch.setattr(doctor.os, "kill", lambda pid, signal: None)

    def process(command):
        monkeypatch.setattr(
            doctor.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=command),
        )
        return doctor._listener_process(marker)

    assert process(f"{os.getuid()} /opt/parley listen run --device 0\n") == "owned"
    assert process(
        f"{os.getuid()} /usr/bin/python3 /opt/parley listen run --device 0\n"
    ) == "owned"
    assert process(f"{os.getuid()} python -m parley listen run\n") == "owned"
    assert process(f"{os.getuid() + 1} /opt/parley listen run\n") == "foreign"
    assert process(f"{os.getuid()} sleep parley listen run\n") == "foreign"


def test_tmux_target_must_resolve_to_requested_pane(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="%other\n"),
    )

    assert not doctor._pane_available("%requested")


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


def test_sensitive_state_file_mode_is_a_failure_without_reading_content(
    monkeypatch,
):
    sensitive = config.STATE / "speak.log"
    sensitive.write_text("must never be read")
    sensitive.chmod(0o666)
    original_read_text = sensitive.__class__.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == sensitive:
            pytest.fail("state content must not be read")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(
        sensitive.__class__,
        "read_text",
        guarded_read_text,
    )

    check = _checks(doctor.collect())["state.directory"]

    assert check["status"] == "fail"
    assert check["data"]["unsafe_entries"] == ["speak.log"]
    assert stat.S_IMODE(sensitive.stat().st_mode) == 0o666


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
