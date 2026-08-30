"""A spool directory drained by one player, so nothing ever overlaps.

Utterances are files in a queue directory, named by nanosecond timestamp so
sorting is arrival order. Any number of processes may enqueue. Exactly one
drains, chosen by an exclusive flock — the rest return immediately and let the
holder pick up their work.
"""
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from parley import config, processes, runtime
from parley.tts import synthesize

SENTENCE_BOUNDARY = re.compile(r'''[.!?]["'\u2019\u201d)\]]*(?=\s)''')
RESUME_REWIND_SECONDS = 0.25
ERROR_PROVIDERS = frozenset({"openai", "elevenlabs", "macos"})


class PlaybackError(RuntimeError):
    """A local audio process failed without an intentional interruption."""


def speech_error(snapshot=None):
    """The last allow-listed speech failure from the shared runtime snapshot."""
    snapshot = snapshot or runtime.snapshot()
    for error in reversed(snapshot["errors"]):
        if error["code"] not in {"synthesis_failed", "playback_failed"}:
            continue
        return {
            "provider": error.get("provider", "unknown"),
            "stage": (
                "synthesis" if error["code"] == "synthesis_failed"
                else "playback"
            ),
            "policy": "drop-after-one-attempt",
            "retry": "manual",
        }
    return None


def speech_error_message(error=None):
    """Actionable CLI text built only from allow-listed operational fields."""
    error = error or speech_error()
    if not error:
        return ""
    provider = error["provider"]
    stage = error["stage"]
    if stage == "playback":
        guidance = "check afplay and the selected audio output"
    elif provider == "macos":
        guidance = "check that the selected macOS voice is installed"
    else:
        guidance = "check provider credentials, voice/model, and network"
    return (
        f"parley: speech failed (provider={provider}, stage={stage}); "
        "the failed block was dropped after one attempt. "
        f"To retry, {guidance}, then run parley say --wait again."
    )


def _safe_provider(provider):
    return provider if provider in ERROR_PROVIDERS else "unknown"


def _record_speech_error(writer, provider, stage, indicate):
    """Publish and indicate a failure without exception or content fields."""
    provider = _safe_provider(provider)
    if stage == "synthesis":
        runtime.set_speech(writer, synthesis="degraded", playback="idle")
        code, runtime_stage = "synthesis_failed", "synthesize"
    else:
        runtime.set_speech(writer, synthesis="idle", playback="degraded")
        code, runtime_stage = "playback_failed", "play"
    runtime.record_error(
        writer, code, "speech", runtime_stage, provider,
    )
    config.log(
        f"speech failure provider={provider} stage={stage} "
        "policy=drop-after-one-attempt retry=manual"
    )
    from parley import indicator

    indicator.refresh()
    if indicate:
        from parley import cues

        try:
            # Detached drain exits with os._exit(), so the short cue must be
            # reaped and its ownership released before this function returns.
            cues.play("error")
        except Exception:
            # The persistent visual/status indication remains available when
            # local cue playback is itself unavailable.
            pass


def _clear_speech_error(writer):
    runtime.clear_errors(writer, {"synthesis_failed", "playback_failed"})
    from parley import indicator

    indicator.refresh()


def chunks(text, limit=None):
    """Split complete prose into bounded, speakable provider requests."""
    remaining = (text or "").strip()
    limit = max(1, limit or config.MAX_CHARS)
    while len(remaining) > limit:
        window = remaining[:limit + 1]
        sentence_ends = [
            match.end() for match in SENTENCE_BOUNDARY.finditer(window)
            if match.end() <= limit
        ]
        if sentence_ends:
            cut = sentence_ends[-1]
        else:
            whitespace = [
                match.start() for match in re.finditer(r"\s+", window)
                if 0 < match.start() <= limit
            ]
            cut = whitespace[-1] if whitespace else limit
        yield remaining[:cut].strip()
        remaining = remaining[cut:].lstrip()
    if remaining:
        yield remaining


def enqueue(text, voice=None, model=None):
    text = (text or "").strip()
    if not text:
        return False
    config.private_directory(config.QUEUE)
    provider = config.provider()
    voice = voice or config.active_voice()
    model = model or config.active_model()
    timestamp = time.time_ns()
    for index, chunk in enumerate(chunks(text)):
        item = {
            "text": chunk,
            "provider": provider,
            "voice": voice,
            "model": model,
        }
        name = f"{timestamp:020d}-{os.getpid()}-{index:04d}.json"
        # Write then rename so a drainer never sees a half-written item.
        tmp = config.QUEUE / (name + ".tmp")
        config.private_write(tmp, json.dumps(item))
        tmp.rename(config.QUEUE / name)
    from parley import indicator

    runtime.refresh_queue()
    indicator.refresh()
    return True


def _wake_output():
    """Bluetooth outputs swallow the first moment while they switch profiles."""
    config.private_directory(config.STATE)
    if not config.SILENCE.exists():
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        if not os.path.exists(ffmpeg):
            return
        subprocess.run(
            [ffmpeg, "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", "0.6", "-q:a", "9", "-y", str(config.SILENCE)],
            capture_output=True,
        )
    if config.SILENCE.exists():
        os.chmod(config.SILENCE, 0o600)
        subprocess.run(["afplay", str(config.SILENCE)], capture_output=True)


def _pause_token():
    try:
        return config.PAUSE.read_text().strip()
    except OSError:
        return ""


def _remaining_audio(path, offset):
    """Decode the unplayed tail so afplay can restart at a checkpoint."""
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not os.path.exists(ffmpeg):
        raise RuntimeError("ffmpeg is required to resume interrupted speech")
    fd, remainder = tempfile.mkstemp(suffix=".wav", dir=config.STATE)
    os.close(fd)
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss",
         f"{offset:.3f}", "-i", path, "-c:a", "pcm_s16le", "-y", remainder],
        capture_output=True,
    )
    if result.returncode:
        os.unlink(remainder)
        detail = result.stderr.decode(errors="replace")
        raise RuntimeError(
            f"could not prepare resumed speech: {detail}"
        )
    return remainder


def play(audio, interrupt=None, skip=None):
    config.private_directory(config.STATE)
    proc = None
    ownership = None
    resumed_paths = []
    offset = 0.0
    suffix = ".aiff" if audio.startswith(b"FORM") else ".mp3"
    with tempfile.NamedTemporaryFile(
        suffix=suffix, delete=False, dir=config.STATE
    ) as fh:
        fh.write(audio)
        path = fh.name
    try:
        while True:
            if skip is not None and _skip_token() != skip:
                return True
            if not _wait_for_microphone(interrupt):
                return False
            playback_path = path
            if offset:
                playback_path = _remaining_audio(path, offset)
                resumed_paths.append(playback_path)
            pause_token = _pause_token()
            started = time.monotonic()
            proc = subprocess.Popen(["afplay", playback_path])
            ownership = processes.claim(config.SPEECH_PID, proc.pid, "speech")
            # A turn can open between the gate check and publishing the pid.
            # pause() changes its token before terminating playback, so the
            # exit is distinguishable from both natural completion and stop().
            if microphone_active() and _pause_token() == pause_token:
                config.private_write(config.PAUSE, str(time.time_ns()))
                proc.terminate()
            if interrupt is not None and _interrupt_token() != interrupt:
                proc.terminate()
            if skip is not None and _skip_token() != skip:
                proc.terminate()
            returncode = proc.wait()
            elapsed = max(0.0, time.monotonic() - started)
            processes.release(ownership)
            ownership = None
            if interrupt is not None and _interrupt_token() != interrupt:
                return False
            if skip is not None and _skip_token() != skip:
                return True
            if returncode == 0:
                return True
            if _pause_token() == pause_token:
                raise PlaybackError(f"afplay exited with status {returncode}")
            offset = max(
                0.001, offset + elapsed - RESUME_REWIND_SECONDS)
            config.log(f"speech checkpointed at {offset:.2f}s")
            if not _wait_for_microphone(interrupt):
                return False
            config.log(f"speech restarting at {offset:.2f}s after microphone turn")
    finally:
        processes.release(ownership)
        from parley import indicator

        indicator.refresh()
        try:
            os.unlink(path)
        except OSError:
            pass
        for resumed_path in resumed_paths:
            try:
                os.unlink(resumed_path)
            except OSError:
                pass


def _pending():
    config.private_directory(config.QUEUE)
    return sorted(p for p in config.QUEUE.iterdir() if p.suffix == ".json")


def _interrupt_token():
    try:
        return config.INTERRUPT.read_text().strip()
    except OSError:
        return ""


def _skip_token():
    try:
        return config.SKIP.read_text().strip()
    except OSError:
        return ""


def _pid_alive(path):
    return bool(processes.owned_pid(path))


def microphone_active():
    """True only while the listener process owning the microphone is alive."""
    if _pid_alive(config.MIC_TURN):
        return True
    was_marked = config.MIC_TURN.exists()
    processes.clear(config.MIC_TURN)
    if was_marked:
        config.log("recovered stale microphone turn; speech released")
    return False


def output_playing():
    """True only after an audio process has actually been launched."""
    return _pid_alive(config.SPEECH_PID)


def _wait_for_microphone(interrupt=None):
    """Hold spoken output behind an exclusive microphone turn."""
    while microphone_active():
        if interrupt is not None and _interrupt_token() != interrupt:
            return False
        time.sleep(0.05)
    return interrupt is None or _interrupt_token() == interrupt


def _signal_speech(sig):
    return processes.send_signal(config.SPEECH_PID, sig, "speech")


def pause():
    """Give the microphone the floor while preserving current and queued speech."""
    processes.claim(config.MIC_TURN, os.getpid(), "microphone-turn")
    config.private_write(config.PAUSE, str(time.time_ns()))
    paused = _signal_speech(signal.SIGTERM)
    config.log(
        f"microphone turn started speech={'checkpointing' if paused else 'waiting'}")
    return paused


def resume():
    """Release the microphone turn so checkpointed speech can restart."""
    had_turn = config.MIC_TURN.exists()
    processes.clear(config.MIC_TURN)
    if not had_turn:
        return False
    config.log("microphone turn ended speech=released")
    return True


def active():
    """True from queued/synthesizing speech through the end of playback."""
    if microphone_active():
        return False
    return bool(_pending()) or _pid_alive(config.DRAIN_PID)


def drain():
    """Play everything queued, in order. Returns immediately if already draining."""
    lock = config.private_open(config.LOCK)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False

    writer = runtime.claim("speech")
    runtime.set_speech(writer, synthesis="idle", playback="idle")
    woke = False
    spoke = False
    last_failure = None
    attempted = 0
    failures = 0
    interrupt = _interrupt_token()
    ownership = processes.claim(config.DRAIN_PID, os.getpid(), "drainer")
    try:
        while True:
            items = _pending()
            if not items:
                # An item enqueued just now would otherwise be stranded with no
                # drainer, so look once more before giving up the lock.
                time.sleep(0.3)
                items = _pending()
                if not items:
                    # Marks the end of the whole spoken response, so you know
                    # the floor is yours again without watching the terminal.
                    if spoke:
                        from parley import cues

                        cues.play("done")
                    if spoke and failures == 0:
                        _clear_speech_error(writer)
                    return attempted == 0 or failures < attempted
            item = items[0]
            try:
                job = json.loads(item.read_text())
            except (OSError, ValueError):
                item.unlink(missing_ok=True)
                continue
            item.unlink(missing_ok=True)
            runtime.refresh_queue()
            attempted += 1
            skip = _skip_token()
            job_provider = job.get("provider", "unknown")
            safe_provider = (
                job_provider
                if isinstance(job_provider, str)
                and job_provider in runtime.ACTIVE_PROVIDERS
                else "unknown"
            )
            try:
                last_failure = None
                runtime.set_provider(writer, safe_provider)
                runtime.set_speech(
                    writer, synthesis="active", playback="idle")
                started = time.time()
                with runtime.writer_context(writer):
                    audio = synthesize(
                        job["text"], job.get("voice"), job.get("model"),
                        job.get("provider"),
                    )
                config.log(
                    f"spoke {len(job['text'])}c "
                    f"synth={time.time() - started:.1f}s {len(audio)}b"
                )
                runtime.set_speech(
                    writer, synthesis="idle", playback="active")
            except Exception:
                _record_speech_error(
                    writer, safe_provider, "synthesis", indicate=failures == 0)
                last_failure = "synthesize"
                failures += 1
                continue
            try:
                if _interrupt_token() != interrupt:
                    config.log("speech interrupted before playback")
                    return True
                if _skip_token() != skip:
                    config.log("speech block skipped before playback")
                    spoke = False
                    continue
                if not _wait_for_microphone(interrupt):
                    return True
                if not woke:
                    _wake_output()
                    woke = True
                if _interrupt_token() != interrupt:
                    config.log("speech interrupted during output warm-up")
                    return True
                if _skip_token() != skip:
                    config.log("speech block skipped during output warm-up")
                    spoke = False
                    continue
                if not _wait_for_microphone(interrupt):
                    return True
                play(audio, interrupt, skip)
                if _interrupt_token() != interrupt:
                    return True
                if _skip_token() != skip:
                    config.log("speech block skipped during playback")
                    spoke = False
                    continue
                spoke = True
                runtime.set_speech(
                    writer, synthesis="idle", playback="idle")
            except Exception:
                _record_speech_error(
                    writer, safe_provider, "playback", indicate=failures == 0)
                last_failure = "play"
                failures += 1
    finally:
        processes.release(ownership)
        runtime.refresh_queue()
        runtime.release(
            writer,
            synthesis="degraded" if last_failure == "synthesize" else "idle",
            playback="degraded" if last_failure == "play" else "idle",
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass


def stop():
    """Permanently discard current and queued speech."""
    config.private_write(config.INTERRUPT, str(time.time_ns()))
    processes.clear(config.MIC_TURN)
    for item in _pending():
        item.unlink(missing_ok=True)
    _signal_speech(signal.SIGTERM)
    processes.clear(config.SPEECH_PID)
    processes.signal_all(config.CUE_PROCESSES, signal.SIGTERM, "cue")
    # Never trust PID-only state written by older Parley versions.
    config.PIDFILE.unlink(missing_ok=True)
    runtime.refresh_queue()


def skip():
    """End only the current speech block and leave queued speech intact."""
    config.private_write(config.SKIP, str(time.time_ns()))
    # A stop-talking utterance ends its provisional microphone reservation too.
    processes.clear(config.MIC_TURN)
    skipped = _signal_speech(signal.SIGTERM)
    config.log(
        f"speech block skip requested active={'yes' if skipped else 'no'}")
    return skipped


def detach(fn):
    """Run fn in a detached child so a hook never blocks the session."""
    if os.fork() != 0:
        return
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        fn()
    except Exception:
        import traceback

        config.log(traceback.format_exc())
    os._exit(0)
