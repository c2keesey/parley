"""Read-only, privacy-safe Parley diagnostics."""

import json
import os
import platform
import shutil
import stat
import subprocess

from parley import config, hooks

SCHEMA_VERSION = 1
_RANK = {"pass": 0, "warn": 1, "fail": 2}


def _check(identifier, status, summary, action=None, data=None):
    item = {"id": identifier, "status": status, "summary": summary}
    if action:
        item["action"] = action
    if data:
        item["data"] = data
    return item


def _tool(identifier, executable, purpose, required, install):
    if shutil.which(executable):
        return _check(identifier, "pass", f"{executable} is available for {purpose}")
    status = "fail" if required else "warn"
    return _check(
        identifier, status, f"{executable} is missing; {purpose} is unavailable",
        install,
    )


def _tool_any(identifier, executables, purpose, install):
    available = next((name for name in executables if shutil.which(name)), None)
    if available:
        return _check(identifier, "pass", f"{available} is available for {purpose}")
    choices = " or ".join(executables)
    return _check(
        identifier, "warn", f"{choices} is missing; {purpose} is unavailable",
        install,
    )


def _state_check():
    path = config.STATE
    data = {"path": str(path)}
    if not path.exists():
        parent = next((item for item in (path, *path.parents) if item.exists()), None)
        usable = bool(parent and os.access(parent, os.W_OK | os.X_OK))
        return _check(
            "state.directory", "warn" if usable else "fail",
            "state directory has not been created" if usable else
            "state directory cannot be created from its existing parent",
            "Run `parley on` to initialize it." if usable else
            "Choose a writable PARLEY_STATE directory.",
            data,
        )
    if not path.is_dir():
        return _check(
            "state.directory", "fail", "state path is not a directory",
            "Set PARLEY_STATE to a private directory.", data,
        )
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return _check(
            "state.directory", "fail", "state directory metadata is unreadable",
            "Check ownership and permissions.", data,
        )
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return _check(
            "state.directory", "fail", "state directory is not fully accessible",
            "Grant the current user read, write, and search access.", data,
        )
    if mode & 0o077:
        return _check(
            "state.directory", "fail", "state directory is visible to other users",
            "Restrict it to mode 700 (owner access only).", data,
        )
    return _check(
        "state.directory", "pass", "state directory is private and accessible",
        data=data,
    )


def _provider_check():
    selected = config.PROVIDER
    if selected == "openai":
        openai = config.openai_key_configured()
        if openai:
            return _check("provider.tts", "pass", "OpenAI is configured")
        return _check(
            "provider.tts", "fail",
            "OpenAI is selected but no credential is configured",
            "Set OPENAI_API_KEY or choose PARLEY_TTS_PROVIDER=macos.",
        )
    if selected == "elevenlabs":
        elevenlabs = config.elevenlabs_key_configured()
        if elevenlabs:
            return _check("provider.tts", "pass", "ElevenLabs is configured")
        return _check(
            "provider.tts", "fail",
            "ElevenLabs is selected but no credential is configured",
            "Add the ElevenLabs Keychain item or set ELEVENLABS_API_KEY.",
        )
    if selected == "macos":
        return _check("provider.tts", "pass", "local macOS speech is selected")
    elevenlabs = config.elevenlabs_key_configured()
    openai = config.openai_key_configured()
    if elevenlabs:
        summary = "auto mode can use ElevenLabs with local speech fallback"
    elif openai:
        summary = "auto mode can use OpenAI with local speech fallback"
    else:
        summary = "auto mode will use credential-free local macOS speech"
    return _check("provider.tts", "pass", summary)


def _pid_alive(path):
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _pane_available(pane):
    if not pane or not shutil.which("tmux"):
        return False
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _listener_checks():
    listener_pid = config.STATE / "listener.pid"
    target_path = config.STATE / "target"
    running = _pid_alive(listener_pid)
    process = _check(
        "listener.process", "pass" if running else "warn",
        "listener is running" if running else "listener is not running",
        None if running else "Run `parley listen on` when hands-free input is wanted.",
    )
    try:
        pane = target_path.read_text().strip()
    except OSError:
        pane = ""
    available = _pane_available(pane)
    if available:
        target = _check("listener.target", "pass", "listener target pane is available")
    elif pane and running:
        target = _check(
            "listener.target", "fail", "running listener target is unavailable",
            "Run `parley listen off`, then `parley listen on` in the intended pane.",
        )
    elif pane:
        target = _check(
            "listener.target", "warn", "saved listener target is unavailable",
            "Run `parley listen on` in the intended tmux pane.",
        )
    else:
        target = _check(
            "listener.target", "warn", "no listener target is configured",
            "Run `parley listen on` from inside the intended tmux pane.",
        )
    microphone = _check(
        "input.microphone", "pass" if running else "warn",
        "active listener confirms microphone capture started" if running else
        "microphone permission is verified only when the listener starts",
        None if running else
        "Allow microphone access for the terminal when `parley listen on` prompts.",
    )
    return [process, target, microphone]


def _hook_check(name, path):
    identifier = f"hook.{name}"
    if not path.exists():
        return _check(
            identifier, "warn", f"{name} hook is not installed",
            f"Run `parley install --harness {name}`.",
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return _check(
            identifier, "fail",
            f"{name} hook configuration is unreadable or invalid JSON",
            "Repair the configuration file before running `parley install`.",
        )
    matchers = (
        data.get("hooks", {}).get(hooks.EVENT, [])
        if isinstance(data, dict) else []
    )
    installed = False
    if isinstance(matchers, list):
        for matcher in matchers:
            configured = matcher.get("hooks", []) if isinstance(matcher, dict) else []
            if not isinstance(configured, list):
                continue
            if any(
                isinstance(hook, dict)
                and hooks.COMMAND in str(hook.get("command", ""))
                for hook in configured
            ):
                installed = True
                break
    if installed:
        return _check(identifier, "pass", f"{name} hook is installed")
    return _check(
        identifier, "warn", f"{name} hook is not installed",
        f"Run `parley install --harness {name}`.",
    )


def collect():
    """Collect diagnostics without changing files, processes, permissions, or hooks."""
    is_macos = platform.system() == "Darwin"
    checks = [
        _check(
            "system.macos", "pass" if is_macos else "fail",
            "running on macOS" if is_macos else
            "Parley runtime support currently requires macOS",
        ),
        _tool("output.say", "say", "local speech synthesis", True,
              "Use the macOS `say` command on a supported system."),
        _tool("output.afplay", "afplay", "audio playback", True,
              "Use the macOS `afplay` command on a supported system."),
        _provider_check(),
        _state_check(),
        _tool("input.ffmpeg", "ffmpeg", "microphone capture", False,
              "Install it with `brew install ffmpeg`."),
        _tool_any(
            "input.whisper", ("whisper-cli", "whisper-cpp"),
            "local trigger recognition", "Install it with `brew install whisper-cpp`.",
        ),
        _tool("input.tmux", "tmux", "listener target routing", False,
              "Install tmux and run the listener inside a tmux pane."),
        *_listener_checks(),
        *(_hook_check(name, path) for name, path in sorted(hooks.TARGETS.items())),
    ]
    overall = max((item["status"] for item in checks), key=_RANK.__getitem__)
    return {"schema_version": SCHEMA_VERSION, "overall": overall, "checks": checks}


def print_report(report, as_json=False):
    if as_json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return
    print(f"Parley doctor — {report['overall'].upper()}")
    for item in report["checks"]:
        print(f"  {item['status'].upper():4}  {item['summary']}")
        if item.get("action"):
            print(f"        {item['action']}")
        if item.get("data", {}).get("path"):
            print(f"        Path: {item['data']['path']}")
    counts = {
        status: sum(item["status"] == status for item in report["checks"])
        for status in ("pass", "warn", "fail")
    }
    print(
        f"Result: {counts['pass']} passed, {counts['warn']} warnings, "
        f"{counts['fail']} failures."
    )
