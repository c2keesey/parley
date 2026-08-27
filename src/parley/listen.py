"""Hands-free input: wake phrase, dictate, send phrase, typed into the session.

Three layers, cheapest first, so nothing expensive runs while the room is quiet:

  1. An energy gate over raw microphone frames. No model, no network. Silence
     costs nothing, which is what makes always-on affordable.
  2. A tiny local whisper over each speech burst, looking only for the wake or
     send phrase. Runs on speech, never on wall-clock time.
  3. The accurate cloud model, once, on the message you actually dictated.

Guardrails against the usual over-triggering: a wake phrase is required before
anything is captured, an explicit send phrase is required before anything is
submitted, and capture stops itself on silence or a hard time cap so a wedged
listener can never deliver a colossal transcript.

Speech that overlaps the agent's own voice is kept rather than discarded, but
only the wake phrase is trusted in it. That is what allows barge-in: say the
wake phrase over a reply and it stops talking and listens.

"Okay computer, stop talking" is a stricter local-only path. When spoken over
active playback it silences Parley immediately and is never sent to an agent.
"""
import os
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from parley import config, cues, player

RATE = 16000
FRAME = 1024
SPEECH_RMS = int(os.environ.get("PARLEY_MIC_THRESHOLD", "500"))
MIN_SPEECH = 0.35        # seconds of sound before a burst counts as speech
END_SILENCE = 0.7        # seconds of quiet that ends a burst
INTERRUPT_SILENCE = 0.25 # quicker burst completion while speech is playing
MAX_BURST = 15.0         # hard cap on a single burst
SILENCE_TIMEOUT = float(os.environ.get("PARLEY_SILENCE_TIMEOUT", "120"))
HARD_STOP = float(os.environ.get("PARLEY_HARD_STOP", "1200"))

WAKE = os.environ.get("PARLEY_WAKE", "okay computer")
SEND = os.environ.get("PARLEY_SEND", "send it")
# Deliberately not "cancel" or "stop" — those turn up in ordinary dictation
# about code, and a discard phrase that fires by accident is worse than none.
CANCEL = os.environ.get("PARLEY_CANCEL", "scrap that")
STOP_TALKING = os.environ.get("PARLEY_STOP_TALKING", "stop talking")

MODEL_DIR = Path(os.environ.get(
    "PARLEY_WHISPER_MODELS", Path.home() / ".cache" / "parley"))
TINY = MODEL_DIR / "ggml-tiny.en.bin"
TINY_URL = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-tiny.en.bin")

TARGET = config.STATE / "target"
LISTEN_PID = config.STATE / "listener.pid"


def whisper_bin():
    return shutil.which("whisper-cli") or shutil.which("whisper-cpp")


def ensure_model():
    if TINY.exists():
        return True
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading the wake-word model to {TINY} (~75MB)...")
    partial = TINY.with_suffix(".part")
    result = subprocess.run(
        ["curl", "-fL", "--progress-bar", "-o", str(partial), TINY_URL])
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        return False
    partial.rename(TINY)
    return True


def normalize(text):
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    return cleaned.split()


def contains_phrase(text, phrase):
    """Word-sequence match, so punctuation and surrounding filler do not matter."""
    words, target = normalize(text), normalize(phrase)
    if not target or len(words) < len(target):
        return False
    return any(
        words[i:i + len(target)] == target
        for i in range(len(words) - len(target) + 1)
    )


def is_stop_talking(text, overlapped):
    """The dedicated interrupt is trusted only when playback overlapped it."""
    return overlapped and contains_phrase(text, f"{WAKE} {STOP_TALKING}")


def strip_phrase(text, phrase):
    """Drop a trailing send phrase from the dictated message."""
    words, target = normalize(text), normalize(phrase)
    spoken = text.split()
    if len(words) >= len(target) and words[-len(target):] == target:
        return " ".join(spoken[: len(spoken) - len(target)]).strip(" ,.")
    return text.strip()


def strip_leading(text, phrase):
    """Drop the wake phrase and anything before it.

    You usually keep talking in the same breath as the wake phrase, so the
    burst that woke us also holds the start of the message. Keeping that audio
    and trimming the words here is what stops the first sentence going missing.
    """
    words, target = normalize(text), normalize(phrase)
    spoken = text.split()
    if not target or len(words) < len(target):
        return text.strip()
    for i in range(len(words) - len(target) + 1):
        if words[i:i + len(target)] == target:
            return " ".join(spoken[i + len(target):]).strip(" ,.")
    return text.strip()


def write_wav(frames, path):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(b"".join(frames))


def transcribe_local(frames):
    """Tiny local model, used only to spot the wake and send phrases."""
    binary = whisper_bin()
    if not binary or not TINY.exists():
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "burst.wav"
        write_wav(frames, wav)
        try:
            result = subprocess.run(
                [binary, "-m", str(TINY), "-f", str(wav),
                 "-nt", "-np", "-t", "4"],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return " ".join(result.stdout.split())


def transcribe_cloud(frames):
    """The accurate model, once, on the message actually dictated."""
    import json
    import urllib.request

    key = config.api_key()
    if not key:
        raise RuntimeError("no OPENAI_API_KEY for transcription")

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "message.wav"
        write_wav(frames, wav)
        audio = wav.read_bytes()

    model = os.environ.get("PARLEY_STT_MODEL", "gpt-4o-transcribe")
    boundary = f"----parley{time.time_ns()}"
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="model"'
        f"\r\n\r\n{model}\r\n".encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="message.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
        audio,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    reply = json.loads(urllib.request.urlopen(request, timeout=90).read())
    return (reply.get("text") or "").strip()


def speaking():
    """True while a player is running, so overlapping speech can be flagged."""
    try:
        pids = config.PIDFILE.read_text().split()
    except OSError:
        return False
    for pid in pids:
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            continue
    return False


def cue(kind):
    """A short chime marking wake, send, and cancel."""
    cues.play(kind)


def set_target(pane):
    config.STATE.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(pane or "")


def get_target():
    try:
        return TARGET.read_text().strip()
    except OSError:
        return ""


def inject(text):
    """Type the message into the session's pane and submit it."""
    pane = get_target()
    if not pane or not text:
        return False
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=False)
    time.sleep(0.15)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=False)
    config.log(f"injected into {pane}: {text[:80]!r}")
    return True


def rms(chunk):
    usable = len(chunk) // 2 * 2
    if usable < 2:
        return 0
    samples = struct.unpack(f"{usable // 2}h", chunk[:usable])
    return int((sum(s * s for s in samples) / len(samples)) ** 0.5)


def bursts(device="0"):
    """Yield (frames, was_playing) per speech burst, plus idle heartbeats.

    Bursts are still collected while the agent is speaking. Dropping them was
    a real bug: you naturally interrupt, and the wake phrase you said over the
    reply was silently discarded. The burst is tagged instead, so the caller
    can require the wake phrase before trusting audio that may contain the
    agent's own voice.

    A (None, False) heartbeat is yielded about once a second so timeouts can
    fire during silence, when no burst would ever arrive.
    """
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    process = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-f", "avfoundation", "-i", f":{device}",
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_seconds = FRAME / RATE
    buffer, voiced, quiet, overlapped = [], 0.0, 0.0, False
    since_beat = 0.0
    try:
        while True:
            chunk = process.stdout.read(FRAME * 2)
            if not chunk:
                return
            since_beat += frame_seconds
            if since_beat >= 1.0:
                since_beat = 0.0
                yield None, False
            if rms(chunk) > SPEECH_RMS:
                buffer.append(chunk)
                voiced += frame_seconds
                quiet = 0.0
                if speaking():
                    overlapped = True
            elif buffer:
                buffer.append(chunk)
                quiet += frame_seconds
                end_silence = INTERRUPT_SILENCE if overlapped else END_SILENCE
                if quiet >= end_silence or voiced >= MAX_BURST:
                    if voiced >= MIN_SPEECH:
                        yield buffer, overlapped
                    buffer, voiced, quiet, overlapped = [], 0.0, 0.0, False
    finally:
        process.terminate()


def run(device="0"):
    """The listening loop. Runs until killed."""
    if not whisper_bin():
        raise SystemExit(
            "whisper-cli not found. Install it with: brew install whisper-cpp")
    if not ensure_model():
        raise SystemExit("could not download the wake-word model")

    config.STATE.mkdir(parents=True, exist_ok=True)
    LISTEN_PID.write_text(str(os.getpid()))
    config.log(f"listening wake={WAKE!r} send={SEND!r} device={device}")

    capturing, captured, started, last_heard = False, [], 0.0, 0.0

    def finish(frames):
        cue("send")
        try:
            spoken = transcribe_cloud(frames)
        except Exception as exc:
            config.log(f"transcription failed: {exc}")
            return
        message = strip_phrase(strip_leading(spoken, WAKE), SEND)
        config.log(f"message {message[:80]!r}")
        if message:
            inject(message)

    try:
        for burst, overlapped in bursts(device):
            now = time.time()

            if capturing:
                # Two independent stops. Silence means you walked away or the
                # send phrase was never recognised. The hard cap exists so a
                # listener wedged open cannot deliver a colossal transcript.
                if now - last_heard > SILENCE_TIMEOUT:
                    capturing, captured = False, []
                    cue("cancel")
                    config.log("discarded: silent too long")
                    continue
                if now - started > HARD_STOP:
                    capturing, captured = False, []
                    cue("cancel")
                    config.log("discarded: hard stop")
                    continue
                if len(captured) * FRAME / RATE > HARD_STOP:
                    capturing, captured = False, []
                    cue("cancel")
                    config.log("discarded: too much audio")
                    continue

            if burst is None:
                continue

            heard = transcribe_local(burst)
            if not heard:
                continue
            config.log(f"heard {heard[:80]!r} capturing={capturing} "
                       f"overlapped={overlapped}")

            # This is a voice-control command, not dictation. It is handled
            # before capture state, never reaches cloud transcription, emits
            # no confirmation sound, and cannot become a chat message.
            if is_stop_talking(heard, overlapped):
                player.stop()
                capturing, captured = False, []
                config.log("voice-control: stopped talking")
                continue

            if not capturing:
                if not contains_phrase(heard, WAKE):
                    continue
                # Barge-in: if this landed over the agent's own speech, stop it
                # talking. Requiring the wake phrase is what makes overlapping
                # audio safe to act on.
                if overlapped:
                    player.stop()
                    config.log("barged in")
                # Keep this burst — you normally keep talking in the same breath
                # as the wake phrase. strip_leading drops everything up to and
                # including it, which also removes any of the agent's words.
                capturing, captured = True, list(burst)
                started = last_heard = now
                cue("wake")
                if contains_phrase(heard, SEND):
                    capturing, captured = False, []
                    finish(list(burst))
                continue

            if contains_phrase(heard, CANCEL):
                capturing, captured = False, []
                cue("cancel")
                config.log("discarded")
                continue

            last_heard = now
            captured.extend(burst)
            if not contains_phrase(heard, SEND):
                continue
            capturing = False
            frames, captured = captured, []
            finish(frames)
    finally:
        LISTEN_PID.unlink(missing_ok=True)


def is_running():
    try:
        pid = int(LISTEN_PID.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return 0


def stop():
    pid = is_running()
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    LISTEN_PID.unlink(missing_ok=True)
    return bool(pid)
