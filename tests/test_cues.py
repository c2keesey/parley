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
