"""Persistent tmux status badge for the hands-free listener."""
import re
import subprocess

COMMAND = "#(parley indicator)"
BADGE = f"#[bg=#ff9e64,fg=#1a1b26,bold]{COMMAND}#[default]"
MIN_STATUS_RIGHT_LENGTH = 220


def _run(argv, timeout=2):
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _session_label(pane):
    result = _run([
        "tmux", "display-message", "-p", "-t", pane, "#{session_name}"])
    name = result.stdout.strip() if result and result.returncode == 0 else ""
    if name.startswith("agentdeck_"):
        name = name[len("agentdeck_"):]
        name = re.sub(r"_[0-9a-fA-F]{8}$", "", name)
    return name.replace("_", " ")


def text():
    """Badge content, including the listener's current operational state."""
    from parley import listen

    if not listen.is_running():
        return ""
    label = _session_label(listen.get_target())
    target = f" → {label}" if label else ""
    state = listen.listener_state()
    if state == "capturing":
        status = "🔴 PARLEY LISTENING"
    elif state == "sending":
        status = "⏳ PARLEY SENDING"
    elif listen.speaking():
        status = "🔊 PARLEY SPEAKING · MIC READY"
    else:
        status = "🎙 PARLEY READY"
    return f" {status}{target} "


def refresh():
    """Refresh the badge immediately after a state transition."""
    _run(["tmux", "refresh-client", "-S"])


def ensure():
    """Append the dynamic badge to every session on this tmux server."""
    listed = _run(["tmux", "list-sessions", "-F", "#{session_name}"])
    if not listed or listed.returncode != 0:
        return 0
    changed = 0
    for session in filter(None, listed.stdout.splitlines()):
        shown = _run([
            "tmux", "show-options", "-v", "-t", session, "status-right"])
        if not shown or shown.returncode != 0:
            continue
        current = shown.stdout.rstrip("\n")
        if COMMAND not in current:
            _run([
                "tmux", "set-option", "-t", session, "status-right",
                f"{current} {BADGE}",
            ])
            changed += 1
        length = _run([
            "tmux", "show-options", "-v", "-t", session,
            "status-right-length",
        ])
        try:
            current_length = int(length.stdout.strip()) if length else 0
        except ValueError:
            current_length = 0
        if current_length < MIN_STATUS_RIGHT_LENGTH:
            _run([
                "tmux", "set-option", "-t", session, "status-right-length",
                str(MIN_STATUS_RIGHT_LENGTH),
            ])
    _run(["tmux", "refresh-client", "-S"])
    return changed
