import wave

import pytest

from parley import config, cues


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "PIDFILE", tmp_path / "playing.pid")


@pytest.mark.parametrize("name", sorted(cues.PATTERNS))
def test_every_cue_renders_playable_audio(name):
    path = cues.build(name)
    with wave.open(str(path)) as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == cues.RATE
        assert handle.getnframes() > 0


@pytest.mark.parametrize("name", sorted(cues.PATTERNS))
def test_cues_stay_short(name):
    path = cues.build(name)
    with wave.open(str(path)) as handle:
        seconds = handle.getnframes() / handle.getframerate()
    assert seconds < 0.6, "a cue that outstays its welcome is worse than none"


def test_cues_are_cached_not_regenerated():
    first = cues.build("wake")
    stamp = first.stat().st_mtime_ns
    assert cues.build("wake").stat().st_mtime_ns == stamp


def test_edges_are_faded_so_there_is_no_click():
    path = cues.build("wake")
    with wave.open(str(path)) as handle:
        frames = handle.readframes(handle.getnframes())
    import struct

    samples = struct.unpack(f"{len(frames) // 2}h", frames)
    assert abs(samples[0]) < 500
    assert abs(samples[-1]) < 500


def test_unknown_cue_is_ignored():
    assert cues.build("nonsense") is None
    cues.play("nonsense")


def test_rebuild_replaces_cached_files():
    path = cues.build("done")
    stamp = path.stat().st_mtime_ns
    cues.rebuild()
    assert path.stat().st_mtime_ns != stamp


def test_no_partial_lands_in_the_harsh_band():
    """3-6kHz is the band that reads as harsh; the first attempt put a third
    harmonic straight into it, which is why it sounded unpleasant."""
    for name, pattern in cues.PATTERNS.items():
        for frequency, _ in pattern:
            for multiple, _gain in cues.HARMONICS:
                partial = frequency * multiple
                assert not 3000 <= partial <= 6000, f"{name} has {partial:.0f}Hz"


def test_fundamentals_sit_in_the_earcon_band():
    for name, pattern in cues.PATTERNS.items():
        for frequency, _ in pattern:
            assert 300 <= frequency <= 1000, f"{name} fundamental {frequency}"


def test_cues_are_brief():
    for name, pattern in cues.PATTERNS.items():
        total = sum(seconds for _, seconds in pattern)
        assert total <= 0.25, f"{name} runs {total:.2f}s"


def test_cues_share_a_peak_level():
    peaks = []
    for name in cues.PATTERNS:
        path = cues.build(name)
        with wave.open(str(path)) as handle:
            frames = handle.readframes(handle.getnframes())
        import struct as _struct
        samples = _struct.unpack(f"{len(frames) // 2}h", frames)
        peaks.append(max(abs(s) for s in samples))
    assert max(peaks) - min(peaks) <= 2, "one cue would jump out over the others"


def test_a_sound_file_can_replace_a_generated_cue(tmp_path, monkeypatch):
    custom = tmp_path / "mine.wav"
    with wave.open(str(custom), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(cues.RATE)
        handle.writeframes(b"\x00\x00" * 100)
    monkeypatch.setenv("PARLEY_CUE_WAKE", str(custom))
    assert cues.override("wake") == str(custom)


def test_a_missing_override_falls_back_to_the_generated_cue(monkeypatch):
    monkeypatch.setenv("PARLEY_CUE_WAKE", "/nope/does-not-exist.wav")
    assert cues.override("wake") is None


@pytest.mark.parametrize("name", ["wake", "send"])
def test_custom_bundled_earcons_match_the_current_patterns(name):
    generated = cues.build(name)
    shipped = cues.SOUNDS / f"{name}.wav"
    assert shipped.read_bytes() == generated.read_bytes()
