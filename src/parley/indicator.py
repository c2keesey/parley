"""Reversible tmux status badge for the hands-free listener."""
import base64
import json
import re
import subprocess

COMMAND = "#(parley indicator"
BADGE_STYLE = "#[bg=#ff9e64,fg=#1a1b26,bold]"
BADGE_PATTERN = re.compile(
    r" ?#\[bg=#ff9e64,fg=#1a1b26,bold\]"
    r"#\(parley indicator(?: '[^']*')?\)#\[default\]"
)
MIN_STATUS_RIGHT_LENGTH = 220
STATE_OPTION = "@parley-indicator-state"
STATE_VERSION = 1


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


def _without_badge(value):
    """Remove only the exact status fragment owned by Parley."""
    return BADGE_PATTERN.sub("", value)


def text(viewing_pane=""):
    """Badge content, including the listener's current operational state."""
    from parley import hooks, listen

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


def _listed_sessions():
    listed = _run(["tmux", "list-sessions", "-F", "#{session_id}"])
    if not listed or listed.returncode != 0:
        return []
    return list(filter(None, listed.stdout.splitlines()))


def _enabled_sessions():
    """Session ids containing at least one pane explicitly enabled for Parley."""
    from parley import hooks

    listed = _run([
        "tmux", "list-panes", "-a", "-F", "#{session_id}\t#{pane_id}",
    ])
    if not listed or listed.returncode != 0:
        return set()
    enabled = set()
    for line in listed.stdout.splitlines():
        if "\t" not in line:
            continue
        session, pane = line.split("\t", 1)
        if hooks.pane_is_on(pane):
            enabled.add(session)
    return enabled


def _option(session, name):
    shown = _run(["tmux", "show-options", "-Av", "-t", session, name])
    if not shown or shown.returncode != 0:
        return None
    local = _run(["tmux", "show-options", "-q", "-t", session, name])
    is_local = bool(local and local.returncode == 0 and local.stdout)
    return shown.stdout.rstrip("\n"), is_local


def _set_option(session, name, value):
    result = _run(["tmux", "set-option", "-t", session, name, str(value)])
    return bool(result and result.returncode == 0)


def _unset_option(session, name):
    result = _run(["tmux", "set-option", "-u", "-t", session, name])
    return bool(result and result.returncode == 0)


def _encode_state(state):
    payload = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(payload).decode()


def _decode_state(value):
    try:
        state = json.loads(base64.urlsafe_b64decode(value).decode())
    except (ValueError, UnicodeError):
        return None
    required = {
        "version", "status_right", "status_right_local",
        "status_right_length", "status_right_length_local", "length_changed",
    }
    if not isinstance(state, dict) or set(state) != required:
        return None
    if state["version"] != STATE_VERSION:
        return None
    return state


def _state(session):
    shown = _run([
        "tmux", "show-options", "-qv", "-t", session, STATE_OPTION,
    ])
    if not shown or shown.returncode != 0 or not shown.stdout.strip():
        return None
    return _decode_state(shown.stdout.strip())


def _install(session):
    status_option = _option(session, "status-right")
    length_option = _option(session, "status-right-length")
    if status_option is None or length_option is None:
        return 0
    current, current_local = status_option
    try:
        length, length_local = int(length_option[0]), length_option[1]
    except ValueError:
        return 0

    state = _state(session)
    created = state is None
    if created:
        state = {
            "version": STATE_VERSION,
            "status_right": _without_badge(current),
            "status_right_local": current_local,
            "status_right_length": length,
            "status_right_length_local": length_local,
            "length_changed": length < MIN_STATUS_RIGHT_LENGTH,
        }
        # Record restoration data before changing either user-facing option.
        if not _set_option(session, STATE_OPTION, _encode_state(state)):
            return 0

    base = _without_badge(current)
    wanted = f"{base} {_badge()}" if base else _badge()
    changed = 0
    if wanted != current and _set_option(session, "status-right", wanted):
        changed = 1
    # Once installed, a different length is a user edit and must win over us.
    if (created and state["length_changed"]
            and _set_option(
                session, "status-right-length", MIN_STATUS_RIGHT_LENGTH)):
        changed = 1
    return changed


def _remove(session):
    status_option = _option(session, "status-right")
    length_option = _option(session, "status-right-length")
    if status_option is None or length_option is None:
        return 0
    current, _current_local = status_option
    try:
        length, length_local = int(length_option[0]), length_option[1]
    except ValueError:
        return 0

    state = _state(session)
    cleaned = _without_badge(current)
    changed = 0
    if cleaned != current:
        if (state and not state["status_right_local"]
                and cleaned == state["status_right"]):
            changed = int(_unset_option(session, "status-right"))
        else:
            changed = int(_set_option(session, "status-right", cleaned))

    if (state and state["length_changed"] and length_local
            and length == MIN_STATUS_RIGHT_LENGTH):
        if state["status_right_length_local"]:
            restored = _set_option(
                session, "status-right-length", state["status_right_length"])
        else:
            restored = _unset_option(session, "status-right-length")
        changed = max(changed, int(restored))

    if state:
        _unset_option(session, STATE_OPTION)
    return changed


def ensure():
    """Reconcile the badge onto only sessions with Parley-enabled panes."""
    enabled = _enabled_sessions()
    changed = 0
    for session in _listed_sessions():
        changed += _install(session) if session in enabled else _remove(session)
    refresh()
    return changed


def cleanup():
    """Remove Parley-owned badge state from every session on this server."""
    changed = sum(_remove(session) for session in _listed_sessions())
    refresh()
    return changed
