import pytest

from parley import listen
from parley.listen import contains_phrase, is_cancel, is_stop_talking, rms, strip_phrase


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


def test_send_phrase_is_stripped_from_the_message():
    assert strip_phrase("run the tests send it", "send it") == "run the tests"
    assert strip_phrase("Run the tests, send it.", "send it") == "Run the tests"


def test_send_phrase_is_only_stripped_from_the_end():
    assert strip_phrase("send it to staging", "send it") == "send it to staging"


def test_silence_reads_as_silence():
    assert rms(b"\x00\x00" * 512) == 0


def test_loud_audio_reads_as_loud():
    assert rms(b"\xff\x7f" * 512) > 30000


def test_short_chunk_is_not_an_error():
    assert rms(b"\x00") == 0


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


def test_stop_talking_never_transcribes_cues_or_sends(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        listen, "cue", lambda kind: pytest.fail("must not chime"))
    monkeypatch.setattr(listen.player, "stop", lambda: actions.append("stopped"))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    listen.run()

    assert actions == ["stopped"]


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


def test_strip_leading_removes_anything_said_before_the_wake_phrase():
    """After barge-in the burst can contain the agent's speech; everything up
    to and including the wake phrase is dropped."""
    from parley.listen import WAKE, strip_leading
    heard = "the tests are green and I pushed it okay computer stop and check the log"
    assert strip_leading(heard, WAKE) == "stop and check the log"
