import numpy as np

from parley import cli, config, echo, evaluate, gate

WAKE = [("okay", "computer"), ("ok", "computer")]


def test_recent_texts_cover_playing_and_just_finished_blocks(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    echo.remember("first block", now=100.0)
    echo.finished(now=110.0)
    echo.remember("second block", now=111.0)

    assert echo.recent_texts(now=112.0) == ["first block", "second block"]
    echo.finished(now=113.0)
    assert echo.recent_texts(now=200.0) == []


def test_echo_needs_the_trigger_in_what_parley_said():
    spoken = ["When you're ready, say okay computer and then your request."]

    assert echo.is_echo("say okay computer and then", WAKE, spoken)
    assert echo.is_echo("Okay, computer.", WAKE, spoken)
    assert not echo.is_echo("okay computer", WAKE, ["The build passed."])
    # Your own words alongside the trigger are not the reply's words.
    assert not echo.is_echo(
        "okay computer open the pull request", WAKE, spoken)


def test_spoken_text_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    echo.remember("private reply")
    assert (tmp_path / "spoken-recent.json").stat().st_mode & 0o777 == 0o600


def test_burst_labels_come_from_the_file_name():
    assert evaluate.label_of("20261101-211147-123456-wake.wav") == "wake"
    assert evaluate.label_of("x-send.wav") == "send"
    assert evaluate.label_of("x-dictation.wav") == "none"


def test_replayed_quiet_speech_passes_new_gate_but_not_the_old_one(monkeypatch):
    """The regression in numbers: speech at RMS ~180 in a room at RMS ~25."""
    monkeypatch.setattr(gate, "default_vad", lambda: None)
    rng = np.random.default_rng(1)
    seconds = 0.8
    t = np.arange(int(gate.RATE * seconds)) / gate.RATE
    envelope = np.sin(np.pi * t / seconds) ** 0.5
    voice = envelope * rng.normal(0, 1, len(t))
    voice *= 180 / np.sqrt(np.mean(voice ** 2))

    bursts = [b for b in evaluate.replay_gate(voice) if not b.short]

    assert len(bursts) == 1
    assert not evaluate.legacy_gate_passes(voice)


def test_cli_exposes_meter_and_debug_triggers():
    parser = cli.build_parser()
    assert parser.parse_args(["listen", "meter", "--seconds", "3"]).seconds == 3
    args = parser.parse_args(["debug-triggers", "--synthetic", "--whisper"])
    assert args.synthetic and args.whisper
