"""Offline trigger evaluation and a live microphone meter.

`parley debug-triggers` replays labelled audio through the same speech gate,
personalized matcher, and (optionally) local Whisper the listener uses, and
reports hit and false-wake rates. Two sources:

  * a directory of 16 kHz mono WAV files whose name ends in its label, for
    example `20261101-211147-123456-wake.wav`. The listener writes exactly
    these to ~/.parley/bursts when started with PARLEY_RECORD_BURSTS=1; rename
    a file's label to correct a miss. Labels: wake, stop, send, cancel, and
    anything else (none, dictation, empty) counts as a negative.
  * `--synthetic`: phrases spoken by macOS `say` voices at several speaking
    rates, microphone gains and noise levels, plus near-miss sentences. No
    recording leaves the temporary directory it is generated in.

`parley listen meter` shows the live microphone level against the gate's
adaptive noise floor and opening level, so a quiet or hot microphone is
visible at a glance.
"""
import collections
import json
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from parley import gate, triggers

POSITIVE = ("wake", "stop", "send", "cancel")
WAKE_LIKE = ("wake", "stop")

SYNTHETIC_PHRASES = {
    "wake": ["okay computer", "okay computer, run the tests and tell me what failed",
             "okay computer, what's the status of the branch"],
    "stop": ["okay computer, stop talking"],
    "send": ["and then open a pull request. send it", "send it"],
    "cancel": ["scratch that"],
    "none": [
        "the computer is slow today",
        "okay, compute the total for me",
        "I sent it to the team yesterday",
        "let's scratch the surface of this problem",
        "we should probably refactor the listener",
        "can you hear me now",
    ],
}
SYNTHETIC_VOICES = ("Samantha", "Daniel", "Karen", "Moira", "Fred")
ENROLL_VOICE = "Samantha"
ENROLL_TEXT = {
    "wake": "okay computer", "stop": "okay computer, stop talking",
    "send": "send it", "cancel": "scratch that",
}
SYNTHETIC_RATES = (165, 200, 235)
SYNTHETIC_GAINS = (0.02, 0.06, 0.3)   # quiet laptop mic .. close headset
NOISE_RMS = 25                         # a quiet room on a MacBook microphone


def read_wav(path):
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError(f"{path}: need 16-bit PCM")
        rate, channels = handle.getframerate(), handle.getnchannels()
        data = np.frombuffer(handle.readframes(handle.getnframes()), "<i2")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1).astype("<i2")
    if rate != gate.RATE:
        positions = np.linspace(0, len(data) - 1, int(len(data) * gate.RATE / rate))
        data = np.interp(positions, np.arange(len(data)), data).astype("<i2")
    return data


def label_of(path):
    label = Path(path).stem.rsplit("-", 1)[-1].lower()
    return label if label in POSITIVE else "none"


def _noise(count, level, seed):
    rng = np.random.default_rng(seed)
    white = rng.normal(0, 1, count)
    # A little low-pass so it sounds like a room, not a hiss.
    shaped = np.convolve(white, np.ones(6) / 6, mode="same")
    shaped *= level / max(1e-9, float(np.sqrt(np.mean(shaped ** 2))))
    return shaped


def frames_of(samples):
    samples = np.asarray(samples, dtype="<i2")
    width = gate.FRAME
    usable = len(samples) // width * width
    return [samples[i:i + width].tobytes() for i in range(0, usable, width)]


def replay_gate(samples, noise=NOISE_RMS, seed=0, speech_gate=None):
    """Bursts the live gate would cut from this clip, with room noise around."""
    lead = _noise(gate.RATE, noise, seed)
    tail = _noise(int(gate.RATE * 1.2), noise, seed + 1)
    body = np.asarray(samples, dtype=np.float64) + _noise(len(samples), noise, seed + 2)
    clip = np.clip(np.concatenate((lead, body, tail)), -32768, 32767).astype("<i2")
    speech_gate = speech_gate or gate.SpeechGate()
    found = []
    for chunk in frames_of(clip):
        event, burst = speech_gate.feed(chunk)
        if burst is not None:
            found.append(burst)
    return found


def legacy_gate_passes(samples, noise=NOISE_RMS, seed=0):
    """Whether the old fixed gate (RMS 500 over 64 ms, 0.10 s voiced) opened."""
    body = np.asarray(samples, dtype=np.float64) + _noise(len(samples), noise, seed + 2)
    clip = np.clip(body, -32768, 32767)
    frame = 1024
    voiced = sum(
        np.sqrt(np.mean(clip[i:i + frame] ** 2)) > 500
        for i in range(0, len(clip) - frame, frame))
    return voiced * frame / gate.RATE >= 0.10


def _say(text, voice, rate, path):
    result = subprocess.run(
        ["say", "-v", voice, "-r", str(rate), "-o", str(path),
         "--file-format=WAVE", "--data-format=LEI16@16000", text],
        capture_output=True, check=False)
    return result.returncode == 0 and path.exists() and path.stat().st_size > 44


def synthetic_corpus(workdir, voices=SYNTHETIC_VOICES, rates=SYNTHETIC_RATES):
    """[(label, voice, rate, text, samples)] spoken by macOS voices."""
    if not shutil.which("say"):
        raise SystemExit("--synthetic needs macOS `say`")
    available = subprocess.run(
        ["say", "-v", "?"], capture_output=True, text=True).stdout
    voices = [voice for voice in voices if voice in available] or ["Samantha"]
    corpus = []
    for label, texts in SYNTHETIC_PHRASES.items():
        for text_index, text in enumerate(texts):
            for voice in voices:
                for rate in rates:
                    path = Path(workdir) / f"{label}-{text_index}-{voice}-{rate}.wav"
                    if _say(text, voice, rate, path):
                        corpus.append((label, voice, rate, text, read_wav(path)))
    return corpus


def scaled(samples, gain):
    peak = max(1, int(np.abs(samples).max()))
    return np.clip(samples.astype(np.float64) * (gain * 32767 / peak),
                   -32768, 32767)


def _whisper(frames):
    from parley import listen

    return listen.transcribe_local(frames)


def classify(burst_frames, allowed, use_matcher, use_whisper):
    """What the listener would decide for one burst: (matcher, whisper text)."""
    from parley import listen

    matched, score, threshold = (None, float("inf"), None)
    if use_matcher:
        matched, score, threshold = triggers.match(burst_frames, allowed)
    heard = _whisper(burst_frames) if use_whisper else ""
    whisper_hit = None
    if heard:
        if listen.is_stop_talking(heard, True):
            whisper_hit = "stop"
        elif "cancel" in allowed and listen.is_cancel(heard):
            whisper_hit = "cancel"
        elif "send" in allowed and listen.is_send(heard):
            whisper_hit = "send"
        elif listen.contains_wake(heard):
            whisper_hit = "wake"
    return matched, score, threshold, heard, whisper_hit


def _hit(label, found):
    if found is None:
        return False
    if label == "stop":
        return found == "stop"
    if label == "wake":
        return found in WAKE_LIKE
    return found == label


class Tally:
    def __init__(self):
        self.rows = collections.defaultdict(collections.Counter)
        self.near = []

    def add(self, label, **outcomes):
        row = self.rows[label]
        row["n"] += 1
        for key, value in outcomes.items():
            row[key] += int(bool(value))

    def report(self, columns):
        names = ["label", "n"] + list(columns)
        lines = ["  ".join(f"{name:>10}" for name in names)]
        for label in sorted(self.rows, key=lambda name: (name == "none", name)):
            row = self.rows[label]
            cells = [f"{label:>10}", f"{row['n']:>10}"]
            for column in columns:
                rate = row[column] / row["n"] if row["n"] else 0
                cells.append(f"{row[column]:>4} {rate:>4.0%}")
            lines.append("  ".join(f"{cell:>10}" for cell in cells))
        return "\n".join(lines)


def _allowed_for(label):
    # The listener only looks for send/cancel during capture.
    return ("wake", "send", "cancel", "stop") if label in ("send", "cancel") else (
        "wake", "stop")


def evaluate_clips(clips, use_matcher, use_whisper, through_gate, verbose=False):
    """clips: [(label, name, samples)]. Returns (Tally, details)."""
    tally, details = Tally(), []
    for index, (label, name, samples) in enumerate(clips):
        if through_gate:
            bursts = [b for b in replay_gate(samples, seed=index) if not b.short]
            legacy = legacy_gate_passes(samples, seed=index)
        else:
            bursts = [frames_of(samples)]
            legacy = None
        allowed = _allowed_for(label)
        matcher_found, whisper_found, best_score, threshold = None, None, None, None
        heard_all = []
        for burst in bursts:
            matched, score, limit, heard, whisper_hit = classify(
                burst, allowed, use_matcher, use_whisper)
            if score is not None and (best_score is None or score < best_score):
                best_score, threshold = score, limit
            matcher_found = matcher_found or matched
            whisper_found = whisper_found or whisper_hit
            if heard:
                heard_all.append(heard)
        positive = label in POSITIVE
        outcome = {"gate": bool(bursts)}
        if legacy is not None:
            outcome["old_gate"] = legacy
        if positive:
            if use_matcher:
                outcome["matcher"] = _hit(label, matcher_found)
            if use_whisper:
                outcome["whisper"] = _hit(label, whisper_found)
            outcome["either"] = (_hit(label, matcher_found)
                                 or _hit(label, whisper_found))
        else:
            false_matcher = matcher_found in WAKE_LIKE
            false_whisper = whisper_found in WAKE_LIKE
            if use_matcher:
                outcome["matcher"] = false_matcher
            if use_whisper:
                outcome["whisper"] = false_whisper
            outcome["either"] = false_matcher or false_whisper
        tally.add(label, **outcome)
        detail = {
            "label": label, "name": name, "bursts": len(bursts),
            "matcher": matcher_found, "whisper": whisper_found,
            "score": (None if best_score in (None, float("inf"))
                      else round(best_score, 3)),
            "threshold": threshold, "heard": " | ".join(heard_all),
        }
        details.append(detail)
        if verbose:
            print(json.dumps(detail))
    return tally, details


def debug_triggers(directory=None, synthetic=False, whisper=False,
                   verbose=False, as_json=False):
    columns = ["gate", "old_gate", "matcher", "whisper", "either"]
    if synthetic:
        with tempfile.TemporaryDirectory(prefix="parley-eval-") as tmp:
            print("Synthesizing speech with macOS voices...", file=sys.stderr)
            corpus = synthetic_corpus(tmp)
            enroll_set = [c for c in corpus if c[1] == ENROLL_VOICE]
            # Enroll the way `parley enroll` does: the enrollment voice says
            # each bare phrase into a quiet laptop microphone, through the
            # same noisy gate the listener uses.
            samples = {}
            for index, (label, voice, rate, text, data) in enumerate(enroll_set):
                if text != ENROLL_TEXT.get(label):
                    continue
                for burst in replay_gate(scaled(data, 0.06), seed=900 + index):
                    if not burst.short:
                        samples.setdefault(label, []).append(b"".join(burst))
            original = triggers.config.TRIGGERS
            triggers.config.TRIGGERS = Path(tmp) / "triggers"
            triggers.load.cache_clear()
            try:
                use_matcher = False
                try:
                    metadata = triggers.save(samples)
                    use_matcher = True
                    print(f"synthetic profile thresholds: {metadata['thresholds']}",
                          file=sys.stderr)
                except ValueError as exc:
                    print(f"matcher skipped: {exc}", file=sys.stderr)
                groups = collections.defaultdict(list)
                for label, voice, rate, text, data in corpus:
                    speaker = ("enrolled speaker" if voice == ENROLL_VOICE
                               else "other speakers")
                    for gain in SYNTHETIC_GAINS:
                        group = f"{speaker}, peak gain {gain}"
                        groups[group].append((
                            label, f"{voice}/{rate}/{gain}:{text}",
                            scaled(data, gain)))
                results = {}
                for group, clips in groups.items():
                    tally, details = evaluate_clips(
                        clips, use_matcher, whisper, True, verbose)
                    results[group] = (tally, details)
            finally:
                triggers.config.TRIGGERS = original
                triggers.load.cache_clear()
    else:
        directory = Path(directory or (triggers.config.STATE / "bursts"))
        paths = sorted(directory.glob("*.wav"))
        if not paths:
            raise SystemExit(
                f"no WAV files in {directory}. Record some with "
                "PARLEY_RECORD_BURSTS=1, or use --synthetic.")
        clips = [(label_of(path), path.name, read_wav(path)) for path in paths]
        use_matcher = triggers.enrolled()
        tally, details = evaluate_clips(clips, use_matcher, whisper, False, verbose)
        results = {str(directory): (tally, details)}
        columns = ["gate", "matcher", "whisper", "either"]

    if as_json:
        print(json.dumps({
            group: {label: dict(row) for label, row in tally.rows.items()}
            for group, (tally, _) in results.items()
        }, indent=2))
        return results
    for group, (tally, _) in results.items():
        shown = [c for c in columns if any(c in row for row in tally.rows.values())]
        print(f"\n{group}")
        print(tally.report(shown))
    print("\nPositive rows are hit rates; the 'none' row is the false-wake rate.")
    return results


def meter(device="default", seconds=None, out=sys.stdout):
    """Print the live microphone level against the gate's thresholds."""
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    process = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
         "-i", f":{device}", "-ac", "1", "-ar", str(gate.RATE), "-f", "s16le",
         "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    speech_gate = gate.SpeechGate()
    vad_name = "webrtc" if speech_gate.vad is not None else "energy only"
    print(f"Mic level (RMS) vs adaptive gate, VAD={vad_name}. Ctrl-C to stop.",
          file=out)
    started = time.time()
    width, scale = 50, 2000.0
    levels, voiced = [], 0
    try:
        while seconds is None or time.time() - started < seconds:
            chunk = process.stdout.read(gate.FRAME * 2)
            if not chunk:
                break
            level, opens, stays, is_voice = speech_gate.classify(chunk)
            event, burst = speech_gate.feed(chunk)
            levels.append(level)
            voiced += int(is_voice)
            if len(levels) < 3:          # ~90 ms per line
                continue
            peak = max(levels)
            open_level = speech_gate.open_level()
            filled = min(width, int(width * min(1.0, peak / scale) ** 0.5))
            mark = min(width - 1, int(width * min(1.0, open_level / scale) ** 0.5))
            bar = ["#" if i < filled else " " for i in range(width)]
            bar[mark] = "|"
            state = ("SPEECH" if speech_gate.open
                     else "voice?" if voiced else "")
            out.write(f"\r{''.join(bar)} rms={peak:5d} floor={speech_gate.floor:5.0f} "
                      f"open={open_level:5.0f} {state:6}")
            out.flush()
            if burst is not None:
                kind = "short" if burst.short else "burst"
                out.write(f"\n  {kind}: {burst.describe()}\n")
            levels, voiced = [], 0
    except KeyboardInterrupt:
        pass
    finally:
        process.terminate()
        out.write("\n")
