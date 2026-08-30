"""Command-line behavior tests."""

import json

from parley import cli, config, listen, triggers


def test_status_consumes_one_runtime_snapshot(monkeypatch, capsys):
    snapshots = []
    value = {
        "health": "degraded",
        "generation": 17,
        "listener": {"state": "degraded"},
        "queue": {"depth": 2},
        "speech": {"synthesis": "degraded", "playback": "idle"},
        "target": {"id": "%42", "available": False},
        "provider": {
            "configured": "auto",
            "active": "macos",
            "fallback": True,
            "fallback_from": "elevenlabs",
        },
        "errors": [{
            "code": "synthesis_failed",
            "component": "speech",
            "stage": "synthesize",
            "at": 1,
        }],
    }
    monkeypatch.setattr(
        cli.runtime, "snapshot", lambda: snapshots.append(True) or value)
    monkeypatch.setattr(cli.hooks, "session_keys", lambda: ["test"])
    monkeypatch.setattr(cli.hooks, "is_on", lambda keys: False)

    cli.main(["status"])

    output = capsys.readouterr().out
    assert snapshots == [True]
    assert "runtime: degraded generation=17 provider=auto -> macos" in output
    assert "speech synthesis=degraded playback=idle queue=2" in output
    assert "target %42 (unavailable)" in output
    assert "fallback elevenlabs -> macos" in output
    assert "recent error synthesis_failed (speech/synthesize)" in output


def test_status_replaces_corrupt_private_fields_instead_of_printing_them(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(cli.hooks, "session_keys", lambda: ["test"])
    monkeypatch.setattr(cli.hooks, "is_on", lambda keys: False)
    snapshot = cli.runtime.snapshot()
    snapshot["errors"] = [{"detail": "private dictated text sk-secret"}]
    config.private_write(
        tmp_path / "runtime-status.json", json.dumps(snapshot))

    cli.main(["status"])

    output = capsys.readouterr().out
    assert "private dictated text" not in output
    assert "sk-secret" not in output
    assert "recent error snapshot_invalid (runtime/status)" in output


def test_listen_status_names_target_session_and_pane(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.runtime,
        "snapshot",
        lambda: {
            "listener": {"state": "ready"},
            "writers": {"listener": {"pid": 123}},
            "target": {"id": "%531", "available": True},
        },
    )
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
