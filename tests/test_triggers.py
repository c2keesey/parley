import os

import numpy as np
import pytest

from parley import config, triggers


def phrase(frequencies, rate=1.0, noise=0.0):
    pieces = []
    for frequency in frequencies:
        duration = int(0.18 * triggers.RATE * rate)
        time = np.arange(duration) / triggers.RATE
        envelope = np.sin(np.linspace(0, np.pi, duration))
        pieces.append(0.55 * envelope * np.sin(2 * np.pi * frequency * time))
        pieces.append(np.zeros(int(0.04 * triggers.RATE * rate)))
    samples = np.concatenate(pieces)
    if noise:
        samples += np.random.default_rng(7).normal(0, noise, len(samples))
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def test_feature_distance_tolerates_pace_but_rejects_different_phrase():
    reference = triggers.features(phrase([220, 330, 440]))
    paced = triggers.features(phrase([220, 330, 440], rate=1.12, noise=0.004))
    different = triggers.features(phrase([440, 220, 620]))

    assert triggers.distance(reference, paced) < triggers.distance(reference, different)


def test_profile_saves_features_not_raw_recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    samples = {
        name: [
            phrase([base, base + 100, base + 180], rate=rate)
            for rate in (0.92, 1.0, 1.08)
        ]
        for name, base in zip(triggers.PHRASES, (210, 360, 510, 660))
    }

    metadata = triggers.save(samples)

    assert triggers.enrolled()
    assert set(metadata["thresholds"]) == set(triggers.PHRASES)
    assert not list(config.TRIGGERS.glob("*.wav"))
    assert not list(config.TRIGGERS.glob("*.pcm"))
    assert os.stat(config.TRIGGERS).st_mode & 0o777 == 0o700
    assert all(
        os.stat(path).st_mode & 0o777 == 0o600
        for path in config.TRIGGERS.iterdir()
    )


def test_send_matches_only_where_it_is_said_at_the_end_of_a_burst(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    send_samples = [
        phrase([330, 440, 550], rate=rate)
        for rate in (0.92, 1.0, 1.08)
    ]
    samples = {
        name: send_samples if name == "send" else [
            phrase([base, base + 80], rate=rate)
            for rate in (0.92, 1.0, 1.08)
        ]
        for name, base in zip(triggers.PHRASES, (210, 330, 510, 660))
    }
    triggers.save(samples)

    matched, score, threshold = triggers.match(
        phrase([330, 440, 550], rate=1.03, noise=0.002), ["send"])
    # Like the trailing-phrase rule for Whisper text: dictation that ends in
    # the send phrase is a send; the phrase in the middle of speech is not.
    trailing = phrase([180, 260, 200, 240, 300, 330, 440, 550])
    embedded = phrase([330, 440, 550, 180, 260, 200, 240, 300])
    hit, _, _ = triggers.match(trailing, ["send"])
    rejected, _, _ = triggers.match(embedded, ["send"])

    assert matched == "send"
    assert score <= threshold
    assert hit == "send"
    assert rejected is None


def _enroll(tmp_path, monkeypatch, bases=(210, 360, 510, 660)):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    samples = {
        name: [phrase([base, base + 100, base + 180], rate=rate)
               for rate in (0.92, 1.0, 1.08)]
        for name, base in zip(triggers.PHRASES, bases)
    }
    return triggers.save(samples)


def naive_subsequence(template, candidate, min_span, max_span):
    rows, columns = len(template), len(candidate)
    cost = 1 - template @ candidate.T
    total = np.full((rows, columns), np.inf)
    start = np.zeros((rows, columns), dtype=int)
    for c in range(columns):
        total[0, c], start[0, c] = cost[0, c], c
    for r in range(1, rows):
        for c in range(columns):
            options = [(total[r - 1, c], start[r - 1, c])]
            if c:
                options += [(total[r - 1, c - 1], start[r - 1, c - 1]),
                            (total[r, c - 1], start[r, c - 1])]
            best, origin = min(options, key=lambda item: item[0])
            total[r, c], start[r, c] = cost[r, c] + best, origin
    scores = [
        total[rows - 1, c] / max(rows, c - start[rows - 1, c] + 1)
        for c in range(columns)
        if rows * min_span <= c - start[rows - 1, c] + 1 <= rows * max_span
    ]
    return min(scores) if scores else float("inf")


def test_vectorized_subsequence_dtw_matches_the_textbook_recurrence():
    rng = np.random.default_rng(3)
    for rows, columns in [(6, 15), (10, 12), (4, 30)]:
        template = rng.normal(size=(rows, 5))
        candidate = rng.normal(size=(columns, 5))
        template /= np.linalg.norm(template, axis=1, keepdims=True)
        candidate /= np.linalg.norm(candidate, axis=1, keepdims=True)
        expected = naive_subsequence(template, candidate, 0.6, 1.8)
        actual = triggers.subsequence_distance(template, candidate)
        assert actual == pytest.approx(expected, rel=1e-5)


def test_wake_matches_at_the_start_of_a_longer_burst(tmp_path, monkeypatch):
    """A live wake burst is "okay computer, <the request>" — v1 never matched it."""
    _enroll(tmp_path, monkeypatch)
    wake_then_request = phrase([210, 310, 390, 800, 900, 700, 650, 950, 720])

    matched, score, threshold = triggers.match(wake_then_request, ["wake"])

    assert matched == "wake", (score, threshold)


def test_quiet_microphone_level_does_not_change_the_match(tmp_path, monkeypatch):
    """v1 trimmed with an absolute level that ate quiet speech."""
    _enroll(tmp_path, monkeypatch)
    loud = np.frombuffer(phrase([210, 310, 390], rate=1.03), "<i2")
    quiet = (loud.astype(np.float64) * 0.02).astype("<i2").tobytes()

    matched, _, _ = triggers.match(quiet, ["wake"])

    assert matched == "wake"


def test_stop_wins_over_the_wake_phrase_it_starts_with(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    wake = [210, 310, 390]
    stop = wake + [700, 820, 640]
    samples = {
        "wake": [phrase(wake, rate=r) for r in (0.92, 1.0, 1.08)],
        "stop": [phrase(stop, rate=r) for r in (0.92, 1.0, 1.08)],
        "send": [phrase([500, 560, 620], rate=r) for r in (0.92, 1.0, 1.08)],
        "cancel": [phrase([900, 760, 980], rate=r) for r in (0.92, 1.0, 1.08)],
    }
    triggers.save(samples)

    matched, _, _ = triggers.match(phrase(stop, rate=1.04), ["wake", "stop"])

    assert matched == "stop"


def test_enrollment_rejects_implausible_samples(tmp_path, monkeypatch):
    """One v1 'scratch that' template was 14.5 seconds of audio."""
    long_audio = phrase([300] * 80)
    vector, reason = triggers.usable(long_audio)
    assert vector is None and reason.startswith("too long")
    vector, reason = triggers.usable(b"\x00\x00" * 8000)
    assert vector is None and reason == "no speech"


def test_thresholds_stay_below_other_phrases(tmp_path, monkeypatch):
    _enroll(tmp_path, monkeypatch)
    templates, thresholds = triggers.load()
    for name, own in templates.items():
        related = {"wake", "stop"} if name in {"wake", "stop"} else {name}
        for other, others in templates.items():
            if other in related:
                continue
            for vector in own:
                for impostor in others:
                    assert (triggers.subsequence_distance(vector, impostor)
                            > thresholds[name])


def test_version_one_profiles_are_ignored_as_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    config.TRIGGERS.mkdir()
    (config.TRIGGERS / "profile.json").write_text(
        '{"version": 1, "thresholds": {"wake": 0.42}}')
    for name in triggers.PHRASES:
        for index in range(3):
            np.save(config.TRIGGERS / f"{name}-{index}.npy",
                    np.ones((20, triggers.WIDTH), dtype=np.float32))

    assert not triggers.enrolled()
    assert triggers.stale()
