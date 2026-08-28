"""Persistent tmux status badge for the hands-free listener."""
import re
import subprocess

COMMAND = "#(parley indicator"
BADGE_STYLE = "#[bg=#ff9e64,fg=#1a1b26,bold]"
BADGE_PATTERN = re.compile(
    r" ?#\[bg=#ff9e64,fg=#1a1b26,bold\]"
    r"#\(parley indicator(?: '[^']*')?\)#\[default\]"
)
MIN_STATUS_RIGHT_LENGTH = 220


def _run(argv, timeout=2):
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _session(pane):
    result = _run([
        "tmux", "display-message", "-p", "-t", pane,
        "#{session_id}\t#{session_name}",
    ])
    value = result.stdout.strip() if result and result.returncode == 0 else ""
    if "\t" not in value:
        return "", ""
    session_id, name = value.split("\t", 1)
    if name.startswith("agentdeck_"):
        name = name[len("agentdeck_"):]
        name = re.sub(r"_[0-9a-fA-F]{8}$", "", name)
    return session_id, name.replace("_", " ")


def session_label(pane):
    """Resolve a pane id to a human-readable tmux session name."""
    return _session(pane)[1]


def _badge(session_id):
    command = f"#(parley indicator '{session_id}')"
    return f"{BADGE_STYLE}{command}#[default]"


def text(viewing_session=""):
    """Badge content, including the listener's current operational state."""
    from parley import listen

    if not listen.is_running():
        return ""
    pane = listen.get_target()
    target_session, label = _session(pane)
    if viewing_session and target_session == viewing_session:
        target = " · THIS SESSION"
        warning = ""
    elif label:
        target = f" · SENDS TO {label}"
        warning = "⚠ " if viewing_session else ""
    else:
        target = f" · TARGET {pane or '(none)'} UNAVAILABLE"
        warning = "⚠ "
    state = listen.listener_state()
    if state == "capturing":
        status = "🔴 PARLEY LISTENING"
    elif state == "sending":
        status = "⏳ PARLEY SENDING"
    elif listen.speaking():
        status = "🔊 PARLEY SPEAKING · MIC READY"
    else:
        status = "🎙 PARLEY READY"
    return f" {warning}{status}{target} "


def refresh():
    """Refresh the badge immediately after a state transition."""
    _run(["tmux", "refresh-client", "-S"])


def ensure():
    """Append the dynamic badge to every session on this tmux server."""
    listed = _run([
        "tmux", "list-sessions", "-F", "#{session_id}\t#{session_name}"])
    if not listed or listed.returncode != 0:
        return 0
    changed = 0
    for line in filter(None, listed.stdout.splitlines()):
        session_id, session = line.split("\t", 1)
        shown = _run([
            "tmux", "show-options", "-v", "-t", session, "status-right"])
        if not shown or shown.returncode != 0:
            continue
        current = shown.stdout.rstrip("\n")
        wanted = _badge(session_id)
        if COMMAND in current:
            updated = BADGE_PATTERN.sub(f" {wanted}", current).lstrip()
        else:
            updated = f"{current} {wanted}".lstrip()
        if updated != current:
            _run([
                "tmux", "set-option", "-t", session, "status-right",
                updated,
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
