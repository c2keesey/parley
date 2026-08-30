"""Paths, validated user settings, and credential discovery.

Non-secret customization may be persisted under ``PARLEY_STATE``. Environment
variables remain per-process overrides, preserving Parley's original behavior:

    environment > persisted setting > built-in default

Credentials are deliberately outside the settings registry and are only read
from the environment, the shared env file, or macOS Keychain.
"""

import functools
import getpass
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
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

VOICES = [
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "nova", "onyx", "sage", "shimmer", "verse",
]
FALLBACKS = ["gpt-4o-mini-tts", "tts-1"]
SETTINGS_VERSION = 1


class ConfigurationError(ValueError):
    """A safe, actionable configuration error suitable for the CLI."""


@dataclass(frozen=True)
class Setting:
    name: str
    env: str
    default: object
    description: str
    parser: object


@dataclass(frozen=True)
class ResolvedSetting:
    name: str
    value: object
    source: str
    env: str


@dataclass(frozen=True)
class Discovery:
    items: tuple
    warning: str | None = None


def _text(value, *, maximum=200):
    if not isinstance(value, str):
        raise ValueError("must be text")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be empty")
    if len(cleaned) > maximum:
        raise ValueError(f"must be at most {maximum} characters")
    if any(ord(char) < 32 for char in cleaned):
        raise ValueError("must not contain control characters")
    return cleaned


def _choice(*choices):
    def parse(value):
        cleaned = _text(value).lower()
        if cleaned not in choices:
            raise ValueError("must be one of " + ", ".join(choices))
        return cleaned
    return parse


def _integer(minimum, maximum):
    def parse(value):
        if isinstance(value, bool):
            raise ValueError("must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be an integer") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"must be between {minimum} and {maximum}")
        return number
    return parse


def _number(minimum, maximum):
    def parse(value):
        if isinstance(value, bool):
            raise ValueError("must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be a number") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"must be between {minimum} and {maximum}")
        return number
    return parse


def _microphone(value):
    cleaned = _text(str(value), maximum=120)
    if cleaned.startswith("-"):
        raise ValueError("must be a non-negative device index or device name")
    return cleaned


def _cue(value):
    return _text(str(value), maximum=1024)


def _phrase(value):
    return _text(value, maximum=80)


def _identifier(value):
    return _text(value)


SETTINGS = {
    item.name: item for item in (
        Setting("provider", "PARLEY_TTS_PROVIDER", "auto",
                "speech provider", _choice("auto", "openai", "elevenlabs", "macos")),
        Setting("voice", "PARLEY_VOICE", "fable",
                "OpenAI voice", _choice(*VOICES)),
        Setting("model", "PARLEY_MODEL", "gpt-4o-mini-tts-2025-12-15",
                "OpenAI speech model", _identifier),
        Setting("openai-fallback-voice", "PARLEY_OPENAI_FALLBACK_VOICE", "onyx",
                "OpenAI voice used by auto fallback", _choice(*VOICES)),
        Setting("elevenlabs-voice", "PARLEY_ELEVENLABS_VOICE_ID",
                "JBFqnCBsd6RMkjVDRZzb", "ElevenLabs voice ID", _identifier),
        Setting("elevenlabs-model", "PARLEY_ELEVENLABS_MODEL",
                "eleven_v3_conversational", "ElevenLabs speech model", _identifier),
        Setting("macos-voice", "PARLEY_MACOS_VOICE", "Eddy (English (US))",
                "macOS voice", _identifier),
        Setting("macos-rate", "PARLEY_MACOS_RATE", 210,
                "macOS words per minute", _integer(80, 500)),
        Setting("speed", "PARLEY_SPEED", 1.2,
                "OpenAI speech speed", _number(0.25, 4.0)),
        Setting("wake", "PARLEY_WAKE", "okay computer",
                "phrase that starts capture", _phrase),
        Setting("send", "PARLEY_SEND", "send it",
                "phrase that submits capture", _phrase),
        Setting("cancel", "PARLEY_CANCEL", "scrap that",
                "phrase that discards capture", _phrase),
        Setting("microphone", "PARLEY_MIC", "0",
                "avfoundation input device index or name", _microphone),
        Setting("mic-threshold", "PARLEY_MIC_THRESHOLD", 500,
                "microphone energy threshold", _integer(0, 32767)),
        Setting("cue-wake", "PARLEY_CUE_WAKE", "default",
                "wake cue: default, off, or an audio file", _cue),
        Setting("cue-send", "PARLEY_CUE_SEND", "default",
                "send cue: default, off, or an audio file", _cue),
        Setting("cue-cancel", "PARLEY_CUE_CANCEL", "default",
                "cancel cue: default, off, or an audio file", _cue),
        Setting("cue-stop", "PARLEY_CUE_STOP", "default",
                "stop cue: default, off, or an audio file", _cue),
        Setting("cue-done", "PARLEY_CUE_DONE", "default",
                "done cue: default, off, or an audio file", _cue),
    )
}

ALIASES = {
    "tts-provider": "provider",
    "device": "microphone",
    "mic": "microphone",
    "threshold": "mic-threshold",
    "rate": "macos-rate",
}


def settings_path():
    return STATE / "settings.json"


def private_directory(directory):
    """Create a state directory that only the current user can inspect."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def private_write(file_path, content):
    """Write a private state file without a world-readable creation window."""
    private_directory(file_path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(file_path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(file_path, 0o600)


def _atomic_private_write(file_path, content):
    """Replace a private file only after the complete new value is durable."""
    private_directory(file_path.parent)
    temporary = file_path.with_name(
        f".{file_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, file_path)
        os.chmod(file_path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _setting_name(name):
    cleaned = str(name).strip().lower().replace("_", "-")
    cleaned = ALIASES.get(cleaned, cleaned)
    if cleaned in SETTINGS:
        return cleaned
    secret_markers = (
        "api-key", "apikey", "password", "secret", "token", "credential")
    if any(marker in cleaned for marker in secret_markers):
        raise ConfigurationError(
            "credentials cannot be stored in Parley settings; use the environment, "
            "shared env file, or macOS Keychain"
        )
    choices = ", ".join(sorted(SETTINGS))
    raise ConfigurationError(f"unknown setting {name!r}; choose one of: {choices}")


def _load_persisted():
    file_path = settings_path()
    try:
        raw = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigurationError(f"cannot read {file_path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{file_path} is not valid JSON ({exc.msg} at line {exc.lineno}); "
            "run 'parley config reset --all' to remove it"
        ) from exc
    if not isinstance(document, dict) or document.get("version") != SETTINGS_VERSION:
        raise ConfigurationError(
            f"{file_path} has an unsupported settings format; "
            "run 'parley config reset --all' to remove it"
        )
    values = document.get("settings")
    if not isinstance(values, dict):
        raise ConfigurationError(f"{file_path} must contain a settings object")
    unknown = sorted(set(values) - set(SETTINGS))
    if unknown:
        raise ConfigurationError(
            f"{file_path} contains unknown setting {unknown[0]!r}; "
            "remove it or run 'parley config reset --all'"
        )
    return values


def _parse(setting, raw, source):
    try:
        return setting.parser(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"{setting.name} from {source} {exc}; "
            f"use 'parley config set {setting.name} VALUE' or unset {setting.env}"
        ) from exc


def resolved(name, persisted=None):
    """Return a typed setting and the source that won precedence."""
    canonical = _setting_name(name)
    setting = SETTINGS[canonical]
    values = _load_persisted() if persisted is None else persisted
    if setting.env in os.environ:
        return ResolvedSetting(
            canonical,
            _parse(
                setting, os.environ[setting.env],
                f"environment variable {setting.env}"),
            f"environment ({setting.env})",
            setting.env,
        )
    if canonical in values:
        return ResolvedSetting(
            canonical,
            _parse(setting, values[canonical], "persisted settings"),
            "persisted",
            setting.env,
        )
    return ResolvedSetting(
        canonical,
        _parse(setting, setting.default, "built-in default"),
        "default",
        setting.env,
    )


def all_settings():
    values = _load_persisted()
    return tuple(resolved(name, values) for name in sorted(SETTINGS))


def validate():
    """Validate the complete effective configuration without changing it."""
    all_settings()


def _validate_persisted_values(values):
    for name, raw in values.items():
        _parse(SETTINGS[name], raw, "persisted settings")
        if name.startswith("cue-") and raw not in {"default", "off"}:
            if not Path(raw).expanduser().is_file():
                raise ConfigurationError(
                    f"{name} file does not exist: {raw}; choose default, off, "
                    "or an existing audio file"
                )


def _validate_discovered_voice(name, voice, provider_name):
    discovery = discover_voices(provider_name)
    if discovery.warning and not discovery.items:
        raise ConfigurationError(
            f"cannot validate {name}: {discovery.warning}")
    if voice not in {item["id"] for item in discovery.items}:
        raise ConfigurationError(
            f"{name} {voice!r} is not available for {provider_name}; "
            f"inspect choices with 'parley config discover voices "
            f"--provider {provider_name}'"
        )


def set_values(updates):
    """Validate and atomically persist one or more non-secret settings."""
    existing = _load_persisted()
    parsed = {}
    for requested, raw in updates.items():
        name = _setting_name(requested)
        parsed[name] = _parse(SETTINGS[name], raw, "new value")
    candidate = {**existing, **parsed}
    _validate_persisted_values(candidate)
    if "macos-voice" in parsed:
        _validate_discovered_voice("macos-voice", parsed["macos-voice"], "macos")
    if "elevenlabs-voice" in parsed and elevenlabs_api_key():
        _validate_discovered_voice(
            "elevenlabs-voice", parsed["elevenlabs-voice"], "elevenlabs")
    document = {"version": SETTINGS_VERSION, "settings": candidate}
    _atomic_private_write(
        settings_path(), json.dumps(document, indent=2, sort_keys=True) + "\n")
    return tuple(parsed)


def reset(name=None):
    """Remove one persisted override, or recover by removing the whole file."""
    file_path = settings_path()
    if name is None:
        file_path.unlink(missing_ok=True)
        return
    canonical = _setting_name(name)
    values = _load_persisted()
    values.pop(canonical, None)
    if not values:
        file_path.unlink(missing_ok=True)
        return
    document = {"version": SETTINGS_VERSION, "settings": values}
    _atomic_private_write(
        file_path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def _safe_value(name):
    """Import safely; CLI validation still reports malformed user input."""
    try:
        return resolved(name).value
    except ConfigurationError:
        return SETTINGS[name].default


# Compatibility constants for existing modules and third-party imports. They
# are populated safely so malformed numeric input never crashes module import.
PROVIDER = _safe_value("provider")
MODEL = _safe_value("model")
VOICE = _safe_value("voice")
OPENAI_FALLBACK_VOICE = _safe_value("openai-fallback-voice")
MACOS_VOICE = _safe_value("macos-voice")
MACOS_RATE = _safe_value("macos-rate")
ELEVENLABS_MODEL = _safe_value("elevenlabs-model")
ELEVENLABS_VOICE = _safe_value("elevenlabs-voice")
SPEED = _safe_value("speed")
WAKE = _safe_value("wake")
SEND = _safe_value("send")
CANCEL = _safe_value("cancel")
MIC_DEVICE = _safe_value("microphone")
MIC_THRESHOLD = _safe_value("mic-threshold")

ELEVENLABS_KEYCHAIN_SERVICE = os.environ.get(
    "PARLEY_ELEVENLABS_KEYCHAIN_SERVICE", "parley-elevenlabs-api-key")
ELEVENLABS_KEYCHAIN_ACCOUNT = os.environ.get(
    "PARLEY_ELEVENLABS_KEYCHAIN_ACCOUNT", getpass.getuser())
INSTRUCTIONS = os.environ.get(
    "PARLEY_INSTRUCTIONS",
    "Natural and conversational, like a colleague talking you through an update. "
    "Brisk but unhurried. Even tone, no announcer polish.",
)


def _safe_number(env, default, parser):
    try:
        return parser(os.environ.get(env, default))
    except ValueError:
        return parser(default)


MAX_CHARS = _safe_number("PARLEY_MAX_CHARS", 3000, _integer(1, 1_000_000))
TTS_RETRY_SECONDS = _safe_number(
    "PARLEY_TTS_RETRY_SECONDS", 300, _number(0, 86_400))

KEY_FILES = [
    os.environ.get("PARLEY_ENV", ""),
    str(Path.home() / ".config" / "parley" / "env"),
]


def cue_choice(name):
    """Effective cue selection: default, off, or a custom local file."""
    setting_name = f"cue-{name}"
    if setting_name not in SETTINGS:
        return "default"
    try:
        return resolved(setting_name).value
    except ConfigurationError:
        return "default"


def log(msg):
    try:
        private_directory(STATE)
        descriptor = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        os.chmod(LOG, 0o600)
    except OSError:
        pass


def secret(name):
    """A provider key from the environment or Parley's shared env file."""
    if os.environ.get(name):
        return os.environ[name]
    for file_name in KEY_FILES:
        if not file_name or not os.path.exists(file_name):
            continue
        try:
            lines = open(file_name, encoding="utf-8", errors="replace")
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


def tts_fallback_active(provider_name="elevenlabs"):
    """Whether auto mode is temporarily avoiding a failed speech provider."""
    marker = STATE / f"tts-{provider_name}-fallback-until"
    try:
        active = float(marker.read_text()) > time.time()
    except (OSError, ValueError):
        return False
    if not active:
        marker.unlink(missing_ok=True)
    return active


def mark_tts_fallback(provider_name="elevenlabs"):
    """Avoid a failed speech provider until it is worth retrying."""
    private_write(
        STATE / f"tts-{provider_name}-fallback-until",
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


def discover_providers():
    """Provider availability without revealing or persisting credentials."""
    elevenlabs_available = bool(elevenlabs_api_key())
    openai_available = bool(api_key())
    macos_available = bool(shutil.which("say"))
    rows = (
        {"id": "auto", "available": True, "detail": "automatic fallback chain"},
        {"id": "elevenlabs", "available": elevenlabs_available,
         "detail": "credential available" if elevenlabs_available else "no credential"},
        {"id": "openai", "available": openai_available,
         "detail": "credential available" if openai_available else "no credential"},
        {"id": "macos", "available": macos_available,
         "detail": "local say command" if macos_available else "say unavailable"},
    )
    return Discovery(rows)


def discover_devices():
    """List avfoundation microphones; absence and parser drift are non-fatal."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return Discovery(
            (), "ffmpeg is unavailable; install it with 'brew install ffmpeg'")
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true",
             "-i", ""],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Discovery((), f"microphone discovery unavailable: {exc}")
    devices = []
    in_audio = False
    for line in (result.stderr + "\n" + result.stdout).splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if in_audio and "AVFoundation " in line and " devices:" in line:
            break
        if not in_audio:
            continue
        match = re.search(r"\]\s+\[(\d+)\]\s+(.+)$", line)
        if match:
            devices.append({"id": match.group(1), "name": match.group(2).strip()})
    warning = None if devices else "no avfoundation microphone devices were reported"
    return Discovery(tuple(devices), warning)


def discover_voices(provider_name=None):
    """List voices for one provider, degrading safely without network or keys."""
    selected = provider_name or provider()
    if selected == "auto":
        selected = provider()
    if selected == "openai":
        return Discovery(tuple({"id": voice, "name": voice} for voice in VOICES))
    if selected == "macos":
        binary = shutil.which("say")
        if not binary:
            return Discovery((), "macOS say is unavailable")
        try:
            result = subprocess.run(
                [binary, "-v", "?"], capture_output=True, text=True,
                timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return Discovery((), f"macOS voice discovery unavailable: {exc}")
        voices = []
        for line in result.stdout.splitlines():
            match = re.match(r"^(.+?)\s{2,}[a-z]{2}_[A-Z]{2}\s+#", line)
            if match:
                name = match.group(1).strip()
                voices.append({"id": name, "name": name})
        warning = None if voices else "no macOS voices were reported"
        return Discovery(tuple(voices), warning)
    if selected == "elevenlabs":
        if not elevenlabs_api_key():
            return Discovery(
                (), "ElevenLabs voice discovery needs a configured credential")
        try:
            from parley.tts import voices
            rows = tuple(
                {"id": item["voice_id"], "name": item.get("name", "Unnamed")}
                for item in voices()
            )
            warning = None if rows else "no ElevenLabs voices were reported"
            return Discovery(rows, warning)
        except Exception as exc:
            return Discovery((), f"ElevenLabs voice discovery unavailable: {exc}")
    return Discovery((), f"unknown provider {selected!r}")


def validate_voice(voice, provider_name=None):
    """Reject a one-shot voice that the active provider cannot use."""
    selected = provider_name or provider()
    discovery = discover_voices(selected)
    if discovery.warning and not discovery.items:
        raise ConfigurationError(discovery.warning)
    valid = {item["id"] for item in discovery.items}
    if voice not in valid:
        sample = ", ".join(sorted(valid)[:8])
        raise ConfigurationError(
            f"voice {voice!r} is not available for {selected}; "
            f"choose one from 'parley voices' ({sample})"
        )
    return voice


def validate_model(model, provider_name=None):
    """Reject a one-shot model flag when the provider has no model choice."""
    selected = provider_name or provider()
    if selected == "macos":
        raise ConfigurationError(
            "--model is not supported by the macOS provider; "
            "use 'parley config set macos-voice VALUE' and macos-rate instead"
        )
    return _identifier(model)
