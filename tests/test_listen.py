import pytest

from parley.listen import contains_phrase, rms, strip_phrase


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


@pytest.mark.parametrize("heard", ["scrap that", "Scrap that.", "okay scrap that"])
def test_cancel_phrase_is_recognised(heard):
    from parley.listen import CANCEL
    assert contains_phrase(heard, CANCEL)


@pytest.mark.parametrize("heard", [
    "cancel it",
    "stop",
    "scrap the old migration and start over",
    "that scrap heap of a function",
])
def test_ordinary_dictation_does_not_cancel(heard):
    """A discard phrase that fires by accident is worse than none."""
    from parley.listen import CANCEL
    assert not contains_phrase(heard, CANCEL)


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


def test_strip_leading_removes_anything_said_before_the_wake_phrase():
    """After barge-in the burst can contain the agent's speech; everything up
    to and including the wake phrase is dropped."""
    from parley.listen import WAKE, strip_leading
    heard = "the tests are green and I pushed it okay computer stop and check the log"
    assert strip_leading(heard, WAKE) == "stop and check the log"
