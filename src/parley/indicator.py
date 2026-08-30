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


def _badge():
    # tmux expands pane_id in the context of each session's active window.
    # Keeping the format expression in status-right makes the badge follow
    # pane/window switches without rewriting the option.
    command = "#(parley indicator '#{pane_id}')"
    return f"{BADGE_STYLE}{command}#[default]"


def text(viewing_pane=""):
    """Badge content, including the listener's current operational state."""
    from parley import hooks, listen
    from parley.player import speech_error

    if speech_error():
        if viewing_pane and not hooks.pane_is_on(viewing_pane):
            return ""
        return " ⚠ PARLEY SPEECH ERROR · RUN PARLEY STATUS "

    if not listen.is_running():
        return ""
    if viewing_pane and not hooks.pane_is_on(viewing_pane):
        return ""
    pane = listen.get_target()
    _target_session, label = _session(pane)
    if viewing_pane and pane == viewing_pane:
        target = " · THIS PANE"
        warning = ""
    elif label:
        target = f" · SENDS TO {label}"
        warning = "⚠ " if viewing_pane else ""
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
        wanted = _badge()
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
