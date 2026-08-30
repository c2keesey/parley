import json
import re
import stat
import threading
import time

import pytest

from parley import config, indicator, player, processes, tts


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "macos")
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(config, "LOCK", tmp_path / "player.lock")
    monkeypatch.setattr(config, "PIDFILE", tmp_path / "playing.pid")
    monkeypatch.setattr(config, "CUE_PROCESSES", tmp_path / "cue-processes")
    monkeypatch.setattr(config, "SPEECH_PID", tmp_path / "speech.pid")
    monkeypatch.setattr(config, "DRAIN_PID", tmp_path / "drainer.pid")
    monkeypatch.setattr(config, "MIC_TURN", tmp_path / "microphone-turn.pid")
    monkeypatch.setattr(config, "PAUSE", tmp_path / "pause")
    monkeypatch.setattr(config, "LOG", tmp_path / "speak.log")
    monkeypatch.setattr(config, "INTERRUPT", tmp_path / "interrupt")
    monkeypatch.setattr(config, "SKIP", tmp_path / "skip")
    monkeypatch.setattr(indicator, "refresh", lambda: None)
    monkeypatch.setattr(
        processes,
        "process_identity",
        lambda pid: f"linux:00000000-0000-0000-0000-000000000000:{pid}",
    )


def own(path, pid, kind):
    ownership = processes.claim(path, pid, kind)
    assert ownership is not None
    return ownership


@pytest.fixture
def recorder(monkeypatch):
    """Capture play order and assert no two players ever run at once."""
    played, live = [], []

    def fake_play(audio, interrupt=None, skip=None):
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

    def slow_play(audio, interrupt=None, skip=None):
        # While this drainer is busy, a second must decline rather than queue up.
        holder_got_lock.append(player.drain())

    monkeypatch.setattr(player, "play", slow_play)
    player.enqueue("only item")
    player.drain()
    assert holder_got_lock == [False]


def test_empty_text_is_not_queued():
    assert player.enqueue("   ") is False
    assert player.enqueue("real") is True


def test_long_reply_is_split_at_sentence_boundaries_without_loss(monkeypatch):
    monkeypatch.setattr(config, "MAX_CHARS", 18)
    text = "Alpha beta. Gamma delta. Epsilon zeta."

    player.enqueue(text)

    import json
    queued = [json.loads(item.read_text())["text"] for item in player._pending()]
    assert queued == ["Alpha beta.", "Gamma delta.", "Epsilon zeta."]
    assert all(len(chunk) <= config.MAX_CHARS for chunk in queued)
    assert re.sub(r"\s", "", "".join(queued)) == re.sub(r"\s", "", text)


def test_long_sentence_falls_back_to_whitespace_then_hard_split():
    assert list(player.chunks("alpha beta gamma delta", 10)) == [
        "alpha beta", "gamma", "delta",
    ]
    assert list(player.chunks("abcdefghijk", 4)) == ["abcd", "efgh", "ijk"]


def test_complete_multichunk_reply_plays_in_order_with_one_done_cue(
        recorder, monkeypatch):
    from parley import cues

    monkeypatch.setattr(config, "MAX_CHARS", 12)
    chimed = []
    monkeypatch.setattr(cues, "play", chimed.append)
    player.enqueue("One short. Two short. Three short.")

    player.drain()

    assert recorder == ["One short.", "Two short.", "Three short."]
    assert chimed == ["done"]


def test_provider_failure_is_sanitized_dropped_and_returns_failure(monkeypatch):
    from parley import cues

    private_text = "synthetic private response sentinel"
    private_detail = "synthetic provider body sentinel"
    chimed = []
    monkeypatch.setattr(
        player, "synthesize",
        lambda *args: (_ for _ in ()).throw(RuntimeError(private_detail)),
    )
    monkeypatch.setattr(
        cues, "play", lambda name, wait=True: chimed.append((name, wait)))
    player.enqueue(private_text)

    assert player.drain() is False

    assert player._pending() == []
    assert player.speech_error() == {
        "policy": "drop-after-one-attempt",
        "provider": config.provider(),
        "retry": "manual",
        "stage": "synthesis",
    }
    assert chimed == [("error", True)]
    exposed = (
        (config.STATE / "runtime-status.json").read_text()
        + config.LOG.read_text()
    )
    assert private_text not in exposed
    assert private_detail not in exposed


def test_entirely_failed_drain_attempts_each_block_once_and_indicates_once(
        monkeypatch):
    from parley import cues

    attempts = []
    chimed = []

    def fail(text, voice, model, provider):
        attempts.append(text)
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(player, "synthesize", fail)
    monkeypatch.setattr(
        cues, "play", lambda name, wait=True: chimed.append((name, wait)))
    player.enqueue("first block")
    player.enqueue("second block")

    assert player.drain() is False
    assert attempts == ["first block", "second block"]
    assert chimed == [("error", True)]
    assert player._pending() == []


def test_nonzero_afplay_is_failure_and_never_emits_done(monkeypatch):
    from parley import cues

    class FailedAfplay:
        pid = 4242

        def wait(self):
            return 7

    chimed = []
    monkeypatch.setattr(player, "synthesize", lambda *args: b"audio")
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    monkeypatch.setattr(player.subprocess, "Popen", lambda argv: FailedAfplay())
    monkeypatch.setattr(
        cues, "play", lambda name, wait=True: chimed.append((name, wait)))
    player.enqueue("synthetic playback block")

    assert player.drain() is False

    assert player.speech_error()["stage"] == "playback"
    assert chimed == [("error", True)]


def test_detached_failed_drain_has_no_error_cue_or_marker_at_return(
        monkeypatch):
    from parley import cues

    wait_calls = []

    class ErrorCueProcess:
        pid = 4242
        running = True

        def wait(self, timeout=None):
            wait_calls.append(timeout)
            assert config.PIDFILE.read_text().split() == [str(self.pid)]
            self.running = False
            return 0

    proc = ErrorCueProcess()
    monkeypatch.setattr(
        player, "synthesize",
        lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(cues.subprocess, "Popen", lambda *args, **kwargs: proc)
    player.enqueue("synthetic failure block")

    assert player.drain() is False

    # detach() calls os._exit immediately after drain() returns. These exact
    # boundary assertions ensure there is no child or marker for it to orphan.
    assert wait_calls == [5]
    assert not proc.running
    assert not config.PIDFILE.exists()


def test_invalid_macos_voice_is_a_sanitized_synthesis_failure(monkeypatch):
    private_detail = "synthetic say diagnostic sentinel"
    monkeypatch.setattr(config, "provider", lambda: "macos")
    monkeypatch.setattr(config, "active_voice", lambda: "Missing Test Voice")
    monkeypatch.setattr(config, "active_model", lambda: "say")
    monkeypatch.setattr(player, "synthesize", tts.synthesize)
    monkeypatch.setattr(
        tts.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "Result", (), {"returncode": 1, "stderr": private_detail})(),
    )
    monkeypatch.setattr("parley.cues.play", lambda *args, **kwargs: None)
    player.enqueue("synthetic invalid voice block")

    assert player.drain() is False

    error = player.speech_error()
    assert error["provider"] == "macos"
    assert error["stage"] == "synthesis"
    assert "selected macOS voice" in player.speech_error_message(error)
    assert private_detail not in config.LOG.read_text()


def test_clean_later_drain_clears_failure_and_reports_recovery(
        recorder, monkeypatch):
    from parley import cues

    chimed = []
    monkeypatch.setattr(cues, "play", lambda name, wait=True: chimed.append(name))
    monkeypatch.setattr(
        player, "synthesize",
        lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    player.enqueue("failed block")
    assert player.drain() is False
    assert player.speech_error() is not None

    monkeypatch.setattr(player, "synthesize", lambda text, *args: text.encode())
    player.enqueue("recovered block")

    assert player.drain() is True
    assert recorder == ["recovered block"]
    assert chimed == ["error", "done"]
    assert player.speech_error() is None


def test_local_aiff_audio_is_written_with_the_right_extension(monkeypatch):
    launched = []

    class Process:
        pid = 12345

        def wait(self):
            return 0

    monkeypatch.setattr(
        player.subprocess,
        "Popen",
        lambda argv: launched.append(argv) or Process(),
    )
    monkeypatch.setattr(processes.os, "kill", lambda pid, signal: None)

    player.play(b"FORM local aiff")

    assert launched[0][1].endswith(".aiff")


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


def test_queued_reply_is_private_on_disk():
    player.enqueue("private assistant reply")

    queued = player._pending()[0]
    assert stat.S_IMODE(queued.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.QUEUE.stat().st_mode) == 0o700


def test_stop_clears_the_queue(monkeypatch):
    monkeypatch.setattr(config, "MAX_CHARS", 4)
    player.enqueue("one two three four")
    assert len(player._pending()) > 1
    player.stop()
    assert player._pending() == []


def test_skip_current_preserves_queue_and_global_interrupt(monkeypatch):
    killed = []
    own(config.SPEECH_PID, 4242, "speech")
    monkeypatch.setattr(
        processes.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    player.enqueue("current response")
    interrupt = player._interrupt_token()

    assert player.skip()

    assert killed == [(4242, player.signal.SIGTERM)]
    assert len(player._pending()) == 1
    assert player._interrupt_token() == interrupt


def test_message_queued_after_skip_still_plays(recorder):
    player.skip()
    player.enqueue("future message")

    player.drain()

    assert recorder == ["future message"]


def test_skip_during_synthesis_moves_to_next_queued_message(
        recorder, monkeypatch):
    synthesized = []

    def synthesize(text, voice, model, provider):
        synthesized.append(text)
        if text == "skip this block":
            player.skip()
        return text.encode()

    monkeypatch.setattr(player, "synthesize", synthesize)
    player.enqueue("skip this block")
    player.enqueue("keep this message")

    player.drain()

    assert synthesized == ["skip this block", "keep this message"]
    assert recorder == ["keep this message"]


def test_skip_during_output_warmup_moves_to_next_queued_block(
        recorder, monkeypatch):
    warmups = []

    def warmup():
        warmups.append(True)
        player.skip()

    monkeypatch.setattr(player, "_wake_output", warmup)
    player.enqueue("skip during warmup")
    player.enqueue("play after warmup")

    player.drain()

    assert warmups == [True]
    assert recorder == ["play after warmup"]


def test_skip_during_playback_moves_to_next_queued_block(
        recorder, monkeypatch):
    played = []

    def play(audio, interrupt=None, skip=None):
        played.append(audio.decode())
        if len(played) == 1:
            player.skip()
        return True

    monkeypatch.setattr(player, "play", play)
    player.enqueue("skip while audible")
    player.enqueue("play this next")

    player.drain()

    assert played == ["skip while audible", "play this next"]


def test_interrupting_playback_suppresses_the_done_chime(monkeypatch):
    from parley import cues

    chimed = []
    monkeypatch.setattr(player, "synthesize", lambda text, v, m, p: b"audio")
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    monkeypatch.setattr(
        player, "play",
        lambda audio, interrupt=None, skip=None: player.stop(),
    )
    monkeypatch.setattr(cues, "play", lambda name: chimed.append(name))
    player.enqueue("please stop me")

    player.drain()

    assert chimed == []
    assert config.INTERRUPT.exists()


def test_stop_terminates_the_active_audio_process(monkeypatch):
    killed = []
    own(config.SPEECH_PID, 4242, "speech")
    monkeypatch.setattr(processes.os, "kill", lambda pid, signal: killed.append(
        (pid, signal)))

    player.stop()

    assert killed == [(4242, 15)]


def test_stop_never_signals_historical_pidfile_entries(monkeypatch):
    """A PID reused after natural completion must never become a signal target."""
    config.PIDFILE.write_text("41001\n41002\n")
    killed = []
    monkeypatch.setattr(
        processes.os, "kill", lambda pid, signal: killed.append((pid, signal))
    )

    player.stop()

    assert killed == []
    assert not config.PIDFILE.exists()


def test_reused_speech_pid_is_recovered_without_a_signal(monkeypatch):
    births = {4242: "original-birth"}
    monkeypatch.setattr(processes, "process_identity", births.get)
    own(config.SPEECH_PID, 4242, "speech")
    births[4242] = "unrelated-reused-birth"
    killed = []
    monkeypatch.setattr(
        processes.os, "kill", lambda pid, signal: killed.append((pid, signal))
    )

    assert not player.skip()

    assert killed == []
    assert not config.SPEECH_PID.exists()


def test_naturally_completed_playback_releases_ownership(monkeypatch):
    seen_while_running = []

    class Process:
        pid = 4242

        def wait(self):
            seen_while_running.append(player.output_playing())
            return 0

    monkeypatch.setattr(player.subprocess, "Popen", lambda *args, **kwargs: Process())

    assert player.play(b"audio")

    assert seen_while_running == [True]
    assert not config.SPEECH_PID.exists()
    assert not config.PIDFILE.exists()


def test_drainer_is_active_during_synthesis_and_clears_marker(monkeypatch):
    active_during_synthesis = []

    def synthesize(text, voice, model, provider):
        active_during_synthesis.append(player.active())
        return b"audio"

    monkeypatch.setattr(player, "synthesize", synthesize)
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    monkeypatch.setattr(
        player, "play", lambda audio, interrupt=None, skip=None: True)
    player.enqueue("hello")

    player.drain()

    assert active_during_synthesis == [True]
    assert not config.DRAIN_PID.exists()
    assert not player.active()


def test_runtime_contract_tracks_queue_synthesis_and_playback(monkeypatch):
    observed = []

    def synthesize(text, voice, model, provider):
        status = player.runtime.snapshot()
        observed.append((
            "synthesis",
            status["queue"]["depth"],
            status["speech"].copy(),
        ))
        return b"audio"

    def play(audio, interrupt=None, skip=None):
        status = player.runtime.snapshot()
        observed.append((
            "playback",
            status["queue"]["depth"],
            status["speech"].copy(),
        ))
        return True

    monkeypatch.setattr(player, "synthesize", synthesize)
    monkeypatch.setattr(player, "play", play)
    monkeypatch.setattr(player, "_wake_output", lambda: None)

    player.enqueue("private content that status must not copy")
    queued = player.runtime.snapshot()
    assert queued["queue"]["depth"] == 1
    assert "private content" not in json.dumps(queued)

    player.drain()

    assert observed == [
        ("synthesis", 0, {"synthesis": "active", "playback": "idle"}),
        ("playback", 0, {"synthesis": "idle", "playback": "active"}),
    ]
    finished = player.runtime.snapshot()
    assert finished["queue"]["depth"] == 0
    assert finished["speech"] == {"synthesis": "idle", "playback": "idle"}


def test_speech_continues_when_runtime_status_cannot_be_written(
        recorder, monkeypatch):
    monkeypatch.setattr(
        player.runtime,
        "_write_unlocked",
        lambda _snapshot: (_ for _ in ()).throw(OSError("read-only state")),
    )

    assert player.enqueue("still speak this")
    assert player.drain()

    assert recorder == ["still speak this"]


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
        player, "play", lambda audio, interrupt=None, skip=None: pytest.fail(
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
        player, "play", lambda audio, interrupt=None, skip=None: pytest.fail(
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


def test_pause_preserves_queue_and_resume_keeps_interrupt_token(monkeypatch):
    signals = []
    own(config.SPEECH_PID, 4242, "speech")
    monkeypatch.setattr(
        processes.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    player.enqueue("current response")
    interrupt = player._interrupt_token()

    assert player.pause()
    assert len(player._pending()) == 1
    assert player._interrupt_token() == interrupt
    assert player.resume()

    assert signals == [
        (4242, player.signal.SIGTERM),
    ]
    assert len(player._pending()) == 1
    assert player._interrupt_token() == interrupt


def test_new_speech_waits_for_exclusive_microphone_turn(monkeypatch):
    from parley import cues

    synthesized = threading.Event()
    played = threading.Event()

    def synthesize(text, voice, model, provider):
        synthesized.set()
        return b"audio"

    monkeypatch.setattr(player, "synthesize", synthesize)
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    monkeypatch.setattr(cues, "play", lambda name: None)
    monkeypatch.setattr(
        player, "play",
        lambda audio, interrupt=None, skip=None: played.set())
    player.pause()
    player.enqueue("queued while Chris is dictating")
    thread = threading.Thread(target=player.drain)
    thread.start()

    assert synthesized.wait(timeout=1)
    assert not played.wait(timeout=0.15)
    player.resume()
    thread.join(timeout=2)

    assert played.is_set()
    assert not thread.is_alive()


def test_explicit_stop_discards_paused_and_queued_speech(monkeypatch):
    killed = []
    own(config.SPEECH_PID, 4242, "speech")
    monkeypatch.setattr(
        processes.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    player.pause()
    player.enqueue("this must be discarded")

    player.stop()

    assert player._pending() == []
    assert not config.MIC_TURN.exists()
    assert not config.SPEECH_PID.exists()
    assert (4242, 15) in killed


def test_microphone_turn_opening_during_audio_launch_restarts_new_process(
        monkeypatch):
    launched = []

    class Process:
        def __init__(self):
            self.pid = 4242 + len(launched)
            self.terminated = False
            launched.append(self)
            if len(launched) == 1:
                own(config.MIC_TURN, os.getpid(), "microphone-turn")

        def terminate(self):
            self.terminated = True
            config.MIC_TURN.unlink(missing_ok=True)

        def wait(self):
            return -player.signal.SIGTERM if self.terminated else 0

    import os

    def kill(pid, sig):
        if pid == os.getpid() and sig == 0:
            return

    monkeypatch.setattr(
        player.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(processes.os, "kill", kill)
    monkeypatch.setattr(player, "_remaining_audio", lambda path, offset: path)

    assert player.play(b"audio", player._interrupt_token())
    assert len(launched) == 2
    assert launched[0].terminated


def test_paused_playback_restarts_remaining_audio_after_send(monkeypatch):
    launched = []
    first_started = threading.Event()
    first_stopped = threading.Event()
    offsets = []

    class Process:
        def __init__(self):
            self.pid = 5000 + len(launched)
            self.terminated = False
            launched.append(self)

        def terminate(self):
            self.terminated = True
            first_stopped.set()

        def wait(self):
            if len(launched) == 1:
                first_started.set()
                assert first_stopped.wait(timeout=1)
                return -player.signal.SIGTERM
            return 0

    monkeypatch.setattr(
        player.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: (
        launched[-1].terminate() if sig == player.signal.SIGTERM else None))
    monkeypatch.setattr(
        player, "_remaining_audio",
        lambda path, offset: offsets.append(offset) or path,
    )

    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            player.play(b"a response with an unheard remainder",
                        player._interrupt_token())))
    thread.start()
    assert first_started.wait(timeout=1)

    time.sleep(0.3)
    assert player.pause()
    assert first_stopped.wait(timeout=1)
    assert thread.is_alive(), "speech must wait while dictation owns the floor"
    assert player.resume()
    thread.join(timeout=2)

    assert result == [True]
    assert len(launched) == 2
    assert offsets and offsets[0] > 0
    assert not thread.is_alive()


def test_natural_completion_during_pause_race_is_not_replayed(monkeypatch):
    launched = []

    class Process:
        pid = 4242

        def wait(self):
            config.private_write(config.PAUSE, "new turn")
            return 0

    monkeypatch.setattr(
        player.subprocess, "Popen",
        lambda *args, **kwargs: launched.append(args) or Process(),
    )

    assert player.play(b"already complete", player._interrupt_token())
    assert len(launched) == 1


def test_stale_microphone_turn_recovers_paused_speech(monkeypatch):
    signals = []
    births = {99999: "original-birth"}
    monkeypatch.setattr(processes, "process_identity", births.get)
    own(config.MIC_TURN, 99999, "microphone-turn")
    births[99999] = "unrelated-reused-birth"

    def kill(pid, sig):
        if pid == 99999 and sig == 0:
            raise ProcessLookupError
        signals.append((pid, sig))

    monkeypatch.setattr(processes.os, "kill", kill)

    assert not player.microphone_active()
    assert not config.MIC_TURN.exists()
    assert signals == []


def test_stale_drainer_marker_does_not_report_active(monkeypatch):
    births = {7777: "original-birth"}
    monkeypatch.setattr(processes, "process_identity", births.get)
    own(config.DRAIN_PID, 7777, "drainer")
    births[7777] = "unrelated-reused-birth"

    assert not player.active()
    assert not config.DRAIN_PID.exists()
