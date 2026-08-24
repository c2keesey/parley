"""Per-session state, the Claude Code hook handler, and hook installation."""
import json
import shutil
import sys
import time
from pathlib import Path

from claude_speak import config
from claude_speak.player import detach, drain, enqueue
from claude_speak.transcript import final_reply

SETTINGS = Path.home() / ".claude" / "settings.json"
COMMAND = "claude-speak hook"
EVENTS = {"SessionStart": 5, "Stop": 10}


def is_on(session_id):
    return bool(session_id) and (config.SESSIONS / session_id).exists()


def turn_on(session_id):
    config.SESSIONS.mkdir(parents=True, exist_ok=True)
    (config.SESSIONS / session_id).touch()
    cutoff = time.time() - 7 * 86400
    for marker in config.SESSIONS.iterdir():
        try:
            if marker.stat().st_mtime < cutoff:
                marker.unlink(missing_ok=True)
        except OSError:
            pass


def turn_off(session_id):
    (config.SESSIONS / session_id).unlink(missing_ok=True)


def speak_reply(session_id, transcript):
    """Speak this turn's reply once the transcript has settled."""
    seen = config.SPOKEN / session_id
    try:
        previous = seen.read_text().strip()
    except OSError:
        previous = ""

    message_id, text = "", ""
    deadline = time.time() + 8
    while time.time() < deadline:
        message_id, text = final_reply(transcript)
        if message_id and message_id != previous:
            break
        time.sleep(0.2)
    if not text or message_id == previous:
        config.log("no new reply to speak")
        return

    # A turn can emit interim text before its final message; let the transcript
    # settle so the last thing said is what gets read.
    for _ in range(6):
        time.sleep(0.4)
        newer_id, newer_text = final_reply(transcript)
        if not newer_id or newer_id == message_id:
            break
        message_id, text = newer_id, newer_text

    seen.parent.mkdir(parents=True, exist_ok=True)
    seen.write_text(message_id)
    enqueue(text)
    drain()


def handle(stream=None):
    """Entry point for both hooks. Reads the payload Claude Code sends on stdin."""
    try:
        raw = (stream or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return

    session_id = payload.get("session_id") or ""

    if payload.get("hook_event_name") == "SessionStart":
        if not session_id:
            return
        if not is_on(session_id):
            if not config.DEFAULT.exists():
                return
            turn_on(session_id)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": config.PROMPT,
        }}))
        return

    if not is_on(session_id):
        return
    transcript = payload.get("transcript_path") or ""
    detach(lambda: speak_reply(session_id, transcript))


def _load_settings():
    if not SETTINGS.exists():
        return {}
    try:
        return json.loads(SETTINGS.read_text())
    except ValueError:
        raise SystemExit(f"{SETTINGS} is not valid JSON; fix it before installing")


def _save_settings(data):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_suffix(".json.claude-speak-backup"))
    SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def install():
    """Add the hooks to settings.json, leaving any existing hooks alone."""
    data = _load_settings()
    hooks = data.setdefault("hooks", {})
    added = []
    for event, timeout in EVENTS.items():
        matchers = hooks.setdefault(event, [])
        if not matchers:
            matchers.append({"hooks": []})
        existing = [
            h for m in matchers for h in m.get("hooks", [])
            if COMMAND in h.get("command", "")
        ]
        if existing:
            continue
        matchers[0].setdefault("hooks", []).append(
            {"type": "command", "command": COMMAND, "timeout": timeout}
        )
        added.append(event)
    if added:
        _save_settings(data)
        print(f"Installed hooks: {', '.join(added)}")
    else:
        print("Hooks already installed")
    print(f"  {SETTINGS}")
    return added


def uninstall():
    data = _load_settings()
    hooks = data.get("hooks", {})
    removed = []
    for event in EVENTS:
        for matcher in hooks.get(event, []):
            before = matcher.get("hooks", [])
            after = [h for h in before if COMMAND not in h.get("command", "")]
            if len(after) != len(before):
                matcher["hooks"] = after
                removed.append(event)
        hooks[event] = [m for m in hooks.get(event, []) if m.get("hooks")]
        if not hooks.get(event):
            hooks.pop(event, None)
    if removed:
        _save_settings(data)
        print(f"Removed hooks: {', '.join(removed)}")
    else:
        print("No hooks to remove")
    return removed
