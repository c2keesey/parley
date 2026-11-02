import numpy as np
import pytest

from parley import gate


def tone(level, seconds, seed=0):
    """Noise-like frames at an exact RMS level."""
    count = max(1, round(seconds / gate.FRAME_SECONDS))
    rng = np.random.default_rng(seed)
    frames = []
    for _ in range(count):
        samples = rng.normal(0, 1, gate.FRAME)
        samples *= level / np.sqrt(np.mean(samples ** 2))
        frames.append(np.clip(samples, -32768, 32767).astype("<i2").tobytes())
    return frames


def run(speech_gate, frames, playing=False):
    events = []
    for frame in frames:
        event, burst = speech_gate.feed(frame, playing)
        if event:
            events.append((event, burst))
    return events


def make(**overrides):
    options = {"vad": None, "min_rms": 60}
    options.update(overrides)
    return gate.SpeechGate(**options)


def test_quiet_speech_opens_once_the_floor_has_learned_the_room():
    """Speech at RMS 200 never crossed the old fixed 500 threshold."""
    speech_gate = make()
    run(speech_gate, tone(20, 2.0))
    assert speech_gate.floor == pytest.approx(20, rel=0.25)

    events = run(speech_gate, tone(200, 0.6) + tone(20, 1.0))

    kinds = [event for event, _ in events]
    assert kinds == ["start", "end"]
    assert events[-1][1].voiced == pytest.approx(0.6, abs=0.07)


def test_floor_follows_a_louder_room_so_noise_alone_stops_opening():
    speech_gate = make()
    run(speech_gate, tone(20, 1.0))
    # A fan switches on. It may open one burst, but the floor catches up.
    run(speech_gate, tone(150, 20.0, seed=3))
    assert speech_gate.floor > 100

    events = run(speech_gate, tone(150, 5.0, seed=4))

    assert [event for event, _ in events] == []


def test_floor_does_not_absorb_ordinary_speech():
    speech_gate = make()
    run(speech_gate, tone(25, 2.0))
    run(speech_gate, tone(400, 2.0) + tone(25, 1.0))
    assert speech_gate.floor < 60


def test_preroll_keeps_the_soft_onset_before_the_gate_opened():
    speech_gate = make(preroll=0.3)
    run(speech_gate, tone(20, 1.0))
    soft = tone(45, 0.15, seed=5)            # a soft "s", below the open level
    events = run(speech_gate, soft + tone(300, 0.4) + tone(20, 1.0))

    burst = events[-1][1]
    assert all(frame in burst for frame in soft)


def test_hysteresis_keeps_a_soft_middle_inside_one_burst():
    speech_gate = make()
    run(speech_gate, tone(20, 1.0))
    # Between words the level dips below the opening level but stays above
    # the stay-open level, and briefly to silence: still one utterance.
    utterance = (tone(300, 0.3) + tone(45, 0.3) + tone(20, 0.3)
                 + tone(300, 0.3))
    events = run(speech_gate, utterance + tone(20, 1.0))

    assert [event for event, _ in events] == ["start", "end"]


def test_single_click_never_opens_but_two_voiced_frames_do():
    speech_gate = make()
    run(speech_gate, tone(20, 1.0))
    assert run(speech_gate, tone(3000, gate.FRAME_SECONDS) + tone(20, 1.0)) == []

    events = run(speech_gate, tone(3000, 2 * gate.FRAME_SECONDS) + tone(20, 1.0))
    assert [event for event, _ in events] == ["start", "short"]
    assert events[-1][1].short


def test_vad_rejection_keeps_loud_non_speech_out():
    speech_gate = make(vad=lambda frame: False)
    run(speech_gate, tone(20, 1.0))
    assert run(speech_gate, tone(2000, 1.0) + tone(20, 1.0)) == []


def test_playback_raises_the_opening_bar():
    speech_gate = make(playing_ratio=2.0)
    run(speech_gate, tone(30, 1.0))
    level = speech_gate.open_level() * 1.3

    assert run(speech_gate, tone(level, 0.4) + tone(30, 1.0), playing=True) == []
    events = run(speech_gate, tone(level, 0.4) + tone(30, 1.0))
    assert [event for event, _ in events] == ["start", "end"]


def test_absolute_minimum_still_applies_in_a_silent_room():
    speech_gate = make(min_rms=80)
    run(speech_gate, tone(2, 2.0))
    assert speech_gate.open_level() == 80
    assert run(speech_gate, tone(40, 0.5) + tone(2, 1.0)) == []


def test_webrtc_vad_is_available_and_rejects_silence():
    vad = gate.default_vad()
    if vad is None:
        pytest.skip("webrtcvad not installed")
    assert vad(b"\x00\x00" * gate.FRAME) is False
