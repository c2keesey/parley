import threading

import pytest

from claude_speak import config, player


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(config, "LOCK", tmp_path / "player.lock")
    monkeypatch.setattr(config, "PIDFILE", tmp_path / "playing.pid")
    monkeypatch.setattr(config, "LOG", tmp_path / "speak.log")


@pytest.fixture
def recorder(monkeypatch):
    """Capture play order and assert no two players ever run at once."""
    played, live = [], []

    def fake_play(audio):
        live.append(1)
        assert len(live) == 1, "two players ran at the same time"
        played.append(audio.decode())
        live.pop()

    monkeypatch.setattr(player, "synthesize", lambda text, v, m: text.encode())
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
    monkeypatch.setattr(player, "synthesize", lambda text, v, m: b"")
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    holder_got_lock = []

    def slow_play(audio):
        # While this drainer is busy, a second must decline rather than queue up.
        holder_got_lock.append(player.drain())

    monkeypatch.setattr(player, "play", slow_play)
    player.enqueue("only item")
    player.drain()
    assert holder_got_lock == [False]


def test_empty_text_is_not_queued():
    assert player.enqueue("   ") is False
    assert player.enqueue("real") is True


def test_stop_clears_the_queue():
    for word in ("one", "two"):
        player.enqueue(word)
    player.stop()
    assert player._pending() == []
