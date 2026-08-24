"""Harness integration: per-session state, the hook handler, and installation.

Deliberately harness agnostic. Claude Code and Codex both send a Stop-style
hook with a JSON payload, and both use the same settings schema, so one handler
serves either. The payload shapes differ — some harnesses hand over the reply
text directly, others hand over a path to a transcript — so the reply is read
through whichever shape arrives.

A session is identified by its terminal pane rather than by any harness's
session id. The pane is what voice actually addresses: it is where the reply is
spoken and where a dictated message is typed back. It also means the same code
works under a harness that exposes no session id at all.
"""
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from parley import config
from parley.player import detach, drain, enqueue
from parley.transcript import final_reply

# Both harnesses read this schema; only the file differs.
TARGETS = {
    "claude-code": Path.home() / ".claude" / "settings.json",
    "codex": Path.home() / ".codex" / "hooks.json",
}
COMMAND = "parley hook"
EVENT = "Stop"
TIMEOUT = 10

SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")
DIRECT_KEYS = ("last-assistant-message", "last_assistant_message",
               "assistant_message", "message", "text")


def _clean(value):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))


def session_keys(payload=None):
    """Every identity this session might be known by, best first.

    Both sides compute this list and a match on any key counts, so arming from
    the shell and firing from a hook agree even if one of them cannot see the
    pane or the harness session id.
    """
    keys = []
    pane = os.environ.get("TMUX_PANE")
    if pane:
        keys.append("pane-" + _clean(pane))
    for var in SESSION_VARS:
        if os.environ.get(var):
            keys.append("id-" + _clean(os.environ[var]))
    if payload:
        for key in ("session_id", "thread-id", "thread_id", "session"):
            if payload.get(key):
                keys.append("id-" + _clean(payload[key]))
    return keys or ["default"]


def is_on(keys):
    return any((config.SESSIONS / k).exists() for k in keys)


def turn_on(keys):
    config.SESSIONS.mkdir(parents=True, exist_ok=True)
    for key in keys:
        (config.SESSIONS / key).touch()
    cutoff = time.time() - 7 * 86400
    for marker in config.SESSIONS.iterdir():
        try:
            if marker.stat().st_mtime < cutoff:
                marker.unlink(missing_ok=True)
        except OSError:
            pass


def turn_off(keys):
    for key in keys:
        (config.SESSIONS / key).unlink(missing_ok=True)


def reply_from(payload):
    """(id, text) of the reply, however this harness chose to report it."""
    for key in DIRECT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            return f"direct-{digest}", text
    for key in ("transcript_path", "rollout_path", "transcript"):
        path = payload.get(key)
        if path:
            return final_reply(path)
    return "", ""


def speak_reply(marker_key, payload):
    """Speak this turn's reply, waiting for it if the hook fired early."""
    seen = config.SPOKEN / marker_key
    try:
        previous = seen.read_text().strip()
    except OSError:
        previous = ""

    reply_id, text = reply_from(payload)
    if not reply_id:
        # Transcript-based harnesses can fire before the reply is flushed.
        deadline = time.time() + 8
        while time.time() < deadline and not reply_id:
            time.sleep(0.2)
            reply_id, text = reply_from(payload)

    if not reply_id or reply_id == previous:
        config.log("no new reply to speak")
        return

    # A turn can emit interim text before its final message; let it settle so
    # the last thing said is what gets read.
    for _ in range(6):
        time.sleep(0.4)
        newer_id, newer_text = reply_from(payload)
        if not newer_id or newer_id == reply_id:
            break
        reply_id, text = newer_id, newer_text

    seen.parent.mkdir(parents=True, exist_ok=True)
    seen.write_text(reply_id)
    enqueue(text)
    drain()


def handle(stream=None, argv=None):
    """Entry point. Payload arrives on stdin, or as a JSON argument."""
    payload = {}
    raw = ""
    for candidate in (argv or []):
        if candidate.strip().startswith("{"):
            raw = candidate
            break
    if not raw:
        try:
            raw = (stream or sys.stdin).read()
        except OSError:
            return
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    if not isinstance(payload, dict):
        return

    keys = session_keys(payload)
    if not is_on(keys):
        return
    detach(lambda: speak_reply(keys[0], payload))


def _load(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        raise SystemExit(f"{path} is not valid JSON; fix it before installing")


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".parley-backup"))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


SKILL_DIRS = {
    "claude-code": Path.home() / ".claude" / "skills",
    "codex": Path.home() / ".codex" / "skills",
}


def install_skill(harness=None):
    """Copy the voice skill in, so the agent can be told to turn voice on.

    A skill rather than a session-start hook: voice gets switched on and off
    mid-session, and only the sessions that asked for it should carry the
    instructions.
    """
    source = Path(__file__).parent / "skills" / "voice" / "SKILL.md"
    if not source.exists():
        return []
    installed = []
    for name, root in SKILL_DIRS.items():
        if harness and harness != name:
            continue
        if not harness and not root.parent.exists():
            continue
        destination = root / "voice" / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        installed.append(name)
        print(f"{name}: skill installed ({destination})")
    return installed


def install(harness=None):
    """Add the Stop hook wherever the harness expects it. Idempotent."""
    installed = []
    for name, path in TARGETS.items():
        if harness and harness != name:
            continue
        if not harness and not path.parent.exists():
            continue
        data = _load(path)
        matchers = data.setdefault("hooks", {}).setdefault(EVENT, [])
        if not matchers:
            matchers.append({"hooks": []})
        already = any(
            COMMAND in hook.get("command", "")
            for matcher in matchers for hook in matcher.get("hooks", [])
        )
        if already:
            print(f"{name}: already installed ({path})")
            continue
        matchers[0].setdefault("hooks", []).append(
            {"type": "command", "command": COMMAND, "timeout": TIMEOUT})
        _save(path, data)
        installed.append(name)
        print(f"{name}: installed ({path})")
    install_skill(harness)
    if not installed and not harness:
        print("Hook already present everywhere it is wanted.")
    return installed


def uninstall(harness=None):
    removed = []
    for name, path in TARGETS.items():
        if harness and harness != name:
            continue
        if not path.exists():
            continue
        data = _load(path)
        hooks = data.get("hooks", {})
        changed = False
        for matcher in hooks.get(EVENT, []):
            before = matcher.get("hooks", [])
            after = [h for h in before if COMMAND not in h.get("command", "")]
            if len(after) != len(before):
                matcher["hooks"] = after
                changed = True
        if changed:
            hooks[EVENT] = [m for m in hooks.get(EVENT, []) if m.get("hooks")]
            if not hooks.get(EVENT):
                hooks.pop(EVENT, None)
            _save(path, data)
            removed.append(name)
            print(f"{name}: removed ({path})")
    if not removed:
        print("No hooks to remove")
    return removed
