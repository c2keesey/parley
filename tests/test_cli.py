"""Command-line behavior tests."""

import json

from parley import cli, config, hooks, listen, player, triggers


def test_off_cancels_only_its_opaque_target_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setenv("TMUX_PANE", "%target-a")
    keys = hooks.session_keys()
    hooks.turn_on(keys)
    owner = hooks.session_owner(keys)
    canceled = []
    monkeypatch.setattr(cli, "cancel", canceled.append)
    monkeypatch.setattr(
        cli,
        "stop",
        lambda: (_ for _ in ()).throw(AssertionError("Off must not stop globally")),
    )
    monkeypatch.setattr(cli, "_report", lambda keys: None)
    monkeypatch.setattr("parley.indicator.refresh", lambda: None)

    cli.main(["off"])

    assert canceled == [owner]
    assert not hooks.is_on(keys)


def test_manual_say_is_unowned_and_global(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(config, "CANCELLATIONS", tmp_path / "cancellations")
    monkeypatch.setattr(cli, "detach", lambda fn: None)
    monkeypatch.setattr("parley.indicator.refresh", lambda: None)

    cli.main(["say", "manual", "status"])

    job = json.loads(player._pending()[0].read_text())
    assert job["text"] == "manual status"
    assert "owner" not in job


def test_stop_is_explicitly_global_in_help_and_output(monkeypatch, capsys):
    stopped = []
    monkeypatch.setattr(cli, "stop", lambda: stopped.append(True))

    assert "GLOBAL Stop Speech" in cli.build_parser().format_help()
    cli.main(["stop"])

    assert stopped == [True]
    assert "Stop Speech (global)" in capsys.readouterr().out


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
