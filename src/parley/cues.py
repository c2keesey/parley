"""Short tones that mark state changes: woke, sent, cancelled, finished.

Generated rather than shipped, so there are no asset files and no ffmpeg
dependency. Each note is a sine with a couple of quiet harmonics under an
exponential decay, which is what makes it read as a chime instead of a beep,
and every note is faded in and out so there is no click at the edges.

Cues register with the player's pid file, so the listener treats them as
"Claude is making noise" and will not try to transcribe them.
"""
import math
import os
import struct
import subprocess
import wave

from parley import config

RATE = 44100
AMPLITUDE = 0.22
HARMONICS = [(1.0, 1.0), (2.0, 0.18), (3.0, 0.06)]

# (frequency, seconds). Rising means listening, falling means finished.
PATTERNS = {
    "wake": [(587.33, 0.10), (880.00, 0.16)],      # D5 -> A5, an open question
    "send": [(880.00, 0.09), (1174.66, 0.09), (1318.51, 0.18)],  # A5 -> D6 -> E6
    "cancel": [(440.00, 0.11), (329.63, 0.20)],    # A4 -> E4, falling away
    "done": [(659.25, 0.09), (523.25, 0.22)],      # E5 -> C5, soft full stop
}


def _note(frequency, seconds, samples):
    total = int(RATE * seconds)
    for i in range(total):
        t = i / RATE
        decay = math.exp(-3.2 * t / seconds)
        # Fade the first and last few milliseconds so the edges do not click.
        edge = min(1.0, i / (RATE * 0.006), (total - i) / (RATE * 0.010))
        value = sum(
            gain * math.sin(2 * math.pi * frequency * mult * t)
            for mult, gain in HARMONICS
        )
        samples.append(int(32767 * AMPLITUDE * value * decay * edge))


def build(name):
    """Render a cue to a wav file once and cache it."""
    path = config.STATE / f"cue-{name}.wav"
    if path.exists():
        return path
    pattern = PATTERNS.get(name)
    if not pattern:
        return None
    samples = []
    for frequency, seconds in pattern:
        _note(frequency, seconds, samples)
    samples = [max(-32767, min(32767, s)) for s in samples]
    config.STATE.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(struct.pack(f"{len(samples)}h", *samples))
    return path


def play(name, wait=True):
    path = build(name)
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
    if not os.environ.get("PARLEY_NO_PREBUILD"):
        for name in PATTERNS:
            build(name)
