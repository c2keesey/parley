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
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from parley import __version__, config
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


def pane_key(pane):
    """The marker name shared by shell commands, hooks, and the tmux badge."""
    return "pane-" + _clean(pane)


def pane_is_on(pane):
    return bool(pane) and is_on([pane_key(pane)])


def session_keys(payload=None):
    """Every identity this session might be known by, best first.

    Both sides compute this list and a match on any key counts, so arming from
    the shell and firing from a hook agree even if one of them cannot see the
    pane or the harness session id.
    """
    keys = []
    pane = os.environ.get("TMUX_PANE")
    if pane:
        keys.append(pane_key(pane))
    for var in SESSION_VARS:
        if os.environ.get(var):
            keys.append("id-" + _clean(os.environ[var]))
    if payload:
        for key in ("session_id", "thread-id", "thread_id", "session"):
            if payload.get(key):
                keys.append("id-" + _clean(payload[key]))
    return keys or ["default"]


def session_label(payload=None):
    """A short, speakable name for the session that produced a reply."""
    override = os.environ.get("PARLEY_SESSION_NAME", "").strip()
    if override:
        return override

    payload = payload or {}
    for key in ("session_name", "sessionName", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    pane = os.environ.get("TMUX_PANE")
    if pane:
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane, "#S"],
                capture_output=True, text=True, timeout=1, check=False,
            )
            name = result.stdout.strip()
            if name:
                # Agent Deck's tmux identity is agentdeck_<title>_<8hex>.
                # Keep the title, which is the name the user sees in the deck.
                if name.startswith("agentdeck_"):
                    name = name[len("agentdeck_"):]
                    name = re.sub(r"_[0-9a-fA-F]{8}$", "", name)
                return name.replace("_", " ")
        except (OSError, subprocess.SubprocessError):
            pass

    cwd = payload.get("cwd") or os.environ.get("PWD", "")
    if cwd:
        return Path(str(cwd)).name
    return "unknown"


def label_reply(text, payload=None):
    """Make concurrent spoken sessions distinguishable without rewriting."""
    return f"Session {session_label(payload)}. {text}"


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
    enqueue(label_reply(text, payload))
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


SKILL_DIRS = {
    "claude-code": Path.home() / ".claude" / "skills",
    "codex": Path.home() / ".codex" / "skills",
}


@dataclass
class InstallPlan:
    name: str
    settings: Path
    skill: Path
    data: dict
    hook_action: str
    skill_action: str

    @property
    def changes(self):
        return self.hook_action != "unchanged" or self.skill_action != "unchanged"


def _skill_source():
    source = Path(__file__).parent / "skills" / "parley" / "SKILL.md"
    if not source.is_file():
        raise SystemExit(f"bundled Parley skill is missing: {source}")
    return source


def _selected_harnesses(harness=None):
    if harness:
        return [harness]
    return [
        name for name, settings in TARGETS.items()
        if settings.parent.exists() or SKILL_DIRS[name].parent.exists()
    ]


def _load(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(
            f"{path} cannot be read as valid JSON; no changes made ({exc})"
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object; no changes made")
    _matchers(data, path)
    return data


def _matchers(data, path):
    """Validate and return the Stop matcher list without changing *data*."""
    if "hooks" not in data:
        return []
    hook_groups = data["hooks"]
    if not isinstance(hook_groups, dict):
        raise SystemExit(f"{path}: 'hooks' must be an object; no changes made")
    if EVENT not in hook_groups:
        return []
    matchers = hook_groups[EVENT]
    if not isinstance(matchers, list):
        raise SystemExit(
            f"{path}: hooks.{EVENT} must be a list; no changes made"
        )
    for index, matcher in enumerate(matchers):
        if not isinstance(matcher, dict):
            raise SystemExit(
                f"{path}: hooks.{EVENT}[{index}] must be an object; "
                "no changes made"
            )
        if "hooks" not in matcher:
            continue
        entries = matcher["hooks"]
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict) for entry in entries
        ):
            raise SystemExit(
                f"{path}: hooks.{EVENT}[{index}].hooks must be a list of "
                "objects; no changes made"
            )
        if any(
            "command" in entry and not isinstance(entry["command"], str)
            for entry in entries
        ):
            raise SystemExit(
                f"{path}: hooks.{EVENT}[{index}] commands must be strings; "
                "no changes made"
            )
    return matchers


def _is_parley_hook(entry):
    return (
        entry.get("type") == "command"
        and entry.get("command", "").strip() == COMMAND
    )


def _hook_action(data, path):
    entries = [
        entry
        for matcher in _matchers(data, path)
        for entry in (matcher.get("hooks") or [])
        if _is_parley_hook(entry)
    ]
    expected = {"type": "command", "command": COMMAND, "timeout": TIMEOUT}
    if not entries:
        return "add"
    if len(entries) == 1 and entries[0] == expected:
        return "unchanged"
    return "repair/update"


def _skill_action(destination, source):
    if not destination.exists():
        return "add"
    if not destination.is_file():
        raise SystemExit(
            f"{destination} is not a regular file; no changes made"
        )
    try:
        current = destination.read_bytes() == source.read_bytes()
        return "unchanged" if current else "update"
    except OSError as exc:
        raise SystemExit(
            f"cannot inspect {destination}; no changes made ({exc})"
        ) from exc


def _plans(harness=None):
    """Inspect every selected harness before any caller is allowed to write."""
    source = _skill_source()
    plans = []
    for name in _selected_harnesses(harness):
        settings = TARGETS[name]
        skill = SKILL_DIRS[name] / "parley" / "SKILL.md"
        data = _load(settings)
        plans.append(InstallPlan(
            name=name,
            settings=settings,
            skill=skill,
            data=data,
            hook_action=_hook_action(data, settings),
            skill_action=_skill_action(skill, source),
        ))
    return plans


def _backup_path(path):
    return path.with_suffix(path.suffix + ".parley-backup")


def _atomic_write(path, content, *, default_mode, backup=True):
    """Replace a file without a partial-write window and retain its mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = default_mode
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        backup_path = _backup_path(path)
        if backup and not backup_path.exists():
            # This is the pre-Parley recovery point, not a rolling snapshot.
            # Updates and uninstall must not replace it with an intermediate
            # configuration that already contains the Parley integration.
            shutil.copy2(path, backup_path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _save(path, data):
    content = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    _atomic_write(path, content, default_mode=0o600)


def _write_skill(destination):
    _atomic_write(
        destination,
        _skill_source().read_bytes(),
        default_mode=0o644,
    )


def _add_or_repair_hook(data):
    hook_groups = data.setdefault("hooks", {})
    matchers = hook_groups.setdefault(EVENT, [])
    for matcher in matchers:
        if matcher.get("hooks") is not None:
            matcher["hooks"] = [
                entry for entry in matcher["hooks"] if not _is_parley_hook(entry)
            ]
    if not matchers:
        matchers.append({"hooks": []})
    destination = next(
        (matcher for matcher in matchers if matcher.get("hooks") is not None),
        matchers[0],
    )
    if destination.get("hooks") is None:
        destination["hooks"] = []
    destination["hooks"].append(
        {"type": "command", "command": COMMAND, "timeout": TIMEOUT}
    )


def _report_plans(plans, operation, *, dry_run):
    qualifier = "dry-run; no changes" if dry_run else "validated plan"
    print(f"Parley {__version__} {operation} preflight ({qualifier})")
    if not plans:
        print("  no Claude Code or Codex configuration directories detected")
    for plan in plans:
        print(f"{plan.name}:")
        print(f"  settings {plan.settings} — hook {plan.hook_action}")
        _report_backup(plan.settings, plan.hook_action != "unchanged")
        print(f"  skill    {plan.skill} — skill {plan.skill_action}")
        _report_backup(plan.skill, plan.skill_action != "unchanged")


def _report_backup(path, will_replace):
    backup = _backup_path(path)
    if backup.exists():
        action = "preserve existing recovery snapshot"
    elif will_replace and path.exists():
        action = "create recovery snapshot"
    else:
        action = "not needed"
    print(f"  backup   {backup} — {action}")


def _report_prerequisites():
    print("Prerequisites for hands-free input:")
    print(f"  macOS — {'ready' if platform.system() == 'Darwin' else 'required'}")
    for executable, reason in (
        ("tmux", "session input routing"),
        ("ffmpeg", "microphone capture and speech resume"),
        ("whisper-cli", "local trigger recognition"),
    ):
        found = shutil.which(executable)
        print(f"  {executable} — {found if found else 'missing'} ({reason})")
    if platform.system() == "Darwin":
        print(
            "Microphone permission: macOS grants access to your terminal app "
            "on first use. Review or revoke it in System Settings > Privacy & "
            "Security > Microphone."
        )


def preflight(harness=None):
    plans = _plans(harness)
    _report_plans(plans, "install/update", dry_run=True)
    _report_prerequisites()
    return plans


def install_skill(harness=None, dry_run=False):
    """Reconcile only the skill half of an installation."""
    plans = _plans(harness)
    changed = []
    for plan in plans:
        if plan.skill_action == "unchanged":
            continue
        if not dry_run:
            _write_skill(plan.skill)
        changed.append(plan.name)
        print(f"{plan.name}: skill {plan.skill_action} ({plan.skill})")
    return changed


def install(harness=None, dry_run=False, operation="install"):
    """Reconcile hooks and skills after validating every selected harness."""
    plans = _plans(harness)
    _report_plans(plans, operation, dry_run=dry_run)
    if dry_run:
        _report_prerequisites()
        return [plan.name for plan in plans if plan.changes]

    changed = []
    for plan in plans:
        if plan.hook_action != "unchanged":
            _add_or_repair_hook(plan.data)
            _save(plan.settings, plan.data)
        if plan.skill_action != "unchanged":
            _write_skill(plan.skill)
        if plan.changes:
            changed.append(plan.name)
            print(f"{plan.name}: reconciled")
        else:
            print(f"{plan.name}: already current")
    _report_prerequisites()
    return changed


def _remove_parley_hooks(data, path):
    hook_groups = data.get("hooks")
    if hook_groups is None:
        return False
    changed = False
    matchers = _matchers(data, path)
    for matcher in matchers:
        entries = matcher.get("hooks")
        if entries is None:
            continue
        retained = [entry for entry in entries if not _is_parley_hook(entry)]
        if len(retained) != len(entries):
            matcher["hooks"] = retained
            changed = True
    if changed:
        hook_groups[EVENT] = [
            matcher for matcher in matchers
            if matcher.get("hooks") is None or matcher.get("hooks")
        ]
        if not hook_groups[EVENT]:
            hook_groups.pop(EVENT)
        if not hook_groups:
            data.pop("hooks")
    return changed


def _skill_removable(destination):
    if not destination.exists():
        return False
    try:
        return (
            destination.is_file()
            and destination.read_bytes() == _skill_source().read_bytes()
        )
    except OSError as exc:
        raise SystemExit(
            f"cannot inspect {destination}; no changes made ({exc})"
        ) from exc


def uninstall(harness=None, dry_run=False):
    """Remove only exact Parley-owned integration files, never runtime state."""
    plans = _plans(harness)
    removals = []
    # Decide everything before writing, including whether a skill was modified.
    for plan in plans:
        hook_owned = any(
            _is_parley_hook(entry)
            for matcher in _matchers(plan.data, plan.settings)
            for entry in (matcher.get("hooks") or [])
        )
        skill_owned = _skill_removable(plan.skill)
        skill_modified = plan.skill.exists() and not skill_owned
        removals.append((plan, hook_owned, skill_owned, skill_modified))

    qualifier = "dry-run; no changes" if dry_run else "validated plan"
    print(f"Parley {__version__} uninstall preflight ({qualifier})")
    for plan, hook_owned, skill_owned, skill_modified in removals:
        hook_action = "remove" if hook_owned else "unchanged"
        if skill_owned:
            skill_action = "remove"
        elif skill_modified:
            skill_action = "retain modified file"
        else:
            skill_action = "unchanged"
        print(f"{plan.name}:")
        print(f"  settings {plan.settings} — hook {hook_action}")
        _report_backup(plan.settings, hook_owned)
        print(f"  skill    {plan.skill} — skill {skill_action}")
        _report_backup(plan.skill, False)

    changed = []
    if not dry_run:
        for plan, hook_owned, skill_owned, _ in removals:
            if hook_owned:
                _remove_parley_hooks(plan.data, plan.settings)
                _save(plan.settings, plan.data)
            if skill_owned:
                plan.skill.unlink()
                try:
                    plan.skill.parent.rmdir()
                except OSError:
                    pass
            if hook_owned or skill_owned:
                changed.append(plan.name)
                print(f"{plan.name}: Parley integration removed")
    print("Runtime data was left untouched (PARLEY_STATE and ~/.cache/parley).")
    return changed
