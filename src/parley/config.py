"""Paths, defaults, and credential discovery.

Every knob is an environment variable so a session can override the voice or
model without editing anything.
"""
import functools
import getpass
import os
import subprocess
import time
from pathlib import Path

STATE = Path(os.environ.get("PARLEY_STATE", Path.home() / ".parley"))
SESSIONS = STATE / "sessions"
SPOKEN = STATE / "spoken"
QUEUE = STATE / "queue"
DEFAULT = STATE / "default"
LOCK = STATE / "player.lock"
PIDFILE = STATE / "playing.pid"
SPEECH_PID = STATE / "speech.pid"
DRAIN_PID = STATE / "drainer.pid"
MIC_TURN = STATE / "microphone-turn.pid"
LOG = STATE / "speak.log"
SILENCE = STATE / "silence.mp3"
INTERRUPT = STATE / "interrupt"
LISTENER_STATE = STATE / "listener.state"
TRIGGERS = STATE / "triggers"

PROMPT = (
    "Spoken session: Parley announces this session's name, then reads your "
    "reply aloud. "
    "Answer in a few sentences of plain speech — no markdown, code, or paths. "
    "Never use AskUserQuestion; it cannot be answered by voice. Ask in your reply."
)

PROVIDER = os.environ.get("PARLEY_TTS_PROVIDER", "auto").lower()
MODEL = os.environ.get("PARLEY_MODEL", "gpt-4o-mini-tts-2025-12-15")
FALLBACKS = ["gpt-4o-mini-tts", "tts-1"]
VOICE = os.environ.get("PARLEY_VOICE", "fable")
ELEVENLABS_MODEL = os.environ.get("PARLEY_ELEVENLABS_MODEL", "eleven_v3")
ELEVENLABS_VOICE = os.environ.get(
    "PARLEY_ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # George
ELEVENLABS_KEYCHAIN_SERVICE = os.environ.get(
    "PARLEY_ELEVENLABS_KEYCHAIN_SERVICE", "parley-elevenlabs-api-key")
ELEVENLABS_KEYCHAIN_ACCOUNT = os.environ.get(
    "PARLEY_ELEVENLABS_KEYCHAIN_ACCOUNT", getpass.getuser())
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


def private_directory(path):
    """Create a state directory that only the current user can inspect."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def private_write(path, content):
    """Write a private state file without a world-readable creation window."""
    private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, 0o600)


def log(msg):
    try:
        private_directory(STATE)
        descriptor = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        os.chmod(LOG, 0o600)
    except OSError:
        pass


def secret(name):
    """A provider key from the environment or Parley's shared env file."""
    if os.environ.get(name):
        return os.environ[name]
    for path in KEY_FILES:
        if not path or not os.path.exists(path):
            continue
        try:
            lines = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line.startswith("#") or not line.startswith(name + "="):
                continue
            value = line.split("=", 1)[-1].strip().strip("\"'")
            if value:
                return value
    return None


def api_key():
    """Backward-compatible OpenAI key lookup used by transcription."""
    return secret("OPENAI_API_KEY")


@functools.lru_cache(maxsize=4)
def keychain_secret(service, account):
    """Read one generic password without echoing or putting the secret in argv."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service,
             "-a", account, "-w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def elevenlabs_api_key():
    return secret("ELEVENLABS_API_KEY") or keychain_secret(
        ELEVENLABS_KEYCHAIN_SERVICE, ELEVENLABS_KEYCHAIN_ACCOUNT)


def provider():
    if PROVIDER not in ("auto", "openai", "elevenlabs"):
        raise RuntimeError(
            "PARLEY_TTS_PROVIDER must be auto, openai, or elevenlabs")
    if PROVIDER == "auto":
        return "elevenlabs" if elevenlabs_api_key() else "openai"
    return PROVIDER


def active_voice():
    return ELEVENLABS_VOICE if provider() == "elevenlabs" else VOICE


def active_model():
    return ELEVENLABS_MODEL if provider() == "elevenlabs" else MODEL
