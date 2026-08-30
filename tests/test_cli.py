"""Command-line behavior tests."""

import json

import pytest

from parley import cli, config, listen, triggers


def _write_speech_error(tmp_path, monkeypatch, stage="synthesis"):
    monkeypatch.setattr(config, "SPEECH_ERROR", tmp_path / "speech-error.json")
    config.private_write(config.SPEECH_ERROR, json.dumps({
        "provider": "openai",
        "stage": stage,
        "policy": "drop-after-one-attempt",
        "retry": "manual",
    }))


def test_listen_status_names_target_session_and_pane(monkeypatch, capsys):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%531")
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen.triggers, "enrolled", lambda: True)
    monkeypatch.setattr(
        listen.indicator, "session_label", lambda pane: "ivory-lynx")

    cli.main(["listen", "status"])

    assert "sends to ivory-lynx (pane %531)" in capsys.readouterr().out


def test_status_reports_sanitized_speech_failure(tmp_path, monkeypatch, capsys):
    _write_speech_error(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.hooks, "session_keys", lambda: ["test"])
    monkeypatch.setattr(cli.hooks, "is_on", lambda keys: False)
    monkeypatch.setattr(config, "provider", lambda: "openai")
    monkeypatch.setattr(config, "active_voice", lambda: "test-voice")
    monkeypatch.setattr(config, "active_model", lambda: "test-model")

    cli.main(["status"])

    error = capsys.readouterr().err
    assert "provider=openai, stage=synthesis" in error
    assert "dropped after one attempt" in error
    assert "To retry" in error


def test_say_wait_exits_nonzero_when_every_block_failed(
        tmp_path, monkeypatch, capsys):
    _write_speech_error(tmp_path, monkeypatch, stage="playback")
    monkeypatch.setattr(cli, "enqueue", lambda *args, **kwargs: True)
    monkeypatch.setattr(cli, "drain", lambda: False)

    with pytest.raises(SystemExit) as raised:
        cli.main(["say", "synthetic block", "--wait"])

    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert "provider=openai, stage=playback" in error
    assert "check afplay" in error


def _stub_enrollment(monkeypatch):
    monkeypatch.setattr(triggers, "collect", lambda device: "samples")
    monkeypatch.setattr(
        triggers,
        "save",
        lambda samples: {"thresholds": {"wake": 0.42}},
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)


def test_enroll_retargets_running_listener_to_invoking_pane(monkeypatch):
    """A successful trigger must not submit into a stale prior session."""
    _stub_enrollment(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")

    stopped = []
    targets = []
    launches = []
    monkeypatch.setattr(listen, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(listen, "set_target", targets.append)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )

    cli.main(["enroll"])

    assert stopped == [True]
    assert targets == ["%392"]
    assert len(launches) == 1


def test_enroll_preserves_target_when_run_outside_tmux(monkeypatch):
    _stub_enrollment(monkeypatch)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")
    monkeypatch.setattr(listen, "stop", lambda: None)

    targets = []
    monkeypatch.setattr(listen, "set_target", targets.append)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: None)

    cli.main(["enroll"])

    assert targets == ["%511"]


def test_enroll_does_not_start_listener_that_was_off(monkeypatch):
    _stub_enrollment(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(listen, "is_running", lambda: None)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")
    monkeypatch.setattr(
        listen,
        "set_target",
        lambda pane: (_ for _ in ()).throw(AssertionError("must not retarget")),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not start listener")
        ),
    )

    cli.main(["enroll"])
