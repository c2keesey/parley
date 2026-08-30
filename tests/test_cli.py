"""Command-line behavior tests."""
import json

from parley import cli, config, hooks, indicator, listen, triggers


def test_json_status_is_stable_and_does_not_discover_credentials(
    monkeypatch, capsys
):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(listen, "get_target", lambda: "%531")
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "listener_state", lambda: "capturing")
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(indicator, "session_label", lambda pane: "ivory lynx")
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)
    monkeypatch.setattr(
        config,
        "provider",
        lambda: (_ for _ in ()).throw(AssertionError("must not read credentials")),
    )

    cli.main(["status", "--json"])

    assert json.loads(capsys.readouterr().out) == {
        "contract_version": 1,
        "listener_running": True,
        "listener_state": "capturing",
        "speaking": False,
        "target": {
            "available": True,
            "label": "ivory lynx",
            "pane": "%531",
        },
        "voice_on": True,
    }


def test_json_status_prefers_listener_routing_target(monkeypatch, capsys):
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(listen, "get_target", lambda: "%531")
    monkeypatch.setattr(listen, "is_running", lambda: 0)
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(indicator, "session_label", lambda pane: "listener target")
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: False)

    cli.main(["status", "--json"])

    status = json.loads(capsys.readouterr().out)
    assert status["target"] == {
        "available": True,
        "label": "listener target",
        "pane": "%531",
    }
    assert status["listener_state"] == "off"


def test_json_status_falls_back_to_calling_pane_without_listener_target(
    monkeypatch, capsys
):
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(listen, "get_target", lambda: "")
    monkeypatch.setattr(listen, "is_running", lambda: 0)
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(indicator, "session_label", lambda pane: "current pane")
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: False)

    cli.main(["status", "--json"])

    assert json.loads(capsys.readouterr().out)["target"] == {
        "available": True,
        "label": "current pane",
        "pane": "%392",
    }


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
