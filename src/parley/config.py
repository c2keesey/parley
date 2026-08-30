"""Paths, defaults, and credential discovery.

Every knob is an environment variable so a session can override the voice or
model without editing anything.
"""
import functools
import getpass
import math
import os
import subprocess
import time
from pathlib import Path


class ConfigurationError(ValueError):
    """A user-actionable environment configuration error."""


_NUMBER_SETTINGS = {
    "PARLEY_MACOS_RATE": (int, "210", "a positive integer"),
    "PARLEY_SPEED": (float, "1.2", "a positive finite number"),
    "PARLEY_MAX_CHARS": (int, "3000", "a positive integer"),
    "PARLEY_TTS_RETRY_SECONDS": (float, "300", "a non-negative finite number"),
    "PARLEY_MIC_THRESHOLD": (int, "500", "a non-negative integer"),
    "PARLEY_MIN_SPEECH": (float, "0.10", "a non-negative finite number"),
    "PARLEY_SILENCE_TIMEOUT": (float, "120", "a positive finite number"),
    "PARLEY_HARD_STOP": (float, "1200", "a positive finite number"),
}


def _valid_number(name, value):
    parser, _default, description = _NUMBER_SETTINGS[name]
    try:
        parsed = parser(value)
    except (TypeError, ValueError):
        return False, description
    if isinstance(parsed, float) and not math.isfinite(parsed):
        return False, description
    if "non-negative" in description:
        return parsed >= 0, description
    return parsed > 0, description


def _number(name):
    parser, default, _description = _NUMBER_SETTINGS[name]
    raw = os.environ.get(name, default)
    valid, _ = _valid_number(name, raw)
    return parser(raw) if valid else parser(default)

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
PAUSE = STATE / "pause"
LOG = STATE / "speak.log"
SILENCE = STATE / "silence.mp3"
INTERRUPT = STATE / "interrupt"
SKIP = STATE / "skip"
LISTENER_STATE = STATE / "listener.state"
TRIGGERS = STATE / "triggers"

PROMPT = (
    "Spoken session: Parley announces this session's name, then reads your "
    "reply aloud. "
    "Answer in a few sentences of plain speech — no markdown, code, URLs, "
    "links, citations, or file paths. If attribution matters, name the source "
    "without linking it. "
    "Never use AskUserQuestion; it cannot be answered by voice. Ask in your reply."
)

PROVIDER = os.environ.get("PARLEY_TTS_PROVIDER", "auto").lower()
MODEL = os.environ.get("PARLEY_MODEL", "gpt-4o-mini-tts-2025-12-15")
FALLBACKS = ["gpt-4o-mini-tts", "tts-1"]
VOICE = os.environ.get("PARLEY_VOICE", "fable")
OPENAI_FALLBACK_VOICE = os.environ.get("PARLEY_OPENAI_FALLBACK_VOICE", "onyx")
MACOS_VOICE = os.environ.get("PARLEY_MACOS_VOICE", "Eddy (English (US))")
MACOS_RATE = _number("PARLEY_MACOS_RATE")
ELEVENLABS_MODEL = os.environ.get(
    "PARLEY_ELEVENLABS_MODEL", "eleven_v3_conversational")
ELEVENLABS_VOICE = os.environ.get(
    "PARLEY_ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # George
ELEVENLABS_KEYCHAIN_SERVICE = os.environ.get(
    "PARLEY_ELEVENLABS_KEYCHAIN_SERVICE", "parley-elevenlabs-api-key")
ELEVENLABS_KEYCHAIN_ACCOUNT = os.environ.get(
    "PARLEY_ELEVENLABS_KEYCHAIN_ACCOUNT", getpass.getuser())
SPEED = _number("PARLEY_SPEED")
INSTRUCTIONS = os.environ.get(
    "PARLEY_INSTRUCTIONS",
    "Natural and conversational, like a colleague talking you through an update. "
    "Brisk but unhurried. Even tone, no announcer polish.",
)
MAX_CHARS = _number("PARLEY_MAX_CHARS")
TTS_RETRY_SECONDS = _number("PARLEY_TTS_RETRY_SECONDS")

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


def secret_configured(name):
    """Whether a key exists, without returning or displaying its value."""
    if os.environ.get(name):
        return True
    for path in KEY_FILES:
        if not path or not os.path.exists(path):
            continue
        try:
            lines = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#") or not stripped.startswith(name + "="):
                    continue
                if stripped.partition("=")[2].strip().strip("\"'"):
                    return True
    return False


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


def keychain_secret_configured(service, account):
    """Check for a Keychain item without requesting its secret value."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def openai_key_configured():
    return secret_configured("OPENAI_API_KEY")


def elevenlabs_key_configured():
    return secret_configured("ELEVENLABS_API_KEY") or keychain_secret_configured(
        ELEVENLABS_KEYCHAIN_SERVICE, ELEVENLABS_KEYCHAIN_ACCOUNT)


def elevenlabs_api_key():
    return secret("ELEVENLABS_API_KEY") or keychain_secret(
        ELEVENLABS_KEYCHAIN_SERVICE, ELEVENLABS_KEYCHAIN_ACCOUNT)


def configuration_errors():
    """Return deterministic, value-free errors for supported environment knobs."""
    errors = []
    if PROVIDER not in ("auto", "openai", "elevenlabs", "macos"):
        errors.append(
            "PARLEY_TTS_PROVIDER must be auto, openai, elevenlabs, or macos; "
            "unset it to use auto"
        )
    for name, (_parser, default, _description) in _NUMBER_SETTINGS.items():
        value = os.environ.get(name, default)
        valid, description = _valid_number(name, value)
        if not valid:
            errors.append(
                f"{name} must be {description}; unset it to use {default}"
            )
    return errors


def require_valid_configuration():
    errors = configuration_errors()
    if errors:
        raise ConfigurationError(errors[0])


def tts_fallback_active(provider="elevenlabs"):
    """Whether auto mode is temporarily avoiding a failed speech provider."""
    marker = STATE / f"tts-{provider}-fallback-until"
    try:
        active = float(marker.read_text()) > time.time()
    except (OSError, ValueError):
        return False
    if not active:
        marker.unlink(missing_ok=True)
    return active


def mark_tts_fallback(provider="elevenlabs"):
    """Avoid a failed speech provider until it is worth retrying."""
    private_write(
        STATE / f"tts-{provider}-fallback-until",
        str(time.time() + TTS_RETRY_SECONDS),
    )


def provider():
    if PROVIDER not in ("auto", "openai", "elevenlabs", "macos"):
        raise RuntimeError(
            "PARLEY_TTS_PROVIDER must be auto, openai, elevenlabs, or macos")
    if PROVIDER == "auto":
        if elevenlabs_api_key() and not tts_fallback_active("elevenlabs"):
            return "elevenlabs"
        if api_key() and not tts_fallback_active("openai"):
            return "openai"
        return "macos"
    return PROVIDER


def active_voice():
    selected = provider()
    if selected == "elevenlabs":
        return ELEVENLABS_VOICE
    if selected == "macos":
        return MACOS_VOICE
    if PROVIDER == "auto":
        return OPENAI_FALLBACK_VOICE
    return VOICE


def active_model():
    selected = provider()
    if selected == "elevenlabs":
        return ELEVENLABS_MODEL
    return "say" if selected == "macos" else MODEL
