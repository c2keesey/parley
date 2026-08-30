"""Command-line behavior tests."""

import pytest

from parley import cli, config, listen, triggers


def test_listen_status_names_target_session_and_pane(monkeypatch, capsys):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%531")
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen.triggers, "enrolled", lambda: True)
    monkeypatch.setattr(
        listen.indicator, "session_label", lambda pane: "ivory-lynx")

    cli.main(["listen", "status"])

    assert "sends to ivory-lynx (pane %531)" in capsys.readouterr().out


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


def test_config_set_get_list_and_reset_show_precedence(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "STATE", tmp_path / "state")
    monkeypatch.delenv("PARLEY_SPEED", raising=False)

    cli.main(["config", "set", "speed", "1.5"])
    assert "source: persisted" in capsys.readouterr().out

    monkeypatch.setenv("PARLEY_SPEED", "1.8")
    cli.main(["config", "get", "speed"])
    output = capsys.readouterr().out
    assert "1.8" in output
    assert "environment (PARLEY_SPEED)" in output

    cli.main(["config", "reset", "speed"])
    assert not config.settings_path().exists()


def test_config_reset_all_recovers_a_malformed_file(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "STATE", tmp_path / "state")
    config.private_write(config.settings_path(), "not-json")

    cli.main(["config", "reset", "--all"])

    assert not config.settings_path().exists()
    assert "Removed persisted settings" in capsys.readouterr().out


def test_config_reports_malformed_environment_without_traceback(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "STATE", tmp_path / "state")
    monkeypatch.setenv("PARLEY_SPEED", "warp-nine")

    with pytest.raises(SystemExit) as error:
        cli.main(["config", "list"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "configuration error" in stderr
    assert "PARLEY_SPEED" in stderr


def test_macos_voices_are_listed_without_queueing_openai_names(
        monkeypatch, capsys):
    monkeypatch.setattr(config, "validate", lambda: None)
    monkeypatch.setattr(config, "provider", lambda: "macos")
    monkeypatch.setattr(config, "active_voice", lambda: "Samantha")
    monkeypatch.setattr(
        config, "discover_voices",
        lambda provider: config.Discovery((
            {"id": "Samantha", "name": "Samantha"},
            {"id": "Eddy", "name": "Eddy"},
        )),
    )
    monkeypatch.setattr(
        cli, "enqueue",
        lambda *args, **kwargs: pytest.fail("macOS voices must not queue OpenAI names"),
    )

    cli.main(["voices"])

    output = capsys.readouterr().out
    assert "* Samantha: Samantha" in output
    assert "macos-voice" in output


def test_say_rejects_voice_incompatible_with_active_provider(
        monkeypatch, capsys):
    monkeypatch.setattr(config, "validate", lambda: None)
    monkeypatch.setattr(
        config, "validate_voice",
        lambda voice: (_ for _ in ()).throw(
            config.ConfigurationError("voice 'nova' is not available for macos")),
    )
    monkeypatch.setattr(
        cli, "enqueue",
        lambda *args, **kwargs: pytest.fail("invalid voice must not be queued"),
    )

    with pytest.raises(SystemExit) as error:
        cli.main(["say", "--voice", "nova", "hello"])

    assert error.value.code == 2
    assert "not available for macos" in capsys.readouterr().err
