"""Hands-free input: wake phrase, dictate, send phrase, injected into Claude Code.

Three layers, cheapest first, so nothing expensive runs while the room is quiet:

  1. An energy gate over raw microphone frames. No model, no network. Silence
     costs nothing, which is what makes always-on affordable.
  2. A tiny local whisper over each speech burst, looking only for the wake or
     send phrase. Runs on speech, never on wall-clock time.
  3. The accurate cloud model, once, on the message you actually dictated.

Guardrails against the usual over-triggering: a wake phrase is required before
anything is captured, an explicit send phrase is required before anything is
submitted, capture expires on its own, and the microphone is ignored entirely
while Claude is speaking so it can never hear itself.
"""
import os
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from claude_speak import config

RATE = 16000
FRAME = 1024
SPEECH_RMS = int(os.environ.get("CLAUDE_SPEAK_MIC_THRESHOLD", "500"))
MIN_SPEECH = 0.35        # seconds of sound before a burst counts as speech
END_SILENCE = 0.7        # seconds of quiet that ends a burst
MAX_BURST = 15.0         # hard cap on a single burst
CAPTURE_TIMEOUT = 120.0  # give up if no send phrase arrives

WAKE = os.environ.get("CLAUDE_SPEAK_WAKE", "okay computer")
SEND = os.environ.get("CLAUDE_SPEAK_SEND", "send it")

MODEL_DIR = Path(os.environ.get(
    "CLAUDE_SPEAK_WHISPER_MODELS", Path.home() / ".cache" / "claude-speak"))
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


def strip_phrase(text, phrase):
    """Drop a trailing send phrase from the dictated message."""
    words, target = normalize(text), normalize(phrase)
    spoken = text.split()
    if len(words) >= len(target) and words[-len(target):] == target:
        return " ".join(spoken[: len(spoken) - len(target)]).strip(" ,.")
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

    model = os.environ.get("CLAUDE_SPEAK_STT_MODEL", "gpt-4o-transcribe")
    boundary = f"----claudespeak{time.time_ns()}"
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
    """True while a player is running, so the microphone never hears Claude."""
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
    """A short tone marking wake, send, and cancel."""
    tone = config.STATE / f"cue-{kind}.mp3"
    if not tone.exists():
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        if not os.path.exists(ffmpeg):
            return
        frequency = {"wake": 880, "send": 1320, "cancel": 440}.get(kind, 880)
        subprocess.run(
            [ffmpeg, "-f", "lavfi",
             "-i", f"sine=frequency={frequency}:duration=0.12",
             "-q:a", "9", "-y", str(tone)], capture_output=True)
    if tone.exists():
        subprocess.run(["afplay", str(tone)], capture_output=True)


def set_target(pane):
    config.STATE.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(pane or "")


def get_target():
    try:
        return TARGET.read_text().strip()
    except OSError:
        return ""


def inject(text):
    """Type the message into the Claude Code pane and submit it."""
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
    """Yield one list of frames per speech burst. Silence yields nothing."""
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    process = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-f", "avfoundation", "-i", f":{device}",
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_seconds = FRAME / RATE
    buffer, voiced, quiet = [], 0.0, 0.0
    try:
        while True:
            chunk = process.stdout.read(FRAME * 2)
            if not chunk:
                return
            if speaking():
                buffer, voiced, quiet = [], 0.0, 0.0
                continue
            if rms(chunk) > SPEECH_RMS:
                buffer.append(chunk)
                voiced += frame_seconds
                quiet = 0.0
            elif buffer:
                buffer.append(chunk)
                quiet += frame_seconds
                if quiet >= END_SILENCE or voiced >= MAX_BURST:
                    if voiced >= MIN_SPEECH:
                        yield buffer
                    buffer, voiced, quiet = [], 0.0, 0.0
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

    capturing, captured, started = False, [], 0.0
    try:
        for burst in bursts(device):
            heard = transcribe_local(burst)
            if not heard:
                continue
            config.log(f"heard {heard[:80]!r} capturing={capturing}")

            if not capturing:
                if contains_phrase(heard, WAKE):
                    capturing, captured, started = True, [], time.time()
                    cue("wake")
                continue

            if time.time() - started > CAPTURE_TIMEOUT:
                capturing, captured = False, []
                cue("cancel")
                config.log("capture expired")
                continue

            captured.extend(burst)
            if not contains_phrase(heard, SEND):
                continue

            cue("send")
            capturing = False
            try:
                message = strip_phrase(transcribe_cloud(captured), SEND)
            except Exception as exc:
                config.log(f"transcription failed: {exc}")
                captured = []
                continue
            captured = []
            if message:
                inject(message)
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
