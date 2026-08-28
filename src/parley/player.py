"""A spool directory drained by one player, so nothing ever overlaps.

Utterances are files in a queue directory, named by nanosecond timestamp so
sorting is arrival order. Any number of processes may enqueue. Exactly one
drains, chosen by an exclusive flock — the rest return immediately and let the
holder pick up their work.
"""
import fcntl
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

from parley import config
from parley.tts import synthesize


def enqueue(text, voice=None, model=None):
    text = (text or "").strip()
    if not text:
        return False
    config.private_directory(config.QUEUE)
    item = {
        "text": text[: config.MAX_CHARS],
        "provider": config.provider(),
        "voice": voice or config.active_voice(),
        "model": model or config.active_model(),
    }
    name = f"{time.time_ns():020d}-{os.getpid()}.json"
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


def play(audio, interrupt=None):
    config.STATE.mkdir(parents=True, exist_ok=True)
    proc = None
    suffix = ".aiff" if audio.startswith(b"FORM") else ".mp3"
    with tempfile.NamedTemporaryFile(
        suffix=suffix, delete=False, dir=config.STATE
    ) as fh:
        fh.write(audio)
        path = fh.name
    try:
        if not _wait_for_microphone(interrupt):
            return False
        proc = subprocess.Popen(["afplay", path])
        with open(config.PIDFILE, "a") as fh:
            fh.write(f"{proc.pid}\n")
        config.SPEECH_PID.write_text(str(proc.pid))
        # Close the race between checking the microphone turn and publishing
        # the new audio pid. A turn that opened in that window pauses it here.
        if microphone_active():
            os.kill(proc.pid, signal.SIGSTOP)
            config.log("speech paused during audio launch")
            if not _wait_for_microphone(interrupt):
                proc.terminate()
            else:
                os.kill(proc.pid, signal.SIGCONT)
                config.log("speech resumed after microphone turn")
        if interrupt is not None and _interrupt_token() != interrupt:
            proc.terminate()
        proc.wait()
        return interrupt is None or _interrupt_token() == interrupt
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


def _pending():
    config.QUEUE.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in config.QUEUE.iterdir() if p.suffix == ".json")


def _interrupt_token():
    try:
        return config.INTERRUPT.read_text().strip()
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
    if was_marked and _signal_speech(signal.SIGCONT):
        config.log("recovered stale microphone turn; speech resumed")
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
    paused = _signal_speech(signal.SIGSTOP)
    config.log(
        f"microphone turn started speech={'paused' if paused else 'waiting'}")
    return paused


def resume():
    """Release the microphone turn and continue the same speech process."""
    had_turn = config.MIC_TURN.exists()
    config.MIC_TURN.unlink(missing_ok=True)
    if not had_turn:
        return False
    resumed = _signal_speech(signal.SIGCONT)
    config.log(
        f"microphone turn ended speech={'resumed' if resumed else 'released'}")
    return resumed


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
            try:
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
                if not _wait_for_microphone(interrupt):
                    return True
                if not woke:
                    _wake_output()
                    woke = True
                if _interrupt_token() != interrupt:
                    config.log("speech interrupted during output warm-up")
                    return True
                if not _wait_for_microphone(interrupt):
                    return True
                play(audio, interrupt)
                if _interrupt_token() != interrupt:
                    return True
                spoke = True
            except Exception as exc:
                config.log(f"error: {exc}")
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


def stop():
    """Permanently discard current and queued speech."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    config.INTERRUPT.write_text(str(time.time_ns()))
    config.MIC_TURN.unlink(missing_ok=True)
    for item in _pending():
        item.unlink(missing_ok=True)
    try:
        pids = config.PIDFILE.read_text().split()
    except OSError:
        pids = []
    try:
        speech_pid = config.SPEECH_PID.read_text().strip()
    except OSError:
        speech_pid = ""
    for pid in pids:
        try:
            os.kill(int(pid), 15)
            # A SIGTERM sent to a SIGSTOP-paused process is handled once it is
            # continued. This guarantees explicit stop is permanent.
            if pid == speech_pid:
                os.kill(int(pid), signal.SIGCONT)
        except (OSError, ValueError):
            pass
    try:
        config.PIDFILE.write_text("")
    except OSError:
        pass
    config.SPEECH_PID.unlink(missing_ok=True)


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
