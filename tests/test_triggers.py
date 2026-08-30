import os
import stat

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


def profile(offset=0, rates=(0.92, 1.0, 1.08)):
    return {
        name: [
            phrase([base + offset, base + offset + 100, base + offset + 180],
                   rate=rate)
            for rate in rates
        ]
        for name, base in zip(triggers.PHRASES, (210, 360, 510, 660))
    }


def profile_snapshot():
    templates, thresholds = triggers.load()
    return {
        name: [template.copy() for template in templates[name]]
        for name in triggers.PHRASES
    }, thresholds.copy()


def assert_profile_equal(actual, expected):
    actual_templates, actual_thresholds = actual
    expected_templates, expected_thresholds = expected
    assert actual_thresholds == expected_thresholds
    for name in triggers.PHRASES:
        assert len(actual_templates[name]) == len(expected_templates[name])
        for actual_template, expected_template in zip(
                actual_templates[name], expected_templates[name]):
            np.testing.assert_array_equal(actual_template, expected_template)


def test_feature_distance_tolerates_pace_but_rejects_different_phrase():
    reference = triggers.features(phrase([220, 330, 440]))
    paced = triggers.features(phrase([220, 330, 440], rate=1.12, noise=0.004))
    different = triggers.features(phrase([440, 220, 620]))

    assert triggers.distance(reference, paced) < triggers.distance(reference, different)


def test_profile_saves_features_not_raw_recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()

    metadata = triggers.save(profile())

    assert triggers.enrolled()
    assert set(metadata["thresholds"]) == set(triggers.PHRASES)
    assert not list(config.TRIGGERS.rglob("*.wav"))
    assert not list(config.TRIGGERS.rglob("*.pcm"))
    for directory, _, filenames in os.walk(config.TRIGGERS):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        assert all(
            stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode) == 0o600
            for name in filenames
        )


def test_validation_failure_preserves_last_good_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    triggers.save(profile())
    before = profile_snapshot()
    marker = (config.TRIGGERS / triggers.PROFILE_MARKER).read_text()
    invalid = profile(offset=25)
    invalid["cancel"] = [b"\0\0"]

    with pytest.raises(ValueError, match="not enough usable cancel recordings"):
        triggers.save(invalid)

    assert (config.TRIGGERS / triggers.PROFILE_MARKER).read_text() == marker
    assert_profile_equal(profile_snapshot(), before)
    assert not list(config.TRIGGERS.glob(f"{triggers.STAGING_PREFIX}*"))


def test_mid_write_failure_preserves_last_good_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    triggers.save(profile())
    before = profile_snapshot()
    marker = (config.TRIGGERS / triggers.PROFILE_MARKER).read_text()
    real_save = triggers.np.save
    writes = 0

    def fail_on_third_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("injected staged profile write failure")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(triggers.np, "save", fail_on_third_write)
    with pytest.raises(OSError, match="injected staged profile write failure"):
        triggers.save(profile(offset=25))

    assert (config.TRIGGERS / triggers.PROFILE_MARKER).read_text() == marker
    assert_profile_equal(profile_snapshot(), before)
    assert not list(config.TRIGGERS.glob(f"{triggers.STAGING_PREFIX}*"))


def test_marker_commit_failure_preserves_last_good_profile(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    triggers.save(profile())
    before = profile_snapshot()
    marker = (config.TRIGGERS / triggers.PROFILE_MARKER).read_text()
    real_replace = triggers.os.replace

    def fail_marker_commit(source, destination):
        if destination == config.TRIGGERS / triggers.PROFILE_MARKER:
            raise OSError("injected atomic marker failure")
        return real_replace(source, destination)

    monkeypatch.setattr(triggers.os, "replace", fail_marker_commit)
    with pytest.raises(OSError, match="injected atomic marker failure"):
        triggers.save(profile(offset=25))

    assert (config.TRIGGERS / triggers.PROFILE_MARKER).read_text() == marker
    assert_profile_equal(profile_snapshot(), before)
    assert len(list(config.TRIGGERS.glob(
        f"{triggers.PROFILE_PREFIX}*"))) == 2


def test_success_atomically_replaces_and_cleans_old_profile(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRIGGERS", tmp_path / "triggers")
    triggers.load.cache_clear()
    triggers.save(profile())
    before, _ = profile_snapshot()
    old_generation = (
        config.TRIGGERS / triggers.PROFILE_MARKER).read_text().strip()

    triggers.save(profile(offset=25))

    after, _ = profile_snapshot()
    new_generation = (
        config.TRIGGERS / triggers.PROFILE_MARKER).read_text().strip()
    assert new_generation != old_generation
    assert not (config.TRIGGERS / old_generation).exists()
    assert [path.name for path in config.TRIGGERS.glob(
        f"{triggers.PROFILE_PREFIX}*")] == [new_generation]
    assert not list(config.TRIGGERS.glob("*.npy"))
    assert not (config.TRIGGERS / "profile.json").exists()
    assert all(
        not np.array_equal(before[name][0], after[name][0])
        for name in triggers.PHRASES
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
