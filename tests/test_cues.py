import wave

import pytest

from parley import config, cues, processes


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "PIDFILE", tmp_path / "playing.pid")
    monkeypatch.setattr(config, "CUE_PROCESSES", tmp_path / "cue-processes")
    monkeypatch.setattr(config, "LOG", tmp_path / "speak.log")
    monkeypatch.setattr(
        processes, "process_identity", lambda pid: f"test-birth:{pid}"
    )


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


def test_cancel_uses_distinct_generated_falling_tone():
    assert cues.bundled("cancel") is None
    assert cues.build("cancel") == config.STATE / "cue-cancel.wav"


def test_stop_uses_a_distinct_generated_two_tap_tone():
    assert cues.bundled("stop") is None
    assert cues.PATTERNS["stop"] != cues.PATTERNS["cancel"]
    assert cues.PATTERNS["stop"] != cues.PATTERNS["done"]
    assert cues.build("stop") == config.STATE / "cue-stop.wav"


def test_play_logs_semantic_cue_name_and_safe_source(monkeypatch):
    class Process:
        pid = 4242

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(cues.subprocess, "Popen", lambda *args, **kwargs: Process())

    cues.play("wake")
    cues.play("cancel")

    log = config.LOG.read_text()
    assert "cue wake source=bundled wait=true" in log
    assert "cue cancel source=generated wait=true" in log


def test_completed_cue_releases_process_ownership(monkeypatch):
    owned_while_playing = []

    class Process:
        pid = 4242

        def wait(self, timeout=None):
            owned_while_playing.extend(
                processes.owned_pids(config.CUE_PROCESSES, "cue")
            )
            return 0

    monkeypatch.setattr(cues.subprocess, "Popen", lambda *args, **kwargs: Process())

    cues.play("wake")

    assert owned_while_playing == [4242]
    assert processes.owned_pids(config.CUE_PROCESSES, "cue") == []


def test_finish_kills_reaps_and_cleans_marker_after_two_timeouts():
    events = []

    class StubbornProcess:
        pid = 4242

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            assert processes.owned_pids(config.CUE_PROCESSES, "cue") == [self.pid]
            if timeout is not None:
                raise cues.subprocess.TimeoutExpired("afplay", timeout)
            events.append(("reaped", None))
            return -9

        def terminate(self):
            events.append(("terminate", None))

        def kill(self):
            events.append(("kill", None))

    ownership = processes.claim_in(config.CUE_PROCESSES, 4242, "cue")
    assert ownership is not None

    cues._finish(StubbornProcess(), ownership)

    assert events == [
        ("wait", 5),
        ("terminate", None),
        ("wait", 1),
        ("kill", None),
        ("wait", None),
        ("reaped", None),
    ]
    assert processes.owned_pids(config.CUE_PROCESSES, "cue") == []


def test_finish_reaps_after_process_exits_during_kill_race():
    waits = []

    class ExitedProcess:
        pid = 4242

        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is not None:
                raise cues.subprocess.TimeoutExpired("afplay", timeout)
            return 0

        def terminate(self):
            pass

        def kill(self):
            raise ProcessLookupError

    ownership = processes.claim_in(config.CUE_PROCESSES, 4242, "cue")
    assert ownership is not None

    cues._finish(ExitedProcess(), ownership)

    assert waits == [5, 1, None]
    assert processes.owned_pids(config.CUE_PROCESSES, "cue") == []


@pytest.mark.parametrize("name", ["wake", "send"])
def test_custom_bundled_earcons_match_the_current_patterns(name):
    generated = cues.build(name)
    shipped = cues.SOUNDS / f"{name}.wav"
    assert shipped.read_bytes() == generated.read_bytes()
