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
import subprocess
import tempfile
import time

from parley import config
from parley.tts import synthesize


def enqueue(text, voice=None, model=None):
    text = (text or "").strip()
    if not text:
        return False
    config.QUEUE.mkdir(parents=True, exist_ok=True)
    item = {
        "text": text[: config.MAX_CHARS],
        "voice": voice or config.VOICE,
        "model": model or config.MODEL,
    }
    name = f"{time.time_ns():020d}-{os.getpid()}.json"
    # Write then rename so a drainer never sees a half-written item.
    tmp = config.QUEUE / (name + ".tmp")
    tmp.write_text(json.dumps(item))
    tmp.rename(config.QUEUE / name)
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


def play(audio):
    config.STATE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".mp3", delete=False, dir=config.STATE
    ) as fh:
        fh.write(audio)
        path = fh.name
    try:
        proc = subprocess.Popen(["afplay", path])
        with open(config.PIDFILE, "a") as fh:
            fh.write(f"{proc.pid}\n")
        proc.wait()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _pending():
    config.QUEUE.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in config.QUEUE.iterdir() if p.suffix == ".json")


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
                audio = synthesize(job["text"], job.get("voice"), job.get("model"))
                config.log(
                    f"spoke {job.get('voice')} {len(job['text'])}c "
                    f"synth={time.time() - started:.1f}s {len(audio)}b"
                )
                if not woke:
                    _wake_output()
                    woke = True
                play(audio)
                spoke = True
            except Exception as exc:
                config.log(f"error: {exc}")
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass


def stop():
    """Drop everything queued and silence anything playing."""
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
