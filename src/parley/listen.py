"""Hands-free input: wake phrase, dictate, send phrase, typed into the session.

Three layers, cheapest first, so nothing expensive runs while the room is quiet:

  1. A speech gate over raw microphone frames (gate.py): an adaptive noise
     floor plus WebRTC's tiny VAD. No network. Silence costs nothing, which
     is what makes always-on affordable.
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
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from parley import audio, config, cues, echo, gate, indicator, player, triggers
from parley.gate import rms  # noqa: F401  (re-exported for callers and tests)

RATE = gate.RATE
FRAME = gate.FRAME
END_SILENCE = gate.END_SILENCE
INTERRUPT_SILENCE = gate.INTERRUPT_SILENCE
MAX_BURST = gate.MAX_BURST
# Personalized near-misses within this factor of the threshold are logged.
NEAR_MISS = 1.35
# Opt-in local recording of bursts for `parley debug-triggers`. Off by default.
RECORD_BURSTS = os.environ.get("PARLEY_RECORD_BURSTS", "") not in {"", "0"}
BURSTS_DIR = config.STATE / "bursts"
RECORD_LIMIT = int(os.environ.get("PARLEY_RECORD_LIMIT", "300"))
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
FOLLOW_ACTIVE = "follow-active-deck-pane"


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


def stop_variants():
    """Word sequences of the stop command, for the playback echo check."""
    action = normalize(STOP_TALKING)
    return tuple(tuple(list(wake) + action) for wake in wake_targets()) + (
        tuple(action),)


def burst_summary(burst):
    describe = getattr(burst, "describe", None)
    return describe() if describe else f"frames={len(burst)}"


def _tokens(text):
    """(original token, normalized form) pairs, dropping bare punctuation."""
    pairs = []
    for token in (text or "").split():
        key = "".join(normalize(token.replace("'", "").replace("’", "")))
        if key:
            pairs.append((token, key))
    return pairs


def repair_dropped_tail(message, final_heard):
    """Restore words the cloud transcript dropped just before the send phrase.

    The final burst is the one local Whisper recognised the send phrase in, so
    its local transcript is known to be decent. If the words right before that
    send phrase are missing from the end of the cloud message, the cloud model
    dropped them: on 2026-11-01 "so I just did something now. check on it.
    send it." was submitted as "so I just did something now". Those local
    words are appended. Small wording differences are left alone; only a
    clear run of at least two missing words, after an anchor the two
    transcripts agree on, is restored.
    """
    import difflib

    tail_text = strip_phrase(strip_wake_phrases(final_heard or ""), SEND)
    tail = _tokens(tail_text)
    cloud = _tokens(message)
    if len(tail) < 2 or not cloud:
        return message
    matcher = difflib.SequenceMatcher(
        None, [key for _, key in cloud], [key for _, key in tail],
        autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    if not blocks:
        return message
    last = blocks[-1]
    # The anchor must be the end of the cloud text; otherwise the cloud has
    # more after it and the local transcript is the one that diverged.
    if last.a + last.size != len(cloud) or last.size < 2:
        return message
    missing = tail[last.b + last.size:]
    if len(missing) < 2:
        return message
    restored = " ".join(token for token, _ in missing).strip(" ,")
    base = message.rstrip()
    joiner = " " if base.endswith(",") else ". "
    if not base.endswith(","):
        restored = restored[:1].upper() + restored[1:]
        base = base.rstrip(".!?")
    config.log(f"restored {len(missing)} words the cloud transcript dropped")
    return f"{base}{joiner}{restored}".strip(" ,.")


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


def set_follow_active(enabled):
    """Choose whether routing follows the active human Agent Deck client."""
    marker = config.STATE / FOLLOW_ACTIVE
    if enabled:
        config.private_write(marker, "1")
    else:
        marker.unlink(missing_ok=True)


def follows_active():
    return (config.STATE / FOLLOW_ACTIVE).exists()


def is_agent_deck_pane(pane):
    """Whether a pane belongs to an Agent Deck-managed tmux session."""
    if not pane:
        return False
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip().startswith("agentdeck_")


# A local pane running one of these is a window onto another machine. Its
# client activity is the user typing over there, not at a local session.
REMOTE_CLIENTS = {"mosh", "mosh-client", "ssh", "et"}


def local_deck_focus():
    """(activity, pane) of the most recently active real terminal client.

    Agent Deck also owns several control-mode clients. They have no TTY and
    their activity reflects background rendering, so they must never win input
    routing over the human's attached terminal.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-clients", "-F",
             "#{client_tty}\t#{pane_id}\t#{client_activity}\t#{client_session}"
             "\t#{pane_current_command}"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0, ""
    if result.returncode != 0:
        return 0, ""
    candidates = []
    for line in result.stdout.splitlines():
        fields = line.split("\t", 4)
        if len(fields) < 4:
            continue
        tty, pane, activity, session = fields[:4]
        command = fields[4].strip() if len(fields) > 4 else ""
        if not tty or not pane or not session.startswith("agentdeck_"):
            continue
        if command in REMOTE_CLIENTS:
            continue
        try:
            candidates.append((int(activity), pane))
        except ValueError:
            continue
    return max(candidates, default=(0, ""))


def active_deck_target():
    """The pane shown by the most recently active real terminal client.

    With a remote relay, remotes that armed the listener report their own
    focus over the relay link, and the most recent client on any machine wins.
    """
    from parley import relay

    local = local_deck_focus()
    if not relay.remote_hosts():
        return local[1]
    remote = relay.remote_focus()
    if remote is None:
        return ""  # a remote's report is overdue; keep the current target
    candidates = [local] if local[1] else []
    candidates.extend(remote)
    return max(candidates, default=(0, ""))[1]


def sync_target():
    """Follow Deck focus when enabled, retaining the armed pane as fallback."""
    stored = get_target()
    if not follows_active():
        return stored
    active = active_deck_target()
    if not active or active == stored:
        return stored
    set_target(active)
    config.log(f"listener target followed active deck pane {active}")
    indicator.refresh()
    return active


PASTE_BUFFER = "parley-dictation"


def target_label(target):
    """A human name for a dictation target, local or remote."""
    from parley import relay

    host, pane = relay.split_target(target)
    if host:
        return relay.target_label() or (
            f"{pane} on {relay.short_host(host)}" if pane else "")
    return indicator.session_label(pane) if pane else ""


def inject(text):
    """Paste the message into the session's pane and submit it.

    A pane on another machine is reached through the relay link; see
    :func:`paste_into` for how the text arrives.
    """
    from parley import relay

    target = sync_target()
    if not target or not text:
        return False
    text = " ".join(text.split())
    host, pane = relay.split_target(target)
    if host:
        if relay.inject(host, pane, text):
            config.log(f"submitted to {pane} on {host} chars={len(text)}")
            return True
        config.log(f"submission failed: relay to {host} could not reach {pane}")
        return False
    return paste_into(pane, text)


def paste_into(pane, text):
    """Paste text into a local tmux pane and submit it.

    The message goes through a tmux paste buffer as one bracketed paste rather
    than through ``send-keys -l``. Typing a long dictation key by key into a
    busy TUI lost its head: Claude Code received 1360 characters as a stream of
    keystrokes while it was still redrawing and kept only the tail. A
    bracketed paste arrives as a single paste event, so the agent's input
    either receives the whole message or none of it.
    """
    if not pane or not text:
        return False
    text = " ".join(text.split())
    buffer = f"{PASTE_BUFFER}-{os.getpid()}"
    loaded = subprocess.run(
        ["tmux", "load-buffer", "-b", buffer, "-"],
        input=text, capture_output=True, text=True, check=False,
    )
    if loaded.returncode != 0:
        config.log(f"submission failed: could not stage text for {pane}")
        return False
    pasted = subprocess.run(
        ["tmux", "paste-buffer", "-d", "-p", "-r", "-b", buffer, "-t", pane],
        capture_output=True, text=True, check=False,
    )
    if pasted.returncode != 0:
        subprocess.run(["tmux", "delete-buffer", "-b", buffer],
                       capture_output=True, text=True, check=False)
        config.log(f"submission failed: could not paste into {pane}")
        return False
    # Let the agent finish ingesting the paste before Enter, longer for long
    # messages; an Enter that overtakes the paste would submit a fragment.
    time.sleep(min(1.0, 0.25 + len(text) / 4000))
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
    try:
        state = config.LISTENER_STATE.read_text().strip()
    except OSError:
        return "ready"
    return state if state in {
        "ready", "capturing", "sending", "reconnecting"
    } else "ready"


def set_listener_state(state):
    """Persist and immediately display a listener state transition."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    config.LISTENER_STATE.write_text(state)
    indicator.refresh()


def _stop_capture(process):
    """Terminate and reap an FFmpeg capture process without hanging shutdown."""
    process.terminate()
    wait = getattr(process, "wait", None)
    if wait is None:
        return
    try:
        wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        wait()


def bursts(device="default", reserve_output=True, reconnect=False,
           gate_factory=None):
    """Yield (frames, was_playing) per speech burst, plus idle heartbeats.

    Bursts are still collected while the agent is speaking. Dropping them was
    a real bug: you naturally interrupt, and the wake phrase you said over the
    reply was silently discarded. The burst is tagged instead, so the caller
    can require the wake phrase before trusting audio that may contain the
    agent's own voice.

    Bursts too short to be a phrase are yielded too, as a gate.Burst with
    ``short`` set: they must not wake anything, but mid-dictation they are the
    quiet start or end of a sentence and belong in the message.

    A (None, False) heartbeat is yielded about once a second so timeouts can
    fire during silence, when no burst would ever arrive.
    """
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    frame_seconds = FRAME / RATE
    make_gate = gate_factory or gate.SpeechGate
    # The noise floor survives a microphone reopen; the room has not changed.
    speech_gate = make_gate()
    while True:
        process = None
        provisional_turn = False
        capture_ready = False
        since_beat = 0.0
        playing, since_playing_check = False, 1.0
        selected_default = (
            audio.default_input_device() if device == "default" else None
        )
        try:
            process = subprocess.Popen(
                [ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-f", "avfoundation", "-i", f":{device}",
                 "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            while True:
                chunk = process.stdout.read(FRAME * 2)
                if not chunk:
                    break
                if reconnect and not capture_ready:
                    capture_ready = True
                    yield None, "capture-ready"
                since_beat += frame_seconds
                if since_beat >= 1.0:
                    since_beat = 0.0
                    if selected_default is not None:
                        current_default = audio.default_input_device()
                        if (current_default is not None
                                and current_default != selected_default):
                            config.log(
                                "default input changed; reopening microphone")
                            break
                    yield None, False
                # Playback state costs a few file reads. Ten checks a second
                # are plenty while idle; inside a burst check every frame.
                since_playing_check += frame_seconds
                if since_playing_check >= 0.1 or speech_gate.open:
                    since_playing_check = 0.0
                    playing = speaking()
                event, burst = speech_gate.feed(chunk, playing)
                if event == "start":
                    # A reply can finish synthesizing after the user starts
                    # the wake utterance but before tiny Whisper recognizes
                    # it. Claim the microphone on the first voiced frame so
                    # that reply stays behind the same gate as an established
                    # dictation turn. Do not pause output that was already
                    # audible: that remains the validated wake phrase's job,
                    # or Parley's own voice would repeatedly pause itself.
                    if reserve_output and not player.output_playing():
                        player.pause()
                        provisional_turn = True
                    continue
                if burst is None:
                    continue
                if burst.short and burst.voiced >= gate.SHORT_SPEECH:
                    config.log(f"gate short audio {burst.describe()}")
                provisional_turn = False
                yield burst, burst.overlapped
        except OSError as exc:
            if not reconnect:
                raise
            config.log(f"microphone unavailable; retrying: {exc}")
        finally:
            if provisional_turn:
                player.resume()
            if process is not None:
                _stop_capture(process)
        if not reconnect:
            return
        yield None, "capture-reset"
        time.sleep(1)


def record_burst(frames, label):
    """Keep a burst for offline evaluation, only when explicitly enabled.

    Recordings stay in a private local directory, capped to the newest
    RECORD_LIMIT files. Rename a file's label to correct it before running
    ``parley debug-triggers``.
    """
    if not RECORD_BURSTS or not frames:
        return
    try:
        config.private_directory(BURSTS_DIR)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = BURSTS_DIR / f"{stamp}-{time.time_ns() % 10**6:06d}-{label}.wav"
        write_wav(frames, path)
        os.chmod(path, 0o600)
        for old in sorted(BURSTS_DIR.glob("*.wav"))[:-RECORD_LIMIT]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        config.log(f"burst recording failed: {exc}")


def run(device="default", recover_devices=False):
    """The listening loop. Runs until killed."""
    if not whisper_bin():
        raise SystemExit(
            "whisper-cli not found. Install it with: brew install whisper-cpp")
    if not ensure_model():
        raise SystemExit("could not download the wake-word model")

    config.STATE.mkdir(parents=True, exist_ok=True)
    LISTEN_PID.write_text(str(os.getpid()))
    set_listener_state("ready")
    sync_target()
    config.log(f"listening wake={WAKE!r} send={SEND!r} device={device}")
    personalized_active = triggers.enrolled()
    if personalized_active:
        config.log("personalized triggers active")
    elif triggers.stale():
        config.log("personalized triggers off: profile predates the current "
                   "matcher; run parley enroll")
    vad = gate.default_vad()
    config.log(f"speech gate vad={'webrtc' if vad else 'energy-only'} "
               f"min_rms={gate.MIN_RMS} open_ratio={gate.OPEN_RATIO}")
    indicator.ensure()
    from parley import relay

    # Follows focus on, and mirrors state to, remotes that armed this listener.
    link = relay.Link()
    link.start()

    capturing, captured, started, last_heard = False, [], 0.0, 0.0
    last_indicator = time.time()
    last_target_sync = 0.0

    def finish(frames, final_heard=""):
        set_listener_state("sending")
        cue("send")
        try:
            try:
                spoken = transcribe_cloud(frames)
            except Exception as exc:
                config.log(f"transcription failed: {exc}")
                return
            config.log(f"transcript words={len(spoken.split())}")
            message = strip_phrase(strip_wake_phrases(spoken), SEND)
            # Only the burst that ended the dictation is compared: its local
            # transcript is what told us the message was over.
            if final_heard:
                message = repair_dropped_tail(message, final_heard)
            config.log(f"message {message[:80]!r}")
            if message:
                inject(message)
        finally:
            player.resume()
            set_listener_state("ready")

    try:
        source = (
            bursts(device, reconnect=True) if recover_devices else bursts(device)
        )
        for burst, overlapped in source:
            now = time.time()

            if burst is None and overlapped == "capture-reset":
                if capturing:
                    config.log(
                        "discarded active dictation after input device loss")
                capturing, captured = False, []
                player.resume()
                set_listener_state("reconnecting")
                continue
            if burst is None and overlapped == "capture-ready":
                set_listener_state("ready")
                continue

            if now - last_target_sync >= 1:
                sync_target()
                last_target_sync = now

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

            # Too brief to be a command. Outside capture it is a click or a
            # breath. Inside capture it is the soft start or end of a
            # sentence, and dropping it lost words ("send it" became "end it").
            if getattr(burst, "short", False):
                if capturing:
                    captured.extend(burst)
                    last_heard = now
                else:
                    player.resume()
                continue

            allowed = ("wake", "send", "cancel", "stop") if capturing else (
                "wake", "stop")
            # The personalized matcher only ever adds recall: a confident
            # match is a hit, anything else goes on to Whisper unchanged.
            personalized = None
            if personalized_active:
                personalized, score, threshold = triggers.match(burst, allowed)
                if personalized == "wake" and overlapped:
                    # Acoustic templates cannot tell Parley's voice saying the
                    # wake phrase from yours; Whisper plus the echo check can.
                    config.log(
                        f"personalized wake ignored during playback "
                        f"score={score:.3f} threshold={threshold:.3f}")
                    personalized = None
                elif personalized:
                    config.log(
                        f"personalized trigger={personalized} score={score:.3f} "
                        f"threshold={threshold:.3f}")
                elif (threshold is not None and score != float("inf")
                        and score <= threshold * NEAR_MISS):
                    config.log(
                        f"personalized near-miss score={score:.3f} "
                        f"threshold={threshold:.3f} {burst_summary(burst)}")

            heard = transcribe_local(burst)
            if not heard and not personalized:
                config.log(f"local transcription empty frames={len(burst)}")
                record_burst(burst, "empty")
                if not capturing:
                    player.resume()
                continue
            if heard:
                config.log(f"heard {heard[:80]!r} capturing={capturing} "
                           f"overlapped={overlapped}")

            playing_now = overlapped or speaking()
            recent = echo.recent_texts() if playing_now or overlapped else []

            # This is a voice-control command, not dictation. It is handled
            # before capture state, never reaches cloud transcription, emits
            # one local confirmation sound, and cannot become a chat message.
            stop_heard = is_stop_talking(heard, playing_now)
            if stop_heard and recent and echo.is_echo(
                    heard, stop_variants(), recent):
                config.log("ignored stop phrase echoed from playback")
                stop_heard = False
            if stop_heard or (personalized == "stop" and playing_now):
                record_burst(burst, "stop")
                player.skip()
                capturing, captured = False, []
                set_listener_state("ready")
                cue("stop")
                config.log("voice-control: skipped current speech block")
                continue

            woke = personalized == "wake" or contains_wake(heard)
            if (woke and personalized != "wake" and recent
                    and echo.is_echo(heard, wake_targets(), recent)):
                config.log("ignored wake phrase echoed from playback")
                woke = False

            if not capturing:
                record_burst(burst, "wake" if woke else "none")
                if not woke:
                    player.resume()
                    continue
                # Barge-in pauses rather than discards. The microphone marker
                # is also set when nothing is speaking yet, making dictation an
                # exclusive turn that all newly queued speech must wait behind.
                # The burst tag reflects whether playback was active while its
                # frames arrived. Recheck now as well: synthesis can finish and
                # playback can start while local transcription is running.
                was_speaking = playing_now
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
                    finish(list(burst), heard)
                continue

            if personalized == "cancel" or is_cancel(heard):
                record_burst(burst, "cancel")
                capturing, captured = False, []
                set_listener_state("ready")
                cue("cancel")
                config.log("discarded")
                player.resume()
                continue

            if woke:
                cue("wake")
                config.log("wake repeated: capture still active")

            last_heard = now
            captured.extend(burst)
            if personalized != "send" and not is_send(heard):
                continue
            record_burst(burst, "send")
            capturing = False
            frames, captured = captured, []
            finish(frames, heard)
    finally:
        # Also releases a provisional pre-recognition turn if local
        # classification failed or the listener exited between yield/handling.
        link.stop()
        if capturing or player.microphone_active():
            player.resume()
        LISTEN_PID.unlink(missing_ok=True)
        config.LISTENER_STATE.unlink(missing_ok=True)


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
    config.LISTENER_STATE.unlink(missing_ok=True)
    from parley import relay

    relay.clear_hosts()
    indicator.refresh()
    player.resume()
    return bool(pid)
