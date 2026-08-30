"""Hands-free input: wake phrase, dictate, send phrase, typed into the session.

Three layers, cheapest first, so nothing expensive runs while the room is quiet:

  1. An energy gate over raw microphone frames. No model, no network. Silence
     costs nothing, which is what makes always-on affordable.
  2. A tiny local whisper over each speech burst, looking only for the wake or
     send phrase. Runs on speech, never on wall-clock time.
  3. An accurate cloud model, or a larger local Whisper model without an API
     key, once on the message you actually dictated.

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

from parley import config, cues, indicator, player, processes, triggers

RATE = 16000
FRAME = 1024
SPEECH_RMS = int(os.environ.get("PARLEY_MIC_THRESHOLD", "500"))
MIN_SPEECH = float(os.environ.get("PARLEY_MIN_SPEECH", "0.10"))
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
CANCEL_ALIASES = tuple(dict.fromkeys((CANCEL, "scratch that")))
STOP_TALKING = os.environ.get("PARLEY_STOP_TALKING", "stop talking")
LOCAL_PROMPT = os.environ.get(
    "PARLEY_LOCAL_PROMPT",
    f"Voice commands: {WAKE}. {SEND}. {CANCEL}. scratch that. "
    f"{STOP_TALKING}.",
)

MODEL_DIR = Path(os.environ.get(
    "PARLEY_WHISPER_MODELS", Path.home() / ".cache" / "parley"))
TINY = MODEL_DIR / "ggml-tiny.en.bin"
TINY_URL = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-tiny.en.bin")
MESSAGE_MODEL = Path(os.environ.get(
    "PARLEY_LOCAL_STT_MODEL", MODEL_DIR / "ggml-base.en.bin"))
MESSAGE_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
    "ggml-base.en.bin"
)

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


def ensure_message_model():
    if MESSAGE_MODEL.exists():
        return True
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    config.log(f"downloading local message model {MESSAGE_MODEL.name}")
    partial = MESSAGE_MODEL.with_suffix(".part")
    result = subprocess.run([
        "curl", "-fL", "--progress-bar", "-o", str(partial), MESSAGE_MODEL_URL
    ])
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        return False
    partial.rename(MESSAGE_MODEL)
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


def wake_targets():
    """Return the configured wake phrase plus narrow local-ASR variants."""
    configured = tuple(normalize(WAKE))
    targets = {configured} if configured else set()
    if configured == ("okay", "computer"):
        targets.update({
            ("ok", "computer"),
            ("okay", "computers"),
            ("ok", "computers"),
        })
    return tuple(sorted(targets, key=len, reverse=True))


def contains_wake(text):
    """Match the wake phrase and common punctuation-safe ASR renderings."""
    words = normalize(text.replace("'", "").replace("’", ""))
    return any(
        words[index:index + len(target)] == list(target)
        for target in wake_targets()
        for index in range(len(words) - len(target) + 1)
    )


def strip_wake_phrases(text):
    """Drop the initial and any repeated wake phrases from cloud text.

    Everything before the first wake is also removed because a barge-in burst
    can begin with words from Parley's own playback.
    """
    spoken = text.split()
    indexed_words = []
    for token_index, token in enumerate(spoken):
        for word in normalize(token.replace("'", "").replace("’", "")):
            indexed_words.append((word, token_index))

    words = [word for word, _ in indexed_words]
    matches = []
    index = 0
    targets = wake_targets()
    while index < len(words):
        target = next((
            candidate for candidate in targets
            if words[index:index + len(candidate)] == list(candidate)
        ), None)
        if target is None:
            index += 1
            continue
        matches.append((index, index + len(target)))
        index += len(target)

    if not matches:
        return text.strip()

    removed = set(range(indexed_words[matches[0][1] - 1][1] + 1))
    for start, end in matches[1:]:
        removed.update(
            token_index for _, token_index in indexed_words[start:end]
        )
    return " ".join(
        token for token_index, token in enumerate(spoken)
        if token_index not in removed
    ).strip(" ,.")


def is_cancel(text):
    """Recognise a standalone discard command without eating dictation.

    A few hesitation or politeness words may precede the command, but ordinary
    language may not. Requiring the cancel phrase at the end also keeps phrases
    such as "scratch that migration" in the dictated message.
    """
    words = normalize(text)
    preamble_words = {"okay", "ok", "please", "um", "uh", "no", "wait"}
    for phrase in CANCEL_ALIASES:
        target = normalize(phrase)
        if not target or len(words) < len(target):
            continue
        if words[-len(target):] != target:
            continue
        if all(word in preamble_words for word in words[:-len(target)]):
            return True
    return False


def is_send(text):
    """Recognise the configured phrase only when it trails the utterance."""
    words, target = normalize(text), normalize(SEND)
    return bool(
        target and len(words) >= len(target)
        and words[-len(target):] == target
    )


def is_stop_talking(text, overlapped):
    """Recognise a narrow set of local-ASR variants only during playback."""
    if not overlapped:
        return False

    # Whisper commonly renders "okay" as "OK" and "computer" as the plural
    # or possessive "computers". Accept those bounded wake-word variants while
    # keeping the action phrase exact; fuzzy-matching "stop talking" itself
    # would make an accidental local control trigger much more likely.
    words = normalize(text.replace("'", "").replace("’", ""))
    wake, action = normalize(WAKE), normalize(STOP_TALKING)
    width = len(wake) + len(action)
    if not wake or not action or len(words) < width:
        return False

    def wake_matches(heard):
        for index, (actual, expected) in enumerate(zip(heard, wake)):
            if actual == expected:
                continue
            if expected == "okay" and actual == "ok":
                continue
            if index == len(wake) - 1 and actual == expected + "s":
                continue
            return False
        return True

    for start in range(len(words) - width + 1):
        candidate = words[start:start + width]
        if wake_matches(candidate[:len(wake)]) and candidate[len(wake):] == action:
            return True
    return False


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
                 "-nt", "-np", "-t", "4", "--prompt", LOCAL_PROMPT],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return " ".join(result.stdout.split())


def transcribe_cloud(frames):
    """Transcribe the final message in the cloud, or locally without a key."""
    import json
    import urllib.request

    key = config.api_key()
    if not key:
        if not ensure_message_model():
            raise RuntimeError("could not download the local message model")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "message.wav"
            write_wav(frames, wav)
            try:
                result = subprocess.run(
                    [whisper_bin(), "-m", str(MESSAGE_MODEL), "-f", str(wav),
                     "-nt", "-np", "-t", "4"],
                    capture_output=True, text=True, timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("local message transcription failed") from exc
        return " ".join(result.stdout.split())

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
    """True while speech is queued, synthesizing, warming up, or playing."""
    if player.microphone_active():
        return False
    if player.active():
        return True
    return bool(processes.owned_pids(config.CUE_PROCESSES, "cue"))


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
    typed = subprocess.run(
        ["tmux", "send-keys", "-t", pane, "-l", text],
        capture_output=True, text=True, check=False,
    )
    if typed.returncode != 0:
        config.log(f"submission failed: could not type into {pane}")
        return False
    time.sleep(0.15)
    submitted = subprocess.run(
        ["tmux", "send-keys", "-t", pane, "Enter"],
        capture_output=True, text=True, check=False,
    )
    if submitted.returncode != 0:
        config.log(f"submission failed: could not press Enter in {pane}")
        return False
    config.log(f"submitted to {pane} chars={len(text)}")
    return True


def listener_state():
    """Current listener state; an absent or invalid marker means ready."""
    if not is_running():
        config.LISTENER_STATE.unlink(missing_ok=True)
        return "ready"
    try:
        state = config.LISTENER_STATE.read_text().strip()
    except OSError:
        return "ready"
    return state if state in {"ready", "capturing", "sending"} else "ready"


def set_listener_state(state):
    """Persist and immediately display a listener state transition."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    config.LISTENER_STATE.write_text(state)
    indicator.refresh()


def rms(chunk):
    usable = len(chunk) // 2 * 2
    if usable < 2:
        return 0
    samples = struct.unpack(f"{usable // 2}h", chunk[:usable])
    return int((sum(s * s for s in samples) / len(samples)) ** 0.5)


def bursts(device="0", reserve_output=True):
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
    provisional_turn = False
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
                # A reply can finish synthesizing after the user starts the
                # wake utterance but before tiny Whisper recognizes it. Claim
                # the microphone on the first voiced frame so that reply stays
                # behind the same gate as an established dictation turn. Do
                # not pause output that was already audible: that remains the
                # validated wake phrase's job, or Parley's own voice would
                # repeatedly pause itself.
                if (not buffer and reserve_output
                        and not player.output_playing()):
                    player.pause()
                    provisional_turn = True
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
                        # Ownership transfers to run(), which either promotes
                        # it after a wake or promptly releases a non-command.
                        provisional_turn = False
                        yield buffer, overlapped
                    elif voiced >= 0.05:
                        config.log(
                            f"gate ignored short audio voiced={voiced:.2f}s")
                    if provisional_turn:
                        player.resume()
                        provisional_turn = False
                    buffer, voiced, quiet, overlapped = [], 0.0, 0.0, False
    finally:
        if provisional_turn:
            player.resume()
        process.terminate()


def run(device="0"):
    """The listening loop. Runs until killed."""
    if not whisper_bin():
        raise SystemExit(
            "whisper-cli not found. Install it with: brew install whisper-cpp")
    if not ensure_model():
        raise SystemExit("could not download the wake-word model")

    config.STATE.mkdir(parents=True, exist_ok=True)
    ownership = processes.claim(LISTEN_PID, os.getpid(), "listener")
    set_listener_state("ready")
    config.log(f"listening wake={WAKE!r} send={SEND!r} device={device}")
    personalized_active = triggers.enrolled()
    if personalized_active:
        config.log("personalized triggers active")
    indicator.ensure()

    capturing, captured, started, last_heard = False, [], 0.0, 0.0
    last_indicator = time.time()

    def finish(frames):
        set_listener_state("sending")
        cue("send")
        try:
            try:
                spoken = transcribe_cloud(frames)
            except Exception as exc:
                config.log(f"transcription failed: {exc}")
                return
            message = strip_phrase(strip_wake_phrases(spoken), SEND)
            config.log(f"message {message[:80]!r}")
            if message:
                inject(message)
        finally:
            player.resume()
            set_listener_state("ready")

    try:
        for burst, overlapped in bursts(device):
            now = time.time()

            # Newly launched Agent Deck sessions get the badge without a
            # listener restart. The displayed text itself checks this process
            # on every tmux status refresh, so a crash cannot leave a false ON.
            if now - last_indicator >= 5:
                indicator.ensure()
                last_indicator = now

            if capturing:
                # Two independent stops. Silence means you walked away or the
                # send phrase was never recognised. The hard cap exists so a
                # listener wedged open cannot deliver a colossal transcript.
                if now - last_heard > SILENCE_TIMEOUT:
                    capturing, captured = False, []
                    set_listener_state("ready")
                    cue("cancel")
                    config.log("discarded: silent too long")
                    player.resume()
                    continue
                if now - started > HARD_STOP:
                    capturing, captured = False, []
                    set_listener_state("ready")
                    cue("cancel")
                    config.log("discarded: hard stop")
                    player.resume()
                    continue
                if len(captured) * FRAME / RATE > HARD_STOP:
                    capturing, captured = False, []
                    set_listener_state("ready")
                    cue("cancel")
                    config.log("discarded: too much audio")
                    player.resume()
                    continue

            if burst is None:
                continue

            allowed = ("wake", "send", "cancel", "stop") if capturing else (
                "wake", "stop")
            personalized = None
            if personalized_active:
                personalized, score, threshold = triggers.match(burst, allowed)
                if personalized:
                    config.log(
                        f"personalized trigger={personalized} score={score:.3f} "
                        f"threshold={threshold:.3f}")

            voiced_seconds = sum(
                rms(frame) > SPEECH_RMS for frame in burst) * FRAME / RATE
            if (personalized_active and voiced_seconds < 0.16
                    and not personalized):
                config.log(
                    f"personalized short candidate rejected "
                    f"voiced={voiced_seconds:.2f}s")
                if not capturing:
                    player.resume()
                continue

            heard = transcribe_local(burst)
            if not heard and not personalized:
                config.log(f"local transcription empty frames={len(burst)}")
                if not capturing:
                    player.resume()
                continue
            if heard:
                config.log(f"heard {heard[:80]!r} capturing={capturing} "
                           f"overlapped={overlapped}")

            # This is a voice-control command, not dictation. It is handled
            # before capture state, never reaches cloud transcription, emits
            # one local confirmation sound, and cannot become a chat message.
            if (is_stop_talking(heard, overlapped or speaking())
                    or (personalized == "stop" and (overlapped or speaking()))):
                player.skip()
                capturing, captured = False, []
                set_listener_state("ready")
                cue("stop")
                config.log("voice-control: skipped current speech block")
                continue

            if not capturing:
                if personalized != "wake" and not contains_wake(heard):
                    player.resume()
                    continue
                # Barge-in pauses rather than discards. The microphone marker
                # is also set when nothing is speaking yet, making dictation an
                # exclusive turn that all newly queued speech must wait behind.
                # The burst tag reflects whether playback was active while its
                # frames arrived. Recheck now as well: synthesis can finish and
                # playback can start while local transcription is running.
                was_speaking = overlapped or speaking()
                player.pause()
                if was_speaking:
                    config.log("barged in: paused active or pending playback")
                # Keep this burst — you normally keep talking in the same breath
                # as the wake phrase. strip_leading drops everything up to and
                # including it, which also removes any of the agent's words.
                capturing, captured = True, list(burst)
                set_listener_state("capturing")
                started = last_heard = now
                cue("wake")
                if is_send(heard):
                    capturing, captured = False, []
                    finish(list(burst))
                continue

            if personalized == "cancel" or is_cancel(heard):
                capturing, captured = False, []
                set_listener_state("ready")
                cue("cancel")
                config.log("discarded")
                player.resume()
                continue

            if personalized == "wake" or contains_wake(heard):
                cue("wake")
                config.log("wake repeated: capture still active")

            last_heard = now
            captured.extend(burst)
            if personalized != "send" and not is_send(heard):
                continue
            capturing = False
            frames, captured = captured, []
            finish(frames)
    finally:
        # Also releases a provisional pre-recognition turn if local
        # classification failed or the listener exited between yield/handling.
        if capturing or player.microphone_active():
            player.resume()
        processes.release(ownership)
        config.LISTENER_STATE.unlink(missing_ok=True)


def is_running():
    return processes.owned_pid(LISTEN_PID, "listener")


def stop():
    pid = is_running()
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    processes.clear(LISTEN_PID)
    config.LISTENER_STATE.unlink(missing_ok=True)
    indicator.refresh()
    player.resume()
    return bool(pid)
