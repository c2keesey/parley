import pytest

from parley import listen
from parley.listen import (
    contains_phrase,
    contains_wake,
    is_cancel,
    is_send,
    is_stop_talking,
    rms,
    strip_phrase,
    strip_wake_phrases,
)


@pytest.fixture(autouse=True)
def isolate_microphone_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(listen.player, "pause", lambda: False)
    monkeypatch.setattr(listen.player, "resume", lambda: False)
    monkeypatch.setattr(listen.player, "microphone_active", lambda: False)
    monkeypatch.setattr(listen.player, "output_playing", lambda: False)
    monkeypatch.setattr(listen.audio, "default_input_device", lambda: None)
    monkeypatch.setattr(listen.config, "LISTENER_STATE", tmp_path / "listener.state")
    monkeypatch.setattr(listen.config, "TRIGGERS", tmp_path / "triggers")
    monkeypatch.setattr(listen.config, "STATE", tmp_path)
    monkeypatch.setattr(listen, "TARGET", tmp_path / "target")
    listen.triggers.load.cache_clear()
    monkeypatch.setattr(listen.indicator, "refresh", lambda: None)
    # Synthetic test tones are not speech to WebRTC's VAD; the gate's own
    # tests cover the VAD path. Here the energy gate alone is deterministic.
    monkeypatch.setattr(listen.gate, "default_vad", lambda: None)
    monkeypatch.setattr(listen.echo.config, "STATE", tmp_path)


def test_active_deck_target_uses_most_recent_human_tmux_client(monkeypatch):
    output = (
        "\t%control\t999\tagentdeck_background_11111111\n"
        "/dev/ttys001\t%old\t100\tagentdeck_old_22222222\n"
        "/dev/ttys002\t%active\t200\tagentdeck_active_33333333\n"
        "/dev/ttys003\t%shell\t300\tplain-shell\n"
    )
    monkeypatch.setattr(
        listen.subprocess,
        "run",
        lambda *args, **kwargs: listen.subprocess.CompletedProcess(
            args[0], 0, stdout=output),
    )

    assert listen.active_deck_target() == "%active"


def test_sync_target_follows_deck_focus_and_retains_static_fallback(
        tmp_path, monkeypatch):
    listen.set_target("%armed")
    monkeypatch.setattr(listen, "active_deck_target", lambda: "%focused")
    logs = []
    monkeypatch.setattr(listen.config, "log", logs.append)

    assert listen.sync_target() == "%armed"

    listen.set_follow_active(True)
    assert listen.sync_target() == "%focused"
    assert listen.get_target() == "%focused"
    assert logs == ["listener target followed active deck pane %focused"]


def test_inject_rechecks_active_target_at_send_time(monkeypatch):
    calls = []
    monkeypatch.setattr(listen, "sync_target", lambda: "%now-focused")
    monkeypatch.setattr(
        listen.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command)
        or listen.subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(listen.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    assert listen.inject("route this")
    assert any(command[-2:] == ["-t", "%now-focused"] for command in calls)


@pytest.mark.parametrize("heard", [
    "okay computer",
    "Okay, computer.",
    "um okay computer are you there",
    "OKAY COMPUTER!",
])
def test_wake_phrase_survives_punctuation_and_filler(heard):
    assert contains_phrase(heard, "okay computer")


@pytest.mark.parametrize("heard", [
    "okay",
    "computer",
    "okay the computer is slow",
    "that computer is okay",
    "",
])
def test_near_misses_do_not_wake(heard):
    """Over-triggering is the failure mode this whole design exists to avoid."""
    assert not contains_phrase(heard, "okay computer")


@pytest.mark.parametrize("heard", [
    "okay computer",
    "Okay, computer?",
    "OK computer.",
    "Okay, computers.",
    "OK, computer's!",
])
def test_repeated_wake_tolerates_realistic_transcription_variants(heard):
    assert contains_wake(heard)


@pytest.mark.parametrize("heard", [
    "okay",
    "computer",
    "okay supercomputer",
    "okay computerized",
    "computer okay",
])
def test_repeated_wake_rejects_near_misses(heard):
    assert not contains_wake(heard)


def test_final_message_uses_local_whisper_without_api_key(tmp_path, monkeypatch):
    model = tmp_path / "ggml-base.en.bin"
    model.touch()
    monkeypatch.setattr(listen, "MESSAGE_MODEL", model)
    monkeypatch.setattr(listen.config, "api_key", lambda: None)
    monkeypatch.setattr(listen, "whisper_bin", lambda: "whisper-cli")
    monkeypatch.setattr(
        listen.subprocess,
        "run",
        lambda argv, **kwargs: type("Result", (), {"stdout": "Local message.\n"})(),
    )

    assert listen.transcribe_cloud([b"\x00\x00"] * 400) == "Local message."


@pytest.mark.parametrize(("spoken", "expected"), [
    (
        "okay computer draft the note Okay, computer and keep going send it",
        "draft the note and keep going send it",
    ),
    (
        "playback words OK computer start here. Okay, computers, continue.",
        "start here. continue",
    ),
    (
        "Okay, computer's begin. OK, computer, finish.",
        "begin. finish",
    ),
])
def test_all_wake_phrases_are_removed_from_cloud_transcription(spoken, expected):
    assert strip_wake_phrases(spoken) == expected


def test_send_phrase_is_stripped_from_the_message():
    assert strip_phrase("run the tests send it", "send it") == "run the tests"
    assert strip_phrase("Run the tests, send it.", "send it") == "Run the tests"


def test_send_phrase_is_only_stripped_from_the_end():
    assert strip_phrase("send it to staging", "send it") == "send it to staging"


@pytest.mark.parametrize("heard", [
    "send it",
    "Send it.",
    "finish this and send it",
])
def test_send_command_must_be_trailing(heard):
    assert is_send(heard)


@pytest.mark.parametrize("heard", [
    "deploy on Sunday",
    "Sunday is the maintenance window",
    "finish this and send it now",
    "the send it command is not always going through",
    "I said send it in the middle and kept talking",
    "Sunday.",
    "send",
    "send this",
])
def test_ordinary_dictation_does_not_submit(heard):
    assert not is_send(heard)


def test_silence_reads_as_silence():
    assert rms(b"\x00\x00" * 512) == 0


def test_loud_audio_reads_as_loud():
    assert rms(b"\xff\x7f" * 512) > 30000


def test_short_chunk_is_not_an_error():
    assert rms(b"\x00") == 0


def test_listener_state_is_persisted_and_refreshed(tmp_path, monkeypatch):
    refreshed = []
    monkeypatch.setattr(listen.config, "LISTENER_STATE", tmp_path / "state")
    monkeypatch.setattr(listen.indicator, "refresh", lambda: refreshed.append(True))

    listen.set_listener_state("capturing")

    assert listen.listener_state() == "capturing"
    assert refreshed == [True]


def test_submission_log_does_not_include_dictated_text(monkeypatch):
    calls = []
    logs = []
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(
        listen.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(argv) or type(
            "Result", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(listen.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(listen.config, "log", logs.append)

    assert listen.inject("my private dictated message")
    assert calls[-1][-1] == "Enter"
    assert logs == ["submitted to %42 chars=27"]
    assert "private" not in logs[0]


class FakeAudioStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read(self, size):
        return next(self.chunks, b"")


class FakeAudioProcess:
    def __init__(self, chunks):
        self.stdout = FakeAudioStream(chunks)

    def terminate(self):
        pass


def audio_chunks(voiced_frames):
    quiet = b"\x00\x00" * listen.FRAME
    loud = b"\xff\x7f" * listen.FRAME
    silence_frames = int(listen.END_SILENCE * listen.RATE / listen.FRAME) + 1
    return [quiet] * 4 + [loud] * voiced_frames + [quiet] * silence_frames


def frames_for(seconds):
    return max(1, round(seconds * listen.RATE / listen.FRAME))


def test_fast_command_sized_burst_reaches_local_recognition(monkeypatch):
    process = FakeAudioProcess(audio_chunks(frames_for(0.3)))
    monkeypatch.setattr(listen.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(listen, "speaking", lambda: False)

    actual = [burst for burst, _ in listen.bursts() if burst is not None]

    assert len(actual) == 1
    assert not actual[0].short
    # Pre-roll: the quiet frames just before the onset are kept, so a soft
    # first consonant is not clipped off the word.
    assert actual[0][0] == b"\x00\x00" * listen.FRAME


def test_click_sized_burst_never_opens_the_gate(monkeypatch):
    process = FakeAudioProcess(audio_chunks(1))
    logs = []
    monkeypatch.setattr(listen.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(listen.config, "log", logs.append)

    actual = [burst for burst, _ in listen.bursts() if burst is not None]

    assert actual == []
    assert logs == []


def test_brief_audio_is_yielded_as_short_and_logged(monkeypatch):
    process = FakeAudioProcess(audio_chunks(frames_for(0.06)))
    logs = []
    monkeypatch.setattr(listen.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(listen.config, "log", logs.append)

    actual = [burst for burst, _ in listen.bursts() if burst is not None]

    assert len(actual) == 1 and actual[0].short
    assert logs and logs[0].startswith("gate short audio voiced=0.06s")


def test_default_input_route_change_reopens_capture(monkeypatch):
    quiet = b"\x00\x00" * listen.FRAME
    heartbeat_frames = int(listen.RATE / listen.FRAME) + 1
    processes = [
        FakeAudioProcess([quiet] * heartbeat_frames),
        FakeAudioProcess([quiet]),
    ]
    launched = []
    devices = iter([101, 202])
    monkeypatch.setattr(
        listen.audio, "default_input_device", lambda: next(devices, 202))
    monkeypatch.setattr(
        listen.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command)
        or processes.pop(0),
    )
    monkeypatch.setattr(listen.time, "sleep", lambda seconds: None)

    events = listen.bursts("default", reconnect=True)

    assert next(events) == (None, "capture-ready")
    assert next(events) == (None, "capture-reset")
    assert next(events) == (None, "capture-ready")
    assert len(launched) == 2
    assert launched[0][launched[0].index("-i") + 1] == ":default"
    events.close()


def test_input_loss_during_dictation_releases_speech_and_reconnects(
        tmp_path, monkeypatch):
    states = []
    resumed = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "set_listener_state", states.append)
    monkeypatch.setattr(listen.player, "resume", lambda: resumed.append(True))
    monkeypatch.setattr(listen.config, "log", lambda message: None)
    monkeypatch.setattr(
        listen,
        "bursts",
        lambda device: iter([
            ([b"wake audio"] * 3, False),
            (None, "capture-reset"),
            (None, "capture-ready"),
        ]),
    )
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: listen.WAKE)
    monkeypatch.setattr(listen, "cue", lambda kind: None)

    listen.run()

    assert "capturing" in states
    assert "reconnecting" in states
    assert states[-1] == "ready"
    assert resumed


def test_possible_wake_holds_speech_before_local_recognition(monkeypatch):
    """A reply reaching playback mid-utterance must wait for classification."""
    import threading

    first_frame_claimed = threading.Event()
    finish_burst = threading.Event()
    waiter_finished = threading.Event()
    microphone_turn = threading.Event()

    class RacingAudioStream:
        def __init__(self):
            self.chunks = iter(audio_chunks(frames_for(0.3)))
            self.reads = 0

        def read(self, size):
            chunk = next(self.chunks, b"")
            self.reads += 1
            # Four quiet frames, then the gate opens on the second loud one.
            if self.reads == 7:
                first_frame_claimed.set()
                assert finish_burst.wait(timeout=1)
            return chunk

    process = FakeAudioProcess([])
    process.stdout = RacingAudioStream()
    monkeypatch.setattr(listen.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(listen.player, "output_playing", lambda: False)
    monkeypatch.setattr(listen.player, "pause", lambda: microphone_turn.set())
    monkeypatch.setattr(listen.player, "resume", lambda: microphone_turn.clear())
    monkeypatch.setattr(
        listen.player, "microphone_active", lambda: microphone_turn.is_set())

    classified = []
    collector = threading.Thread(
        target=lambda: classified.append(next(
            burst for burst, _ in listen.bursts() if burst is not None)))
    collector.start()
    assert first_frame_claimed.wait(timeout=1)
    assert microphone_turn.is_set()

    waiter = threading.Thread(target=lambda: (
        listen.player._wait_for_microphone(), waiter_finished.set()))
    waiter.start()
    assert not waiter_finished.wait(timeout=0.15)

    finish_burst.set()
    collector.join(timeout=1)
    assert classified
    assert not waiter_finished.wait(timeout=0.15)

    # A non-wake classification would release here; a wake promotes the same
    # marker until send/cancel instead.
    listen.player.resume()
    assert waiter_finished.wait(timeout=1)
    waiter.join(timeout=1)


def test_non_wake_candidate_promptly_releases_provisional_turn(
        tmp_path, monkeypatch):
    actions = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(
        listen, "bursts", lambda device: iter([([b"ordinary speech"], False)]))
    monkeypatch.setattr(
        listen, "transcribe_local", lambda frames: "ordinary room speech")
    monkeypatch.setattr(listen.player, "resume", lambda: actions.append("resume"))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert actions == ["resume"]


@pytest.mark.parametrize("heard", [
    "scrap that",
    "Scrap that.",
    "okay scrap that",
    "scratch that",
    "Scratch that.",
    "okay, scratch that!",
    "wait scratch that",
])
def test_cancel_phrase_is_recognised(heard):
    assert is_cancel(heard)


@pytest.mark.parametrize("heard", [
    "cancel it",
    "stop",
    "scrap the old migration and start over",
    "that scrap heap of a function",
    "scratch that migration and start over",
    "Also the scratch that didn't work",
    "say the words scratch that",
    "we should scratch that",
    "scratch the old migration",
])
def test_ordinary_dictation_does_not_cancel(heard):
    """A discard phrase that fires by accident is worse than none."""
    assert not is_cancel(heard)


def test_scratch_that_cancels_locally_without_transcribing_or_sending(
        tmp_path, monkeypatch):
    cues = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"cancel"], False),
    ]))
    heard = iter(["okay computer start a message", "scratch that."])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: pytest.fail("cancel must remain local"),
    )
    monkeypatch.setattr(
        listen, "inject", lambda text: pytest.fail("cancel must not send chat"))
    monkeypatch.setattr(listen, "cue", lambda kind: cues.append(kind))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert cues == ["wake", "cancel"]


def test_timeouts_are_ordered_sensibly():
    """Silence should stop a capture long before the hard cap does."""
    from parley.listen import HARD_STOP, SILENCE_TIMEOUT
    assert 0 < SILENCE_TIMEOUT < HARD_STOP
    assert SILENCE_TIMEOUT <= 180, "a stuck capture should not linger"
    assert HARD_STOP <= 1800, "a wedged listener must not send a colossal transcript"


def test_wake_phrase_is_what_makes_overlapping_audio_safe():
    """Barge-in acts on audio that may contain the agent's own voice, so the
    wake phrase must not appear in ordinary spoken replies."""
    from parley.listen import WAKE
    for reply in [
        "Okay, I updated the computer configuration and the tests pass.",
        "The computer science approach here is fine.",
        "Okay. That is done.",
    ]:
        assert not contains_phrase(reply, WAKE)


@pytest.mark.parametrize("heard", [
    "okay computer stop talking",
    "Okay, computer, stop talking!",
    "Okay, computers stop talking.",
    "OK computers, stop talking!",
    "Um, okay, computer's stop talking now.",
])
def test_stop_talking_is_a_local_control_only_during_playback(heard):
    assert is_stop_talking(heard, overlapped=True)
    assert not is_stop_talking(heard, overlapped=False)


@pytest.mark.parametrize("heard", [
    "stop talking",
    "okay computer stop the server",
    "okay computer talk about stop conditions",
    "okay supercomputers stop talking",
    "okay computer stops talking",
    "okay computers stop the talking process",
])
def test_stop_talking_near_misses_remain_normal_input(heard):
    assert not is_stop_talking(heard, overlapped=True)


def test_stop_talking_stays_local_and_confirms_with_one_cue(tmp_path, monkeypatch):
    actions = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"voice control"], True),
    ]))
    monkeypatch.setattr(
        listen, "transcribe_local",
        lambda frames: "Okay, computers stop talking.",
    )
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: pytest.fail("must remain local"),
    )
    monkeypatch.setattr(
        listen, "inject", lambda text: pytest.fail("must not send chat"))
    monkeypatch.setattr(listen, "cue", lambda kind: actions.append(f"cue:{kind}"))
    monkeypatch.setattr(listen.player, "skip", lambda: actions.append("skipped"))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert actions == ["skipped", "cue:stop"]


def test_other_wake_input_keeps_normal_dictation_flow(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"message"], False),
    ]))
    heard = iter(["okay computer run the tests", "send it"])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: "okay computer run the tests send it",
    )
    monkeypatch.setattr(listen, "inject", lambda text: sent.append(text))
    monkeypatch.setattr(listen, "cue", lambda kind: None)
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert sent == ["run the tests"]


def test_trailing_send_command_submits_and_releases_microphone_turn(
        tmp_path, monkeypatch):
    actions = []
    sent = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"message"], False),
        ([b"send"], False),
    ]))
    heard = iter(["okay computer", "run the real path", "send it."])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: "okay computer run the real path send it",
    )
    monkeypatch.setattr(listen, "inject", lambda text: sent.append(text))
    monkeypatch.setattr(listen.player, "pause", lambda: actions.append("pause"))
    monkeypatch.setattr(listen.player, "resume", lambda: actions.append("resume"))
    monkeypatch.setattr(listen, "cue", lambda kind: actions.append(kind))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert sent == ["run the real path"]
    assert actions == ["pause", "wake", "send", "resume"]


def test_empty_local_transcription_is_identifiable_in_listener_log(
        tmp_path, monkeypatch):
    logs = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(
        listen, "bursts", lambda device: iter([([b"audio"] * 7, False)]))
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: "")
    monkeypatch.setattr(listen.config, "log", logs.append)

    listen.run()

    assert "local transcription empty frames=7" in logs


def test_send_it_inside_content_does_not_end_capture_or_get_removed(
        tmp_path, monkeypatch):
    actions = []
    sent = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"internal phrase"], False),
        ([b"continued message"], False),
        ([b"send"], False),
    ]))
    heard = iter([
        "okay computer",
        "I said send it in the middle and kept talking",
        "so this content must still be captured",
        "send it.",
    ])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: (
            "okay computer I said send it in the middle and kept talking "
            "so this content must still be captured send it"
        ),
    )
    monkeypatch.setattr(listen, "inject", lambda text: sent.append(text))
    monkeypatch.setattr(listen.player, "pause", lambda: actions.append("pause"))
    monkeypatch.setattr(listen.player, "resume", lambda: actions.append("resume"))
    monkeypatch.setattr(listen, "cue", lambda kind: actions.append(kind))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert sent == [
        "I said send it in the middle and kept talking "
        "so this content must still be captured",
    ]
    assert actions == ["pause", "wake", "send", "resume"]


def test_personalized_audio_recovers_wake_and_send_asr_misses(
        tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"content"], False),
        ([b"send"], False),
    ]))
    heard = iter([
        "okay something is bugging out",
        "the send it command is unreliable",
        "Sunday.",
    ])
    matches = iter([
        ("wake", 0.1, 0.3),
        (None, 0.8, 0.3),
        ("send", 0.1, 0.3),
    ])
    monkeypatch.setattr(listen.triggers, "enrolled", lambda: True)
    monkeypatch.setattr(listen.triggers, "match", lambda frames, allowed: next(matches))
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: (
            "okay computer the send it command is unreliable send it"),
    )
    monkeypatch.setattr(listen, "inject", sent.append)
    monkeypatch.setattr(listen, "cue", lambda kind: None)
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert sent == ["the send it command is unreliable"]


def test_local_whisper_is_biased_toward_control_phrases(tmp_path, monkeypatch):
    model = tmp_path / "tiny.bin"
    model.write_bytes(b"model")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return listen.subprocess.CompletedProcess(command, 0, stdout="Send it.")

    monkeypatch.setattr(listen, "TINY", model)
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/opt/whisper-cli")
    monkeypatch.setattr(listen.subprocess, "run", run)

    assert listen.transcribe_local([b"\x00\x00"] * 10) == "Send it."
    prompt_index = commands[0].index("--prompt") + 1
    assert "send it" in commands[0][prompt_index].lower()


def test_tmux_submission_checks_typing_and_enter(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return listen.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(listen.subprocess, "run", run)
    monkeypatch.setattr(listen.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    assert listen.inject("run the tests")
    buffer = f"parley-dictation-{listen.os.getpid()}"
    assert calls == [
        ["tmux", "load-buffer", "-b", buffer, "-"],
        ["tmux", "paste-buffer", "-d", "-p", "-r", "-b", buffer, "-t", "%42"],
        ["tmux", "send-keys", "-t", "%42", "Enter"],
    ]


def test_long_dictation_is_pasted_whole_not_typed_key_by_key(monkeypatch):
    """A 1360-character message typed with send-keys -l lost its head."""
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs.get("input")))
        return listen.subprocess.CompletedProcess(command, 0)

    message = "This session is going to focus on all the Parley stuff. " * 25
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(listen.subprocess, "run", run)
    monkeypatch.setattr(listen.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    assert listen.inject(message)
    staged = [stdin for command, stdin in calls if "load-buffer" in command]
    assert staged == [message.strip()]
    assert not any("-l" in command for command, _ in calls)
    assert calls[-1][0][-1] == "Enter"


@pytest.mark.parametrize("failure", [0, 1, 2])
def test_tmux_submission_reports_each_failure_without_claiming_success(
        failure, monkeypatch):
    calls = []
    logs = []

    def run(command, **kwargs):
        calls.append(command)
        return listen.subprocess.CompletedProcess(
            command, 1 if len(calls) - 1 == failure else 0)

    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(listen.subprocess, "run", run)
    monkeypatch.setattr(listen.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(listen.config, "log", lambda message: logs.append(message))

    assert not listen.inject("run the tests")
    assert logs[-1].startswith("submission failed:")


def test_wake_rechecks_just_starting_playback_and_barges_in(
        tmp_path, monkeypatch):
    actions = []
    sent = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"message"], True),
    ]))
    heard = iter(["okay computer", "talk to me send it"])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(listen, "speaking", lambda: True)
    monkeypatch.setattr(listen.player, "pause", lambda: actions.append("paused"))
    monkeypatch.setattr(listen.player, "resume", lambda: actions.append("resumed"))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: "okay computer talk to me send it",
    )
    monkeypatch.setattr(listen, "inject", lambda text: sent.append(text))
    monkeypatch.setattr(listen, "cue", lambda kind: actions.append(kind))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert actions == ["paused", "wake", "send", "resumed"]
    assert sent == ["talk to me"]


def test_dictation_turn_pauses_before_wake_and_resumes_after_injection(
        tmp_path, monkeypatch):
    actions = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], True),
        ([b"message"], False),
    ]))
    heard = iter(["okay computer", "reply to this send it"])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(listen.player, "pause", lambda: actions.append("pause"))
    monkeypatch.setattr(listen.player, "resume", lambda: actions.append("resume"))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: "okay computer reply to this send it",
    )
    monkeypatch.setattr(listen, "inject", lambda text: actions.append(f"inject:{text}"))
    monkeypatch.setattr(listen, "cue", lambda kind: actions.append(f"cue:{kind}"))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert actions == [
        "pause",
        "cue:wake",
        "cue:send",
        "inject:reply to this",
        "resume",
    ]


def test_cancel_releases_exclusive_microphone_turn(tmp_path, monkeypatch):
    actions = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"cancel"], False),
    ]))
    heard = iter(["okay computer", "scratch that"])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(listen.player, "pause", lambda: actions.append("pause"))
    monkeypatch.setattr(listen.player, "resume", lambda: actions.append("resume"))
    monkeypatch.setattr(listen, "cue", lambda kind: actions.append(kind))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert actions == ["pause", "wake", "cancel", "resume"]


def test_transcription_failure_releases_exclusive_microphone_turn(
        tmp_path, monkeypatch):
    actions = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"send"], False),
    ]))
    heard = iter(["okay computer", "send it"])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(listen.player, "pause", lambda: actions.append("pause"))
    monkeypatch.setattr(listen.player, "resume", lambda: actions.append("resume"))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(listen, "cue", lambda kind: actions.append(kind))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert actions == ["pause", "wake", "send", "resume"]


def test_repeated_wake_replays_tone_keeps_listening_and_is_not_sent(
        tmp_path, monkeypatch):
    cues = []
    sent = []
    cloud_calls = []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter([
        ([b"wake"], False),
        ([b"repeat"], False),
        ([b"message"], False),
        ([b"send"], False),
    ]))
    heard = iter([
        "okay computer start a message",
        "Okay, computers!",
        "continue listening",
        "send it",
    ])
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))

    def transcribe(frames):
        cloud_calls.append(frames)
        return (
            "okay computer start a message Okay, computers! "
            "continue listening send it"
        )

    monkeypatch.setattr(listen, "transcribe_cloud", transcribe)
    monkeypatch.setattr(listen, "inject", lambda text: sent.append(text))
    monkeypatch.setattr(listen, "cue", lambda kind: cues.append(kind))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert cues == ["wake", "wake", "send"]
    assert len(cloud_calls) == 1
    assert sent == ["start a message continue listening"]


def test_strip_leading_removes_anything_said_before_the_wake_phrase():
    """After barge-in the burst can contain the agent's speech; everything up
    to and including the wake phrase is dropped."""
    from parley.listen import WAKE, strip_leading
    heard = "the tests are green and I pushed it okay computer stop and check the log"
    assert strip_leading(heard, WAKE) == "stop and check the log"


# --- adaptive gate, echo, and message assembly regressions -----------------

def _run_listener(tmp_path, monkeypatch, bursts, heard, cloud=None,
                  speaking=False, logs=None):
    sent, actions, cloud_frames = [], [], []
    monkeypatch.setattr(listen, "LISTEN_PID", tmp_path / "listener.pid")
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/bin/true")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen.indicator, "ensure", lambda: 0)
    monkeypatch.setattr(listen, "bursts", lambda device: iter(bursts))
    heard = iter(heard)
    monkeypatch.setattr(listen, "transcribe_local", lambda frames: next(heard))
    monkeypatch.setattr(
        listen, "transcribe_cloud",
        lambda frames: cloud_frames.append(list(frames)) or cloud or "")
    monkeypatch.setattr(listen, "inject", sent.append)
    monkeypatch.setattr(listen, "speaking", lambda: speaking)
    monkeypatch.setattr(listen.player, "skip", lambda: actions.append("skip"))
    monkeypatch.setattr(listen, "cue", lambda kind: actions.append(kind))
    monkeypatch.setattr(
        listen.config, "log",
        (logs.append if logs is not None else (lambda message: None)))
    listen.run()
    return sent, actions, cloud_frames


def test_short_bursts_during_dictation_stay_in_the_message(
        tmp_path, monkeypatch):
    """A quiet word mid-dictation used to be dropped before the cloud saw it."""
    short = listen.gate.Burst([b"soft-start"], voiced=0.06, short=True)
    sent, _, cloud_frames = _run_listener(
        tmp_path, monkeypatch,
        [([b"wake"], False), (short, False), ([b"send"], False)],
        ["okay computer", "send it."],
        cloud="okay computer check the logs send it")

    assert sent == ["check the logs"]
    assert cloud_frames == [[b"wake", b"soft-start", b"send"]]


def test_short_burst_outside_capture_never_reaches_whisper(
        tmp_path, monkeypatch):
    short = listen.gate.Burst([b"click"], voiced=0.06, short=True)
    sent, actions, _ = _run_listener(
        tmp_path, monkeypatch, [(short, False)], [])

    assert sent == [] and actions == []


def test_personalized_miss_never_blocks_whisper(tmp_path, monkeypatch):
    """The matcher used to reject every short burst it did not recognise."""
    monkeypatch.setattr(listen.triggers, "enrolled", lambda: True)
    monkeypatch.setattr(
        listen.triggers, "match", lambda frames, allowed: (None, 0.9, 0.3))
    burst = listen.gate.Burst([b"wake"], voiced=0.13)
    sent, actions, _ = _run_listener(
        tmp_path, monkeypatch, [(burst, False), ([b"send"], False)],
        ["okay computer", "do it send it"],
        cloud="okay computer do it send it")

    assert sent == ["do it"]
    assert actions[:1] == ["wake"]


def test_personalized_near_miss_is_logged_with_score_and_threshold(
        tmp_path, monkeypatch):
    logs = []
    monkeypatch.setattr(listen.triggers, "enrolled", lambda: True)
    monkeypatch.setattr(
        listen.triggers, "match", lambda frames, allowed: (None, 0.36, 0.30))
    _run_listener(tmp_path, monkeypatch, [([b"x"], False)], ["hello"],
                  logs=logs)

    assert any(line.startswith("personalized near-miss score=0.360 "
                               "threshold=0.300") for line in logs)


def test_own_playback_saying_the_wake_phrase_does_not_wake(
        tmp_path, monkeypatch):
    listen.echo.remember(
        "To start, say okay computer, then give your request.")
    logs = []
    sent, actions, _ = _run_listener(
        tmp_path, monkeypatch, [([b"echo"], True)],
        ["say okay computer. give your request."], speaking=True, logs=logs)

    assert actions == []
    assert "ignored wake phrase echoed from playback" in logs


def test_wake_over_unrelated_playback_still_barges_in(tmp_path, monkeypatch):
    listen.echo.remember("The tests passed and the branch is ready.")
    _, actions, _ = _run_listener(
        tmp_path, monkeypatch, [([b"wake"], True)], ["okay computer"],
        speaking=True)

    assert actions == ["wake"]


def test_stop_talking_still_works_over_a_reply_that_mentions_the_wake(
        tmp_path, monkeypatch):
    listen.echo.remember("Say okay computer whenever you want me.")
    _, actions, _ = _run_listener(
        tmp_path, monkeypatch, [([b"stop"], True)],
        ["okay computer stop talking"], speaking=True)

    assert actions == ["skip", "stop"]


def test_reply_reading_out_the_stop_command_does_not_stop_itself(
        tmp_path, monkeypatch):
    listen.echo.remember(
        "To interrupt me, say okay computer stop talking at any time.")
    _, actions, _ = _run_listener(
        tmp_path, monkeypatch, [([b"echo"], True)],
        ["say okay computer stop talking at any time"], speaking=True)

    assert "skip" not in actions


def test_cloud_transcript_that_drops_the_last_sentence_is_repaired(
        tmp_path, monkeypatch):
    """2026-11-01 21:11:53: "check on it" was lost before "send it"."""
    sent, _, _ = _run_listener(
        tmp_path, monkeypatch,
        [([b"wake"], False), ([b"message"], False)],
        ["okay computer.", "so I just did something now. check on it. send it."],
        cloud="Okay computer, so I just did something now. Send it.")

    assert sent == ["so I just did something now. Check on it"]


@pytest.mark.parametrize("cloud, local, expected", [
    # Agreement: nothing to restore.
    ("Run the tests.", "run the tests send it", "Run the tests."),
    # A one-word wording difference is the cloud being more accurate.
    ("Run the tests now.", "run the test now send it", "Run the tests now."),
    # Cloud has more than local after the anchor: trust the cloud.
    ("Fix it and then deploy to staging.", "fix it and then send it",
     "Fix it and then deploy to staging."),
    # A clear dropped run is restored.
    ("Open the file", "open the file and read the header send it",
     "Open the file. And read the header"),
])
def test_dropped_tail_repair_is_conservative(cloud, local, expected):
    assert listen.repair_dropped_tail(cloud, local) == expected
