"""Paths, defaults, and credential discovery.

Every knob is an environment variable so a session can override the voice or
model without editing anything.
"""
import os
import time
from pathlib import Path

STATE = Path(os.environ.get("PARLEY_STATE", Path.home() / ".parley"))
SESSIONS = STATE / "sessions"
SPOKEN = STATE / "spoken"
QUEUE = STATE / "queue"
DEFAULT = STATE / "default"
LOCK = STATE / "player.lock"
PIDFILE = STATE / "playing.pid"
LOG = STATE / "speak.log"
SILENCE = STATE / "silence.mp3"

PROMPT = (
    "Spoken session: your reply is read aloud verbatim. "
    "Answer in a few sentences of plain speech — no markdown, code, or paths. "
    "Never use AskUserQuestion; it cannot be answered by voice. Ask in your reply."
)

MODEL = os.environ.get("PARLEY_MODEL", "gpt-4o-mini-tts-2025-12-15")
FALLBACKS = ["gpt-4o-mini-tts", "tts-1"]
VOICE = os.environ.get("PARLEY_VOICE", "fable")
SPEED = float(os.environ.get("PARLEY_SPEED", "1.2"))
INSTRUCTIONS = os.environ.get(
    "PARLEY_INSTRUCTIONS",
    "Natural and conversational, like a colleague talking you through an update. "
    "Brisk but unhurried. Even tone, no announcer polish.",
)
MAX_CHARS = int(os.environ.get("PARLEY_MAX_CHARS", "3000"))

VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable",
          "nova", "onyx", "sage", "shimmer", "verse"]

KEY_FILES = [
    os.environ.get("PARLEY_ENV", ""),
    str(Path.home() / ".config" / "parley" / "env"),
]


def log(msg):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def api_key():
    """OPENAI_API_KEY from the environment, or the first key file that has one."""
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    for path in KEY_FILES:
        if not path or not os.path.exists(path):
            continue
        try:
            lines = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line.startswith("#") or "OPENAI_API_KEY" not in line:
                continue
            value = line.split("=", 1)[-1].strip().strip("\"'")
            if value.startswith("sk-"):
                return value
    return None
