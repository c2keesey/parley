"""Command-line behavior tests."""

import pytest

from parley import cli, listen, triggers


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


def test_enroll_retargets_running_listener_to_invoking_pane(monkeypatch):
    """A successful trigger must not submit into a stale prior session."""
    _stub_enrollment(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")

    stopped = []
    launches = []
    monkeypatch.setattr(listen, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(
        listen,
        "start",
        lambda device, pane, executable: launches.append(
            (device, pane, executable)),
    )

    cli.main(["enroll"])

    assert stopped == [True]
    assert len(launches) == 1
    assert launches[0][:2] == ("0", "%392")


def test_enroll_preserves_target_when_run_outside_tmux(monkeypatch):
    _stub_enrollment(monkeypatch)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")
    monkeypatch.setattr(listen, "stop", lambda: None)

    starts = []
    monkeypatch.setattr(
        listen,
        "start",
        lambda device, pane, executable: starts.append((device, pane)),
    )

    cli.main(["enroll"])

    assert starts == [("0", "%511")]


def test_enroll_does_not_start_listener_that_was_off(monkeypatch):
    _stub_enrollment(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(listen, "is_running", lambda: None)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")
    monkeypatch.setattr(
        listen,
        "start",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not start listener")
        ),
    )

    cli.main(["enroll"])


def test_listen_on_prints_success_only_after_verified_start(monkeypatch, capsys):
    monkeypatch.setenv("TMUX_PANE", "%392")
    starts = []
    monkeypatch.setattr(
        listen,
        "start",
        lambda device, pane, executable: starts.append((device, pane)),
    )

    cli.main(["listen", "on", "--device", "7"])

    captured = capsys.readouterr()
    assert starts == [("7", "%392")]
    assert "listening: on" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("failure", [
    "ffmpeg not found. Install it with: brew install ffmpeg",
    (
        "Microphone access was denied. Allow microphone access for your terminal "
        "in System Settings > Privacy & Security > Microphone, then retry."
    ),
    "Microphone device '99' is unavailable. Set PARLEY_MIC to a valid input.",
    "Listener exited before microphone capture was ready (exit code 23).",
    (
        "Listener PID 77 did not release the microphone within 4 seconds; "
        "replacement was not started."
    ),
])
def test_listen_on_failure_is_nonzero_and_never_claims_on(
        failure, monkeypatch, capsys):
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(
        listen,
        "start",
        lambda *args: (_ for _ in ()).throw(
            listen.ListenerStartupError(failure)),
    )

    with pytest.raises(SystemExit) as exited:
        cli.main(["listen", "on", "--device", "99"])

    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert "listening: on" not in captured.out
    assert failure in captured.err


def test_internal_listener_run_receives_handshake_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(
        listen,
        "run",
        lambda device, token, fd: calls.append((device, token, fd)),
    )

    cli.main([
        "listen", "run", "--device", "7",
        "--owner-token", "nonce", "--ready-fd", "42",
    ])

    assert calls == [("7", "nonce", 42)]
