import os

import numpy as np

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


def test_personalized_match_is_bounded_to_command_sized_audio(
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
    ordinary = phrase([180] * 12 + [330, 440, 550])
    rejected, _, _ = triggers.match(ordinary, ["send"])

    assert matched == "send"
    assert score <= threshold
    assert rejected is None
