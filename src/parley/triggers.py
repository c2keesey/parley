"""Speaker-personalized trigger matching over local acoustic features.

Profiles contain normalized MFCC-like feature matrices, never raw recordings.
Dynamic time warping makes comparisons tolerant of natural changes in pace.
"""
import json
import math
import os
import subprocess
import time
from functools import lru_cache

import numpy as np

from parley import config

RATE = 16000
FRAME_LENGTH = 400
HOP = 160
FFT_SIZE = 512
FILTERS = 32
COEFFICIENTS = 16
PHRASES = {
    "wake": "okay computer",
    "send": "send it",
    "cancel": "scratch that",
    "stop": "okay computer stop talking",
}
SAMPLES = {"wake": 6, "send": 7, "cancel": 5, "stop": 5}
DEFAULT_THRESHOLDS = {
    "wake": 0.34,
    "send": 0.27,
    "cancel": 0.29,
    "stop": 0.31,
}


def _frames(samples):
    if len(samples) < FRAME_LENGTH:
        return np.empty((0, FRAME_LENGTH), dtype=np.float32)
    count = 1 + (len(samples) - FRAME_LENGTH) // HOP
    shape = (count, FRAME_LENGTH)
    strides = (samples.strides[0] * HOP, samples.strides[0])
    return np.lib.stride_tricks.as_strided(
        samples, shape=shape, strides=strides).copy()


def _mel_filters():
    low = 2595 * math.log10(1 + 80 / 700)
    high = 2595 * math.log10(1 + (RATE / 2) / 700)
    mel = np.linspace(low, high, FILTERS + 2)
    hz = 700 * (10 ** (mel / 2595) - 1)
    bins = np.floor((FFT_SIZE + 1) * hz / RATE).astype(int)
    bank = np.zeros((FILTERS, FFT_SIZE // 2 + 1), dtype=np.float32)
    for index in range(FILTERS):
        left, middle, right = bins[index:index + 3]
        if middle > left:
            bank[index, left:middle] = (
                np.arange(left, middle) - left) / (middle - left)
        if right > middle:
            bank[index, middle:right] = (
                right - np.arange(middle, right)) / (right - middle)
    return bank


MEL_FILTERS = _mel_filters()
DCT = np.cos(
    np.pi / FILTERS
    * (np.arange(FILTERS) + 0.5)[None, :]
    * np.arange(COEFFICIENTS)[:, None]
).astype(np.float32)


def features(audio):
    """Return time-normalized acoustic features for 16 kHz signed PCM."""
    if isinstance(audio, (list, tuple)):
        audio = b"".join(audio)
    samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768
    framed = _frames(samples)
    if not len(framed):
        return np.empty((0, (COEFFICIENTS - 1) * 2), dtype=np.float32)

    energy = np.sqrt(np.mean(framed * framed, axis=1) + 1e-10)
    threshold = max(0.008, float(energy.max()) * 0.12)
    voiced = np.flatnonzero(energy >= threshold)
    if not len(voiced):
        return np.empty((0, (COEFFICIENTS - 1) * 2), dtype=np.float32)
    start = max(0, int(voiced[0]) - 3)
    end = min(len(framed), int(voiced[-1]) + 4)
    framed = framed[start:end]

    framed *= np.hanning(FRAME_LENGTH).astype(np.float32)
    power = np.abs(np.fft.rfft(framed, FFT_SIZE)) ** 2
    mel = np.maximum(power @ MEL_FILTERS.T, 1e-10)
    cepstra = np.log(mel) @ DCT.T
    cepstra = cepstra[:, 1:]
    cepstra -= cepstra.mean(axis=0, keepdims=True)
    scale = cepstra.std(axis=0, keepdims=True)
    cepstra /= np.maximum(scale, 0.1)
    delta = np.gradient(cepstra, axis=0) if len(cepstra) > 1 else np.zeros_like(cepstra)
    result = np.concatenate((cepstra, delta), axis=1).astype(np.float32)
    norms = np.linalg.norm(result, axis=1, keepdims=True)
    return result / np.maximum(norms, 1e-6)


def distance(left, right):
    """Length-normalized dynamic-time-warping cosine distance."""
    if not len(left) or not len(right):
        return float("inf")
    if not 0.5 <= len(left) / len(right) <= 2.0:
        return float("inf")
    rows, columns = len(left), len(right)
    previous = np.full(columns + 1, np.inf, dtype=np.float32)
    previous[0] = 0
    band = max(abs(rows - columns) + 2, int(max(rows, columns) * 0.35))
    for row in range(1, rows + 1):
        current = np.full(columns + 1, np.inf, dtype=np.float32)
        low, high = max(1, row - band), min(columns, row + band)
        costs = 1 - right[low - 1:high] @ left[row - 1]
        for column, cost in zip(range(low, high + 1), costs):
            current[column] = cost + min(
                current[column - 1], previous[column], previous[column - 1])
        previous = current
    return float(previous[columns] / max(rows, columns))


def _profile_path(name, index):
    return config.TRIGGERS / f"{name}-{index}.npy"


def save(samples):
    """Persist local feature templates and calibrated positive thresholds."""
    config.TRIGGERS.mkdir(parents=True, exist_ok=True)
    os.chmod(config.TRIGGERS, 0o700)
    for old_template in config.TRIGGERS.glob("*.npy"):
        old_template.unlink()
    metadata = {"version": 1, "thresholds": {}}
    for name, recordings in samples.items():
        vectors = [features(recording) for recording in recordings]
        vectors = [vector for vector in vectors if len(vector)]
        if len(vectors) < 3:
            raise ValueError(f"not enough usable {name} recordings")
        nearest = []
        for index, vector in enumerate(vectors):
            nearest.append(min(
                distance(vector, other)
                for other_index, other in enumerate(vectors)
                if other_index != index
            ))
        observed = float(np.percentile(nearest, 90))
        default = DEFAULT_THRESHOLDS[name]
        metadata["thresholds"][name] = round(
            min(default + 0.08, max(default, observed * 1.22)), 4)
        for index, vector in enumerate(vectors):
            path = _profile_path(name, index)
            np.save(path, vector, allow_pickle=False)
            os.chmod(path, 0o600)
    metadata_path = config.TRIGGERS / "profile.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    os.chmod(metadata_path, 0o600)
    load.cache_clear()
    return metadata


@lru_cache(maxsize=1)
def load():
    """Load the profile, returning empty data when enrollment is absent."""
    metadata_path = config.TRIGGERS / "profile.json"
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, ValueError):
        return {}, {}
    templates = {}
    for name in PHRASES:
        paths = sorted(config.TRIGGERS.glob(f"{name}-*.npy"))
        try:
            templates[name] = [np.load(path, allow_pickle=False) for path in paths]
        except (OSError, ValueError):
            templates[name] = []
    return templates, metadata.get("thresholds", {})


def enrolled():
    templates, _ = load()
    return all(len(templates.get(name, [])) >= 3 for name in PHRASES)


def match(audio, allowed=None):
    """Return (trigger, score, threshold), or (None, best score, threshold)."""
    templates, thresholds = load()
    candidate = features(audio)
    choices = tuple(allowed or PHRASES)
    best_name, best_score, best_threshold = None, float("inf"), None
    for name in choices:
        enrolled_templates = templates.get(name, [])
        if not enrolled_templates:
            continue
        score = min(distance(candidate, template) for template in enrolled_templates)
        threshold = float(thresholds.get(name, DEFAULT_THRESHOLDS[name]))
        if score < best_score:
            best_name, best_score, best_threshold = name, score, threshold
    if best_name is not None and best_score <= best_threshold:
        return best_name, best_score, best_threshold
    return None, best_score, best_threshold


def collect(device="0"):
    """Guide a hands-free local enrollment and return raw in-memory samples."""
    from parley import listen

    introduction = (
        "Personalized trigger enrollment is starting. After each prompt, "
        "say the requested phrase once in your natural voice.")
    print(introduction, flush=True)
    subprocess.run(["say", introduction], check=False)
    time.sleep(2)
    collected = {}
    for name, phrase in PHRASES.items():
        collected[name] = []
        for index in range(SAMPLES[name]):
            prompt = f"Say {phrase}. Sample {index + 1} of {SAMPLES[name]}."
            print(prompt, flush=True)
            subprocess.run(["say", prompt], check=False)
            time.sleep(0.2)
            heartbeats = 0
            for burst, _ in listen.bursts(device):
                if burst is None:
                    heartbeats += 1
                    if heartbeats >= 60:
                        raise TimeoutError(f"timed out waiting for {phrase}")
                    continue
                candidate = b"".join(burst)
                if len(features(candidate)) < 8:
                    continue
                collected[name].append(candidate)
                break
    subprocess.run(["say", "Personalized trigger enrollment complete."], check=False)
    return collected
