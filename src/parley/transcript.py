"""Reading the session transcript that Claude Code writes as JSONL."""
import json


def _text_of(entry):
    blocks = entry.get("message", {}).get("content", []) or []
    return "\n".join(
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def _is_turn_boundary(entry):
    """A real user message, not a tool result.

    Claude Code records tool results as user entries. Treating one as the start
    of a turn would anchor the reply to the wrong place.
    """
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return False
    return True


def final_reply(path):
    """(message_id, text) of the reply to the CURRENT turn, or ("", "").

    Anchored to the last real user message rather than to "newest assistant
    entry". The Stop hook can fire before this turn's reply is flushed, and the
    previous turn's reply is also new relative to whatever was last spoken — so
    comparing ids alone reads out the wrong turn.
    """
    try:
        raw = open(path, encoding="utf-8", errors="replace").readlines()
    except OSError:
        return "", ""

    entries = []
    for line in raw:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue

    boundary = -1
    for i, entry in enumerate(entries):
        if _is_turn_boundary(entry):
            boundary = i

    for entry in reversed(entries[boundary + 1:]):
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        text = _text_of(entry)
        message_id = entry.get("uuid")
        if text and isinstance(message_id, str) and message_id:
            return message_id, text
    return "", ""
