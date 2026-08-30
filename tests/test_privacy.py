import os
import stat
from contextlib import contextmanager

from parley import cli, config, cues, hooks, listen, player


@contextmanager
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def configure_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    paths = {
        "STATE": state,
        "SESSIONS": state / "sessions",
        "SPOKEN": state / "spoken",
        "QUEUE": state / "queue",
        "DEFAULT": state / "default",
        "LOCK": state / "player.lock",
        "PIDFILE": state / "playing.pid",
        "SPEECH_PID": state / "speech.pid",
        "DRAIN_PID": state / "drainer.pid",
        "MIC_TURN": state / "microphone-turn.pid",
        "PAUSE": state / "pause",
        "INTERRUPT": state / "interrupt",
        "SKIP": state / "skip",
        "LISTENER_STATE": state / "listener.state",
        "LOG": state / "speak.log",
    }
    for name, path in paths.items():
        monkeypatch.setattr(config, name, path)
    monkeypatch.setattr(listen, "TARGET", state / "target")
    monkeypatch.setattr(listen, "LISTEN_PID", state / "listener.pid")
    return paths


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_listener_logs_metadata_without_recognized_or_dictated_text(
        tmp_path, monkeypatch):
    configure_state(tmp_path, monkeypatch)
    logs = []
    private_content = "synthetic-private-canary"
    heard = iter([
        f"{listen.WAKE} {private_content}",
        listen.SEND,
    ])
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: None)
    monkeypatch.setattr(listen.indicator, "refresh", lambda: None)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"send"], False),
    ]))
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(
        listen,
        "transcribe_cloud",
        lambda frames: f"{listen.WAKE} {private_content} {listen.SEND}",
    )
    monkeypatch.setattr(listen, "inject", lambda text: True)
    monkeypatch.setattr(listen, "cue", lambda kind: None)
    monkeypatch.setattr(listen.player, "pause", lambda: False)
    monkeypatch.setattr(listen.player, "resume", lambda: False)
    monkeypatch.setattr(listen.player, "microphone_active", lambda: False)
    monkeypatch.setattr(listen.triggers, "enrolled", lambda: False)
    monkeypatch.setattr(listen.config, "log", logs.append)

    listen.run()

    assert all(private_content not in event for event in logs)
    assert any(event.startswith("local transcription chars=") for event in logs)
    assert any(event.startswith("message transcribed chars=") for event in logs)


def test_listener_state_is_private_under_permissive_umask(tmp_path, monkeypatch):
    paths = configure_state(tmp_path, monkeypatch)
    monkeypatch.setattr(listen.indicator, "refresh", lambda: None)

    with permissive_umask():
        listen.set_target("%0")
        listen.set_listener_state("ready")

    assert mode(paths["STATE"]) == 0o700
    assert mode(listen.TARGET) == 0o600
    assert mode(paths["LISTENER_STATE"]) == 0o600


def test_hook_state_is_private_under_permissive_umask(tmp_path, monkeypatch):
    paths = configure_state(tmp_path, monkeypatch)
    monkeypatch.setattr(hooks, "reply_from", lambda payload: ("reply-id", "payload"))
    monkeypatch.setattr(hooks, "enqueue", lambda text: True)
    monkeypatch.setattr(hooks, "drain", lambda: True)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)

    with permissive_umask():
        hooks.turn_on(["synthetic-session"])
        hooks.speak_reply("synthetic-session", {})

    assert mode(paths["STATE"]) == 0o700
    assert mode(paths["SESSIONS"]) == 0o700
    assert mode(paths["SESSIONS"] / "synthetic-session") == 0o600
    assert mode(paths["SPOKEN"]) == 0o700
    assert mode(paths["SPOKEN"] / "synthetic-session") == 0o600


def test_player_state_is_private_under_permissive_umask(tmp_path, monkeypatch):
    paths = configure_state(tmp_path, monkeypatch)
    monkeypatch.setattr(player, "_signal_speech", lambda signal: False)

    with permissive_umask():
        player.pause()

    assert mode(paths["STATE"]) == 0o700
    assert mode(paths["MIC_TURN"]) == 0o600
    assert mode(paths["PAUSE"]) == 0o600

    with permissive_umask():
        player.stop()

    assert mode(paths["QUEUE"]) == 0o700
    assert mode(paths["INTERRUPT"]) == 0o600
    assert mode(paths["PIDFILE"]) == 0o600


def test_default_on_is_private_under_permissive_umask(tmp_path, monkeypatch):
    paths = configure_state(tmp_path, monkeypatch)

    with permissive_umask():
        cli.main(["default", "on"])

    assert mode(paths["STATE"]) == 0o700
    assert mode(paths["DEFAULT"]) == 0o600


def test_generated_cue_cache_is_private_under_permissive_umask(
        tmp_path, monkeypatch):
    paths = configure_state(tmp_path, monkeypatch)

    with permissive_umask():
        cue_path = cues.build("cancel")

    assert mode(paths["STATE"]) == 0o700
    assert mode(cue_path) == 0o600


def test_listener_cue_pidfile_is_private_without_playing_audio(
        tmp_path, monkeypatch):
    paths = configure_state(tmp_path, monkeypatch)

    class Process:
        pid = 4242

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(cues, "bundled", lambda name: "/synthetic/bundled.wav")
    monkeypatch.setattr(cues.subprocess, "Popen", lambda *args, **kwargs: Process())

    with permissive_umask():
        cues.play("wake")

    assert mode(paths["STATE"]) == 0o700
    assert mode(paths["PIDFILE"]) == 0o600
