"""Listener state indicator: the tmux status badge and its JSON snapshot.

The tmux badge polls ``parley indicator <pane>``; the Claude Code band (the
mod under ``claude_mod/``) reads ``parley indicator --json <pane>``. Both are
drawn from :func:`snapshot`, so they never disagree.
"""
import re
import subprocess

COMMAND = "#(parley indicator"
BADGE_STYLE = "#[bg=#ff9e64,fg=#1a1b26,bold]"
BADGE_PATTERN = re.compile(
    r" ?#\[bg=#ff9e64,fg=#1a1b26,bold\]"
    r"#\(parley indicator(?: '[^']*')?\)#\[default\]"
)
MIN_STATUS_RIGHT_LENGTH = 220
# The Claude Code band rewrites bands/<pane key> every few seconds while it is
# drawn in that pane. A marker this fresh means the band covers the pane and
# the tmux badge stands aside; once Claude exits the marker goes stale (or is
# emptied) and the badge comes back.
BAND_FRESH_SECONDS = 15


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


STATES = {
    "listening": "🔴 PARLEY LISTENING",
    "sending": "⏳ PARLEY SENDING",
    "reconnecting": "🎧 PARLEY RECONNECTING",
    "speaking": "🔊 PARLEY SPEAKING · MIC READY",
    "ready": "🎙 PARLEY READY",
}


def snapshot(viewing_pane=""):
    """The listener's state as data, for the tmux badge and the Claude Code band.

    ``state`` is ``off`` when the badge would be blank: the listener is not
    running, or the viewing pane does not have Parley on. ``route`` says where
    a dictated message goes relative to the viewer: ``this`` pane, an
    ``other`` session (``label`` names it), an ``unavailable`` pane, or
    ``target`` when no viewing pane was given.
    """
    from parley import hooks, listen, relay

    off = {"state": "off", "route": "", "label": "", "pane": "",
           "warning": False, "text": ""}
    if not listen.is_running():
        # On a relay remote the listener runs on the speaker, which mirrors
        # its state here over the relay link.
        mirrored = relay.mirror()
        if not mirrored:
            return off
        if viewing_pane and not hooks.pane_is_on(viewing_pane):
            return off
        return _mirrored(viewing_pane, mirrored)
    if viewing_pane and not hooks.pane_is_on(viewing_pane):
        return off
    pane = listen.get_target()
    host, _remote_pane = relay.split_target(pane)
    if host:
        label = listen.target_label(pane)
    else:
        _target_session, label = _session(pane)
    if viewing_pane and pane == viewing_pane:
        route, warning = "this", False
    elif label:
        route, warning = ("other" if viewing_pane else "target"), bool(viewing_pane)
    else:
        route, warning = "unavailable", True
    listener = listen.listener_state()
    if listener == "capturing":
        state = "listening"
    elif listener in ("sending", "reconnecting"):
        state = listener
    elif listen.speaking():
        state = "speaking"
    else:
        state = "ready"
    snap = {"state": state, "route": route, "label": label, "pane": pane,
            "warning": warning, "wake": listen.WAKE, "send": listen.SEND,
            "stop": listen.STOP_TALKING}
    snap["text"] = _render(snap)
    return snap


def _mirrored(viewing_pane, mirrored):
    """A snapshot drawn from the state the speaker mirrored to this remote."""
    pane = str(mirrored.get("pane") or "")
    if pane:
        label = _session(pane)[1]
        if viewing_pane and pane == viewing_pane:
            route, warning = "this", False
        elif label:
            route, warning = ("other" if viewing_pane else "target"), bool(viewing_pane)
        else:
            route, warning = "unavailable", True
    else:
        # The target is on another machine, usually the speaker itself.
        label = str(mirrored.get("label") or mirrored.get("speaker") or "")
        route = "other" if viewing_pane else "target"
        warning = bool(viewing_pane)
    state = mirrored.get("state")
    if state not in STATES:
        state = "ready"
    snap = {"state": state, "route": route, "label": label, "pane": pane,
            "warning": warning,
            "wake": str(mirrored.get("wake") or ""),
            "send": str(mirrored.get("send") or ""),
            "stop": str(mirrored.get("stop") or ""),
            "speaker": str(mirrored.get("speaker") or "")}
    snap["text"] = _render(snap)
    return snap


def _render(snap):
    if snap["state"] == "off":
        return ""
    if snap["route"] == "this":
        target = " · THIS PANE"
    elif snap["route"] in ("other", "target"):
        target = f" · SENDS TO {snap['label']}"
    else:
        target = f" · TARGET {snap['pane'] or '(none)'} UNAVAILABLE"
    warning = "⚠ " if snap["warning"] else ""
    return f" {warning}{STATES[snap['state']]}{target} "


def band_covers(pane):
    """True while the Claude Code band is drawing Parley's state in ``pane``."""
    import time

    from parley import config, hooks

    if not pane:
        return False
    marker = config.STATE / "bands" / hooks.pane_key(pane)
    try:
        fresh = time.time() - marker.stat().st_mtime < BAND_FRESH_SECONDS
        return fresh and bool(marker.read_text().strip())
    except OSError:
        return False


def text(viewing_pane=""):
    """Badge content, including the listener's current operational state.

    Blank for a pane the Claude Code band already covers, so a Claude session
    never shows the state twice; Codex and other panes keep the badge.
    """
    if band_covers(viewing_pane):
        return ""
    return snapshot(viewing_pane)["text"]


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
