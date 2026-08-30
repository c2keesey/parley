"""Command-line behavior tests."""

import json

import pytest

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


def _write_speech_error(tmp_path, monkeypatch, stage="synthesis"):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(
        cli.runtime.processes,
        "process_identity",
        lambda pid: f"linux:00000000-0000-0000-0000-000000000000:{pid}",
    )
    writer = cli.runtime.claim("speech")
    if stage == "synthesis":
        cli.runtime.set_speech(writer, synthesis="degraded", playback="idle")
        code, runtime_stage = "synthesis_failed", "synthesize"
    else:
        cli.runtime.set_speech(writer, synthesis="idle", playback="degraded")
        code, runtime_stage = "playback_failed", "play"
    cli.runtime.record_error(
        writer, code, "speech", runtime_stage, "openai",
    )


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
