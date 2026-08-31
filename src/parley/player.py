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

from parley import config
from parley.tts import synthesize

SENTENCE_BOUNDARY = re.compile(r'''[.!?]["'\u2019\u201d)\]]*(?=\s)''')
RESUME_REWIND_SECONDS = 0.25
OWNER_PATTERN = re.compile(r"owner-[0-9a-f]{32}\Z")
REVISION_PATTERN = re.compile(r"[0-9]{0,32}\Z")


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


def _valid_owner(owner):
    return isinstance(owner, str) and bool(OWNER_PATTERN.fullmatch(owner))


def _atomic_private_write(path, content):
    """Publish coordination metadata without exposing a partial value."""
    config.private_directory(path.parent)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        config.private_write(temporary, content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cancellation_path(owner):
    if not _valid_owner(owner):
        return None
    return config.CANCELLATIONS / owner


def owner_revision(owner):
    """Current bounded cancellation generation for an automatic target."""
    path = _cancellation_path(owner)
    if path is None:
        return ""
    try:
        revision = path.read_text().strip()
    except OSError:
        return ""
    return revision if REVISION_PATTERN.fullmatch(revision) else ""


def enqueue(text, voice=None, model=None, owner=None, revision=None):
    text = (text or "").strip()
    if not text:
        return False
    if owner is not None and not _valid_owner(owner):
        raise ValueError("queue owner must be an opaque Parley owner token")
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
        if owner is not None:
            queued_revision = (
                owner_revision(owner) if revision is None else str(revision)
            )
            if not REVISION_PATTERN.fullmatch(queued_revision):
                raise ValueError("queue owner revision is invalid")
            item["owner"] = owner
            item["owner_revision"] = queued_revision
        name = f"{timestamp:020d}-{os.getpid()}-{index:04d}.json"
        # Write then rename so a drainer never sees a half-written item.
        tmp = config.QUEUE / (name + ".tmp")
        config.private_write(tmp, json.dumps(item))
        tmp.rename(config.QUEUE / name)
    from parley import indicator

    indicator.refresh()
    return True


def _wake_output():
    """Bluetooth outputs swallow the first moment while they switch profiles."""
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
    config.STATE.mkdir(parents=True, exist_ok=True)
    proc = None
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
            if _active_cancelled():
                return True
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
            with open(config.PIDFILE, "a") as fh:
                fh.write(f"{proc.pid}\n")
            config.SPEECH_PID.write_text(str(proc.pid))
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
            if _active_cancelled():
                proc.terminate()
            returncode = proc.wait()
            elapsed = max(0.0, time.monotonic() - started)
            try:
                if config.SPEECH_PID.read_text().strip() == str(proc.pid):
                    config.SPEECH_PID.unlink(missing_ok=True)
            except OSError:
                pass
            if interrupt is not None and _interrupt_token() != interrupt:
                return False
            if _active_cancelled():
                return True
            if skip is not None and _skip_token() != skip:
                return True
            if _pause_token() == pause_token or returncode == 0:
                return True
            offset = max(
                0.001, offset + elapsed - RESUME_REWIND_SECONDS)
            config.log(f"speech checkpointed at {offset:.2f}s")
            if not _wait_for_microphone(interrupt):
                return False
            config.log(f"speech restarting at {offset:.2f}s after microphone turn")
    finally:
        try:
            if proc is not None and config.SPEECH_PID.read_text().strip() == str(
                    proc.pid):
                config.SPEECH_PID.unlink(missing_ok=True)
        except OSError:
            pass
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
    config.QUEUE.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in config.QUEUE.iterdir() if p.suffix == ".json")


def _job_cancelled(job):
    owner = job.get("owner")
    if not _valid_owner(owner):
        # Legacy and manual items are intentionally outside target cancellation.
        return False
    revision = str(job.get("owner_revision", ""))
    return revision != owner_revision(owner)


def _set_active(job):
    owner = job.get("owner") if _valid_owner(job.get("owner")) else None
    revision = str(job.get("owner_revision", "")) if owner else ""
    active = {"owner": owner, "owner_revision": revision}
    _atomic_private_write(config.ACTIVE, json.dumps(active, separators=(",", ":")))


def _active_job():
    try:
        active = json.loads(config.ACTIVE.read_text())
    except (OSError, ValueError, TypeError):
        config.ACTIVE.unlink(missing_ok=True)
        return None
    # Keep liveness behind the existing path-based validator. The accepted
    # PID-safety work can harden that single seam without a competing raw-PID
    # contract here.
    if not _pid_alive(config.DRAIN_PID):
        config.ACTIVE.unlink(missing_ok=True)
        config.log("recovered stale active ownership metadata")
        return None
    owner = active.get("owner")
    if owner is not None and not _valid_owner(owner):
        return None
    return active


def _active_cancelled():
    active = _active_job()
    if not active or not active.get("owner"):
        return False
    return str(active.get("owner_revision", "")) != owner_revision(
        active["owner"])


def _clear_active():
    config.ACTIVE.unlink(missing_ok=True)


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
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def microphone_active():
    """True only while the listener process owning the microphone is alive."""
    if _pid_alive(config.MIC_TURN):
        return True
    was_marked = config.MIC_TURN.exists()
    config.MIC_TURN.unlink(missing_ok=True)
    if was_marked:
        config.log("recovered stale microphone turn; speech released")
    return False


def output_playing():
    """True only after an audio process has actually been launched."""
    return _pid_alive(config.SPEECH_PID)


def _wait_for_microphone(interrupt=None):
    """Hold spoken output behind an exclusive microphone turn."""
    while microphone_active():
        if _active_cancelled():
            return False
        if interrupt is not None and _interrupt_token() != interrupt:
            return False
        time.sleep(0.05)
    return (
        not _active_cancelled()
        and (interrupt is None or _interrupt_token() == interrupt)
    )


def _signal_speech(sig):
    try:
        pid = int(config.SPEECH_PID.read_text().strip())
        os.kill(pid, sig)
        return True
    except (OSError, ValueError):
        config.SPEECH_PID.unlink(missing_ok=True)
        return False


def pause():
    """Give the microphone the floor while preserving current and queued speech."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    config.MIC_TURN.write_text(str(os.getpid()))
    config.private_write(config.PAUSE, str(time.time_ns()))
    paused = _signal_speech(signal.SIGTERM)
    config.log(
        f"microphone turn started speech={'checkpointing' if paused else 'waiting'}")
    return paused


def resume():
    """Release the microphone turn so checkpointed speech can restart."""
    had_turn = config.MIC_TURN.exists()
    config.MIC_TURN.unlink(missing_ok=True)
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
    config.STATE.mkdir(parents=True, exist_ok=True)
    lock = open(config.LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False

    woke = False
    spoke = False
    interrupt = _interrupt_token()
    config.DRAIN_PID.write_text(str(os.getpid()))
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
                    return True
            item = items[0]
            try:
                job = json.loads(item.read_text())
            except (OSError, ValueError):
                item.unlink(missing_ok=True)
                continue
            item.unlink(missing_ok=True)
            if not isinstance(job, dict):
                continue
            if _job_cancelled(job):
                config.log("target-canceled queued speech block discarded")
                continue
            skip = _skip_token()
            _set_active(job)
            try:
                # cancel() can race the gap between dequeue and active publish.
                if _job_cancelled(job):
                    config.log("target-canceled active speech before synthesis")
                    continue
                started = time.time()
                audio = synthesize(
                    job["text"], job.get("voice"), job.get("model"),
                    job.get("provider"),
                )
                config.log(
                    f"spoke {job.get('voice')} {len(job['text'])}c "
                    f"synth={time.time() - started:.1f}s {len(audio)}b"
                )
                if _interrupt_token() != interrupt:
                    config.log("speech interrupted before playback")
                    return True
                if _job_cancelled(job):
                    config.log("target-canceled speech before playback")
                    continue
                if _skip_token() != skip:
                    config.log("speech block skipped before playback")
                    spoke = False
                    continue
                if not _wait_for_microphone(interrupt):
                    if _job_cancelled(job):
                        config.log("target-canceled speech waiting for microphone")
                        continue
                    return True
                if not woke:
                    _wake_output()
                    woke = True
                if _interrupt_token() != interrupt:
                    config.log("speech interrupted during output warm-up")
                    return True
                if _job_cancelled(job):
                    config.log("target-canceled speech during output warm-up")
                    continue
                if _skip_token() != skip:
                    config.log("speech block skipped during output warm-up")
                    spoke = False
                    continue
                if not _wait_for_microphone(interrupt):
                    if _job_cancelled(job):
                        config.log("target-canceled speech waiting for microphone")
                        continue
                    return True
                play(audio, interrupt, skip)
                if _interrupt_token() != interrupt:
                    return True
                if _job_cancelled(job):
                    config.log("target-canceled speech during playback")
                    continue
                if _skip_token() != skip:
                    config.log("speech block skipped during playback")
                    spoke = False
                    continue
                spoke = True
            except Exception as exc:
                config.log(f"error: {exc}")
            finally:
                _clear_active()
    finally:
        try:
            if config.DRAIN_PID.read_text().strip() == str(os.getpid()):
                config.DRAIN_PID.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass


def cancel(owner):
    """Cancel only one automatic target's existing speech.

    Unowned manual and legacy queue items are deliberately preserved. The
    generation changes before the queue scan so jobs already detached from a
    hook cannot escape an Off race by arriving slightly later.
    """
    path = _cancellation_path(owner)
    if path is None:
        return False
    previous = owner_revision(owner)
    revision = str(time.time_ns())
    if revision == previous:
        revision = str(int(revision) + 1)
    _atomic_private_write(path, revision)

    removed = 0
    for item in _pending():
        try:
            job = json.loads(item.read_text())
        except (OSError, ValueError):
            continue
        if (isinstance(job, dict) and job.get("owner") == owner
                and _job_cancelled(job)):
            try:
                item.unlink()
                removed += 1
            except OSError:
                pass

    active = _active_job()
    matched_active = bool(active and active.get("owner") == owner)
    signaled = _signal_speech(signal.SIGTERM) if matched_active else False
    config.log(
        f"target speech canceled queued={removed} "
        f"active={'yes' if matched_active else 'no'} "
        f"signaled={'yes' if signaled else 'no'}"
    )
    from parley import indicator

    indicator.refresh()
    return bool(removed or matched_active)


def stop():
    """Globally and permanently discard current and queued speech."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    config.INTERRUPT.write_text(str(time.time_ns()))
    config.MIC_TURN.unlink(missing_ok=True)
    for item in _pending():
        item.unlink(missing_ok=True)
    try:
        pids = config.PIDFILE.read_text().split()
    except OSError:
        pids = []
    for pid in pids:
        try:
            os.kill(int(pid), 15)
        except (OSError, ValueError):
            pass
    try:
        config.PIDFILE.write_text("")
    except OSError:
        pass
    config.SPEECH_PID.unlink(missing_ok=True)


def skip():
    """End only the current speech block and leave queued speech intact."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    config.private_write(config.SKIP, str(time.time_ns()))
    # A stop-talking utterance ends its provisional microphone reservation too.
    config.MIC_TURN.unlink(missing_ok=True)
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
