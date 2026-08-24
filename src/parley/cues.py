"""Short tones that mark state changes: woke, sent, cancelled, finished.

Tuned to published earcon guidance rather than taste:

  - Keep them short. Around 200ms total; research finds shorter durations are
    markedly less distracting, and these fire many times an hour.
  - Keep fundamentals in the 350-1000Hz band that earcon studies centre on.
  - Put no harmonic energy in 3-6kHz, the band that reads as harsh. That is
    what made the first attempt unpleasant: a third harmonic over an 880Hz
    fundamental lands at 2.6kHz, and over 1.3kHz it lands at 4kHz.
  - Let contour carry the meaning. Rising opens (listening), falling closes
    (finished), falling and darker means discarded.
  - Match loudness across the set so no one cue jumps out.

Generated rather than shipped, so there are no asset files and no ffmpeg
dependency. To use a sound pack instead, point PARLEY_CUE_<NAME> at a wav or
mp3 file; plenty of CC0 UI packs exist and nothing here assumes these tones.

Cues register with the player's pid file, so the listener treats them as
"the agent is making noise" and will not try to transcribe them.
"""
import math
import os
import struct
import subprocess
import wave
from pathlib import Path

from parley import config

RATE = 44100
AMPLITUDE = 0.16
# Fundamental plus one quiet octave. No third harmonic: over these
# fundamentals it would sit in the harsh 3-6kHz band.
HARMONICS = [(1.0, 1.0), (2.0, 0.14)]

# (frequency, seconds). All fundamentals sit under 1kHz, so the highest
# partial in the set is about 2kHz.
PATTERNS = {
    "wake": [(523.25, 0.07), (783.99, 0.13)],    # C5 -> G5 rising, ready
    "send": [(659.25, 0.06), (987.77, 0.13)],    # E5 -> B5 rising, committed
    "done": [(783.99, 0.07), (523.25, 0.15)],    # G5 -> C5 falling, resolved
    "cancel": [(466.16, 0.07), (349.23, 0.15)],  # Bb4 -> F4 falling, dropped
}


SOUNDS = Path(__file__).parent / "sounds"


def override(name):
    """A sound file to use instead of the generated tone, if one is set."""
    path = os.environ.get(f"PARLEY_CUE_{name.upper()}")
    return path if path and os.path.exists(path) else None


def bundled(name):
    """The shipped sound for this cue.

    These are from Kenney's CC0 interface packs — sounds made by someone who
    does this for a living, loudness-matched to -14 LUFS so the set is even.
    Chosen for meaning, not just pleasantness: an interrogative to open, a
    confirmation to commit, a resolved bell to close, a retreat to discard.
    The synthesized tones remain as a fallback if the files are missing.
    """
    if os.environ.get("PARLEY_SYNTH_CUES"):
        return None
    path = SOUNDS / f"{name}.wav"
    return str(path) if path.exists() else None


def _note(frequency, seconds, samples):
    total = int(RATE * seconds)
    for i in range(total):
        t = i / RATE
        # A gentle decay reads as a struck tone; a steep one reads as a beep.
        decay = math.exp(-2.2 * t / seconds)
        # Fade both edges, the tail more slowly, so nothing clicks.
        edge = min(1.0, i / (RATE * 0.008), (total - i) / (RATE * 0.018))
        value = sum(
            gain * math.sin(2 * math.pi * frequency * mult * t)
            for mult, gain in HARMONICS
        )
        samples.append(32767 * AMPLITUDE * value * decay * edge)


def build(name):
    """Render a cue to a wav file once and cache it."""
    pattern = PATTERNS.get(name)
    if not pattern:
        return None
    path = config.STATE / f"cue-{name}.wav"
    if path.exists():
        return path
    samples = []
    for frequency, seconds in pattern:
        _note(frequency, seconds, samples)
    peak = max((abs(s) for s in samples), default=1.0) or 1.0
    # Normalise so every cue lands at the same perceived level.
    ceiling = 32767 * AMPLITUDE
    scaled = [int(max(-32767, min(32767, s / peak * ceiling))) for s in samples]
    config.STATE.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(struct.pack(f"{len(scaled)}h", *scaled))
    return path


def play(name, wait=True):
    path = override(name) or bundled(name) or build(name)
    if not path:
        return
    try:
        proc = subprocess.Popen(["afplay", str(path)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return
    # Registered so the listener knows this noise is ours and ignores it.
    try:
        with open(config.PIDFILE, "a") as fh:
            fh.write(f"{proc.pid}\n")
    except OSError:
        pass
    if wait:
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def rebuild():
    """Drop cached cues so changed patterns take effect."""
    for name in PATTERNS:
        (config.STATE / f"cue-{name}.wav").unlink(missing_ok=True)
        (config.STATE / f"cue-{name}.mp3").unlink(missing_ok=True)
    for name in PATTERNS:
        build(name)
