import threading

import pytest

from parley import config, player


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(config, "LOCK", tmp_path / "player.lock")
    monkeypatch.setattr(config, "PIDFILE", tmp_path / "playing.pid")
    monkeypatch.setattr(config, "DRAIN_PID", tmp_path / "drainer.pid")
    monkeypatch.setattr(config, "LOG", tmp_path / "speak.log")
    monkeypatch.setattr(config, "INTERRUPT", tmp_path / "interrupt")


@pytest.fixture
def recorder(monkeypatch):
    """Capture play order and assert no two players ever run at once."""
    played, live = [], []

    def fake_play(audio, interrupt=None):
        live.append(1)
        assert len(live) == 1, "two players ran at the same time"
        played.append(audio.decode())
        live.pop()

    monkeypatch.setattr(player, "synthesize", lambda text, v, m, p: text.encode())
    monkeypatch.setattr(player, "play", fake_play)
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    return played


def test_plays_in_arrival_order(recorder):
    for word in ("one", "two", "three"):
        player.enqueue(word)
    player.drain()
    assert recorder == ["one", "two", "three"]


def test_concurrent_drainers_never_overlap(recorder):
    for word in ("one", "two", "three", "four", "five"):
        player.enqueue(word)
    threads = [threading.Thread(target=player.drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert recorder == ["one", "two", "three", "four", "five"]


def test_only_one_drainer_wins_the_lock(monkeypatch):
    monkeypatch.setattr(player, "synthesize", lambda text, v, m, p: b"")
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    holder_got_lock = []

    def slow_play(audio, interrupt=None):
        # While this drainer is busy, a second must decline rather than queue up.
        holder_got_lock.append(player.drain())

    monkeypatch.setattr(player, "play", slow_play)
    player.enqueue("only item")
    player.drain()
    assert holder_got_lock == [False]


def test_empty_text_is_not_queued():
    assert player.enqueue("   ") is False
    assert player.enqueue("real") is True


def test_queue_uses_the_active_provider_voice_and_model(monkeypatch):
    monkeypatch.setattr(config, "provider", lambda: "elevenlabs")
    monkeypatch.setattr(config, "active_voice", lambda: "eleven-voice")
    monkeypatch.setattr(config, "active_model", lambda: "eleven-model")
    player.enqueue("hello")
    import json

    job = json.loads(player._pending()[0].read_text())
    assert job["provider"] == "elevenlabs"
    assert job["voice"] == "eleven-voice"
    assert job["model"] == "eleven-model"


def test_stop_clears_the_queue():
    for word in ("one", "two"):
        player.enqueue(word)
    player.stop()
    assert player._pending() == []


def test_interrupting_playback_suppresses_the_done_chime(monkeypatch):
    from parley import cues

    chimed = []
    monkeypatch.setattr(player, "synthesize", lambda text, v, m, p: b"audio")
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    monkeypatch.setattr(player, "play", lambda audio, interrupt=None: player.stop())
    monkeypatch.setattr(cues, "play", lambda name: chimed.append(name))
    player.enqueue("please stop me")

    player.drain()

    assert chimed == []
    assert config.INTERRUPT.exists()


def test_stop_terminates_the_active_audio_process(monkeypatch):
    killed = []
    config.PIDFILE.write_text("4242\n")
    monkeypatch.setattr(player.os, "kill", lambda pid, signal: killed.append(
        (pid, signal)))

    player.stop()

    assert killed == [(4242, 15)]
    assert config.PIDFILE.read_text() == ""


def test_drainer_is_active_during_synthesis_and_clears_marker(monkeypatch):
    active_during_synthesis = []

    def synthesize(text, voice, model, provider):
        active_during_synthesis.append(player.active())
        return b"audio"

    monkeypatch.setattr(player, "synthesize", synthesize)
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    monkeypatch.setattr(player, "play", lambda audio, interrupt=None: True)
    player.enqueue("hello")

    player.drain()

    assert active_during_synthesis == [True]
    assert not config.DRAIN_PID.exists()
    assert not player.active()


def test_interrupt_during_synthesis_prevents_audio_from_starting(monkeypatch):
    from parley import cues

    def synthesize(text, voice, model, provider):
        player.stop()
        return b"audio that must not play"

    monkeypatch.setattr(player, "synthesize", synthesize)
    monkeypatch.setattr(
        player, "_wake_output",
        lambda: pytest.fail("interrupted speech must not warm output"),
    )
    monkeypatch.setattr(
        player, "play", lambda audio, interrupt=None: pytest.fail(
            "interrupted speech must not play"),
    )
    monkeypatch.setattr(
        cues, "play", lambda name: pytest.fail("interruption must not chime"),
    )
    player.enqueue("interrupt me while synthesizing")

    player.drain()

    assert not config.DRAIN_PID.exists()


def test_interrupt_during_output_warmup_prevents_audio_from_starting(monkeypatch):
    from parley import cues

    monkeypatch.setattr(
        player, "synthesize", lambda text, voice, model, provider: b"audio")
    monkeypatch.setattr(player, "_wake_output", lambda: player.stop())
    monkeypatch.setattr(
        player, "play", lambda audio, interrupt=None: pytest.fail(
            "speech interrupted during warm-up must not play"),
    )
    monkeypatch.setattr(
        cues, "play", lambda name: pytest.fail("interruption must not chime"),
    )
    player.enqueue("interrupt me during warm-up")

    player.drain()

    assert not config.DRAIN_PID.exists()


def test_pending_queue_counts_as_active_before_drainer_starts():
    player.enqueue("not draining yet")
    assert player.active()


def test_interrupt_in_audio_launch_window_terminates_new_process(monkeypatch):
    terminated = []

    class Process:
        pid = 4242

        def __init__(self):
            player.stop()

        def terminate(self):
            terminated.append(self.pid)

        def wait(self):
            return 0

    monkeypatch.setattr(
        player.subprocess, "Popen", lambda *args, **kwargs: Process())
    interrupt = player._interrupt_token()

    played = player.play(b"audio", interrupt)

    assert played is False
    assert terminated == [4242]
