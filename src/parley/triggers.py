"""Speaker-personalized trigger matching over local acoustic features.

Profiles contain normalized MFCC-like feature matrices, never raw recordings.
Dynamic time warping makes comparisons tolerant of natural changes in pace.

This matcher is a recall booster. The listener accepts a confident match as a
trigger and otherwise hands the burst to Whisper exactly as if the matcher did
not exist, so a poor profile can cost nothing worse than a missed shortcut.

Why the first version never matched (profile version 1, enrolled 2026-08-27):

  * It compared a whole burst against a whole template. A live wake burst is
    usually "okay computer, run the tests ...", several times the template's
    length, which the 0.5-2x length guard turned into an infinite distance.
    Matching is now subsequence DTW anchored where each phrase is said: wake
    and stop at the start of a burst, send and cancel at its end.
  * Silence trimming used an absolute level (0.008, about RMS 262). On a
    quiet built-in microphone with speech peaking near RMS 600 that trimmed
    the soft parts of the phrase itself; in a noisier room it kept the room.
    Trimming is now relative to the burst's own noise floor and peak.
  * Enrollment stored whatever a burst held: one "scratch that" template was
    14.5 seconds long, another 0.2 seconds. Samples now need a plausible
    duration and are trimmed before they are saved.
  * Thresholds were calibrated on positives only. They now also sit below the
    distance to the other phrases' recordings, so a match means something.

Version 1 profiles are ignored; run `parley enroll` again.
"""
import json
import math
import os
import subprocess
import time
from functools import lru_cache

import numpy as np

from parley import config

VERSION = 2
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
SAMPLES = {"wake": 6, "send": 6, "cancel": 5, "stop": 5}
# Where in a burst each phrase is said.
ANCHORS = {"wake": "start", "stop": "start", "send": "end", "cancel": "end"}
DEFAULT_THRESHOLDS = {
    "wake": 0.30,
    "send": 0.28,
    "cancel": 0.28,
    "stop": 0.30,
}
MIN_DURATION = 0.25
MAX_DURATION = 3.0
REGION_SLACK = 1.6        # search this many template-lengths from the anchor
MARGIN_SECONDS = 0.12


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
WIDTH = (COEFFICIENTS - 1) * 2


def _samples(audio):
    if isinstance(audio, np.ndarray):
        return audio.astype(np.float32)
    if isinstance(audio, (list, tuple)):
        audio = b"".join(audio)
    return np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768


def voiced_span(samples):
    """(first, last) 10 ms frame indices of speech, relative to the burst.

    The threshold is relative: well above this burst's own quiet frames and a
    fixed fraction of its peak, so the microphone's gain does not matter.
    """
    framed = _frames(samples)
    if not len(framed):
        return None
    energy = np.sqrt(np.mean(framed * framed, axis=1) + 1e-12)
    noise = float(np.percentile(energy, 10))
    peak = float(energy.max())
    threshold = max(noise * 2.5, peak * 0.08, 3e-4)
    if peak < threshold:
        return None
    voiced = np.flatnonzero(energy >= threshold)
    if not len(voiced):
        return None
    return int(voiced[0]), int(voiced[-1])


def features(audio, trim=True):
    """Return time-normalized acoustic features for 16 kHz signed PCM."""
    samples = _samples(audio)
    framed = _frames(samples)
    if not len(framed):
        return np.empty((0, WIDTH), dtype=np.float32)
    if trim:
        span = voiced_span(samples)
        if span is None:
            return np.empty((0, WIDTH), dtype=np.float32)
        start = max(0, span[0] - 3)
        end = min(len(framed), span[1] + 4)
        framed = framed[start:end]

    framed = framed * np.hanning(FRAME_LENGTH).astype(np.float32)
    power = np.abs(np.fft.rfft(framed, FFT_SIZE)) ** 2
    mel = np.maximum(power @ MEL_FILTERS.T, 1e-10)
    cepstra = np.log(mel) @ DCT.T
    cepstra = cepstra[:, 1:]
    cepstra -= cepstra.mean(axis=0, keepdims=True)
    scale = cepstra.std(axis=0, keepdims=True)
    cepstra /= np.maximum(scale, 0.1)
    delta = (np.gradient(cepstra, axis=0) if len(cepstra) > 1
             else np.zeros_like(cepstra))
    result = np.concatenate((cepstra, delta), axis=1).astype(np.float32)
    norms = np.linalg.norm(result, axis=1, keepdims=True)
    return result / np.maximum(norms, 1e-6)


def subsequence_distance(template, candidate, min_span=0.6, max_span=1.8):
    """Best DTW cosine distance of the whole template to part of candidate.

    Free start and end in the candidate, so a phrase inside a longer burst
    still matches. The matched span must be min_span-max_span template lengths
    long, which keeps a phrase from aligning to a fragment. The result is the
    accumulated cost over max(template length, span), as in plain DTW.
    """
    rows, columns = len(template), len(candidate)
    if not rows or not columns or columns < rows * min_span:
        return float("inf")
    costs = 1 - template @ candidate.T            # rows x columns
    index = np.arange(columns)
    previous = np.zeros(columns, dtype=np.float64)
    previous_start = index.copy()
    for row in range(rows):
        cost = costs[row].astype(np.float64)
        if row == 0:
            current = cost.copy()
            start = index.copy()
        else:
            # From above (same column) or diagonal (previous column).
            diagonal = np.concatenate(([np.inf], previous[:-1]))
            diagonal_start = np.concatenate(([0], previous_start[:-1]))
            take_diagonal = diagonal < previous
            entry = cost + np.where(take_diagonal, diagonal, previous)
            entry_start = np.where(
                take_diagonal, diagonal_start, previous_start)
            # Horizontal moves: current[c] = min_k<=c entry[k] + sum cost(k, c].
            prefix = np.cumsum(cost)
            shifted = entry - prefix
            running = np.minimum.accumulate(shifted)
            current = running + prefix
            best_at = np.maximum.accumulate(
                np.where(shifted <= running, index, 0))
            start = entry_start[best_at]
        previous, previous_start = current, start
    span = index - previous_start + 1
    valid = (span >= rows * min_span) & (span <= rows * max_span)
    if not valid.any():
        return float("inf")
    normalized = previous / np.maximum(rows, span)
    return float(normalized[valid].min())


def distance(left, right):
    """Symmetric whole-utterance distance, kept for calibration and tests."""
    if not len(left) or not len(right):
        return float("inf")
    if not 0.5 <= len(left) / len(right) <= 2.0:
        return float("inf")
    return subsequence_distance(left, right, min_span=0.5, max_span=2.0)


def _profile_path(name, index):
    return config.TRIGGERS / f"{name}-{index}.npy"


def usable(recording):
    """Features of one enrollment sample, or None with the reason."""
    samples = _samples(recording)
    span = voiced_span(samples)
    if span is None:
        return None, "no speech"
    seconds = (span[1] - span[0] + 1) * HOP / RATE
    if seconds < MIN_DURATION:
        return None, f"too short ({seconds:.2f}s)"
    if seconds > MAX_DURATION:
        return None, f"too long ({seconds:.2f}s)"
    vector = features(samples)
    if len(vector) < 8:
        return None, "too little audio"
    return vector, ""


def calibrate(vectors, impostors, default):
    """Threshold between this phrase's spread and the nearest other phrase."""
    nearest = []
    for index, vector in enumerate(vectors):
        nearest.append(min(
            subsequence_distance(vector, other)
            for other_index, other in enumerate(vectors)
            if other_index != index
        ))
    finite = [value for value in nearest if math.isfinite(value)]
    observed = float(np.percentile(finite, 90)) if finite else default
    threshold = min(default + 0.10, max(default, observed * 1.15))
    rivals = [
        subsequence_distance(vector, impostor)
        for vector in vectors for impostor in impostors
    ]
    rivals = [value for value in rivals if math.isfinite(value)]
    if rivals:
        threshold = min(threshold, min(rivals) * 0.92)
    return round(threshold, 4)


def save(samples):
    """Persist local feature templates and calibrated thresholds."""
    vectors_by_name = {}
    for name, recordings in samples.items():
        vectors = []
        for recording in recordings:
            vector, _ = usable(recording)
            if vector is not None:
                vectors.append(vector)
        if len(vectors) < 3:
            raise ValueError(f"not enough usable {name} recordings")
        vectors_by_name[name] = vectors

    config.TRIGGERS.mkdir(parents=True, exist_ok=True)
    os.chmod(config.TRIGGERS, 0o700)
    for old_template in config.TRIGGERS.glob("*.npy"):
        old_template.unlink()
    metadata = {"version": VERSION, "thresholds": {}}
    for name, vectors in vectors_by_name.items():
        # "okay computer" is the start of "okay computer stop talking", so
        # those two are not each other's impostors.
        related = {"wake", "stop"} if name in {"wake", "stop"} else {name}
        impostors = [
            vector for other, others in vectors_by_name.items()
            if other not in related for vector in others
        ]
        metadata["thresholds"][name] = calibrate(
            vectors, impostors, DEFAULT_THRESHOLDS[name])
        for index, vector in enumerate(vectors):
            path = _profile_path(name, index)
            np.save(path, vector, allow_pickle=False)
            os.chmod(path, 0o600)
    metadata_path = config.TRIGGERS / "profile.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    os.chmod(metadata_path, 0o600)
    load.cache_clear()
    return metadata


def profile_version():
    try:
        return int(json.loads(
            (config.TRIGGERS / "profile.json").read_text()).get("version", 1))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


@lru_cache(maxsize=1)
def load():
    """Load the profile, returning empty data when enrollment is absent.

    A profile from an older matcher is treated as absent: its templates were
    made with different trimming and cannot be compared meaningfully.
    """
    metadata_path = config.TRIGGERS / "profile.json"
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, ValueError):
        return {}, {}
    if metadata.get("version") != VERSION:
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


def stale():
    """An enrollment exists but was made for an older matcher."""
    version = profile_version()
    return bool(version) and version != VERSION


def _region(samples, span, template_rows, anchor):
    """The audio around where a phrase of this length would be said."""
    margin = int(MARGIN_SECONDS * RATE)
    length = int(template_rows * REGION_SLACK * HOP) + FRAME_LENGTH
    first = span[0] * HOP
    last = span[1] * HOP + FRAME_LENGTH
    if anchor == "start":
        begin = max(0, first - margin)
        return samples[begin:min(len(samples), first + length)]
    end = min(len(samples), last + margin)
    return samples[max(0, last - length):end]


def match(audio, allowed=None):
    """Return (trigger, score, threshold), or (None, best score, threshold)."""
    templates, thresholds = load()
    choices = tuple(allowed or PHRASES)
    samples = _samples(audio)
    span = voiced_span(samples)
    if span is None:
        return None, float("inf"), None
    region_features = {}
    scored = {}
    for name in choices:
        enrolled_templates = templates.get(name, [])
        if not enrolled_templates:
            continue
        anchor = ANCHORS.get(name, "start")
        score = float("inf")
        for template in enrolled_templates:
            # Regions are rounded to 50 ms so templates of similar length
            # share one feature extraction.
            key = (anchor, int(len(template) * REGION_SLACK / 5))
            if key not in region_features:
                region_features[key] = features(
                    _region(samples, span, len(template), anchor))
            score = min(score, subsequence_distance(
                template, region_features[key]))
        threshold = float(thresholds.get(name, DEFAULT_THRESHOLDS[name]))
        scored[name] = (score, threshold)
    if not scored:
        return None, float("inf"), None
    # "okay computer stop talking" begins with the wake phrase, so the wake
    # templates match it too. When the longer command matches, it wins.
    if "stop" in scored and scored["stop"][0] <= scored["stop"][1]:
        return "stop", *scored["stop"]
    # Otherwise compare relative to each phrase's own threshold, so a phrase
    # with a looser calibration does not win by having larger distances.
    name = min(scored, key=lambda key: scored[key][0] / scored[key][1])
    score, threshold = scored[name]
    if score <= threshold:
        return name, score, threshold
    return None, score, threshold


def collect(device="default"):
    """Guide a hands-free local enrollment and return raw in-memory samples."""
    from parley import listen

    introduction = (
        "Personalized trigger enrollment is starting. After each prompt, "
        "say the requested phrase once in your natural voice, at your usual "
        "distance from the microphone.")
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
            # Enrollment owns its prompts and is not a live dictation loop.
            for burst, _ in listen.bursts(device, reserve_output=False):
                if burst is None:
                    heartbeats += 1
                    if heartbeats >= 60:
                        raise TimeoutError(f"timed out waiting for {phrase}")
                    continue
                if getattr(burst, "short", False):
                    continue
                candidate = b"".join(burst)
                vector, reason = usable(candidate)
                if vector is None:
                    print(f"  didn't catch that ({reason}); again please.",
                          flush=True)
                    subprocess.run(["say", "Again, please."], check=False)
                    continue
                collected[name].append(candidate)
                break
    subprocess.run(["say", "Personalized trigger enrollment complete."], check=False)
    return collected
