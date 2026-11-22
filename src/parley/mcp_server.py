"""A stdio MCP server that gives the agent a `speak` tool.

In tool mode (`parley mode tool`) the Stop hook no longer reads the final reply
aloud. Speech becomes something the agent does on purpose, through this tool,
so the written reply is free to be a normal written reply — tables, code,
paths — while what the user hears is composed for the ear.

The harness launches this server as its own child, so it inherits the
session's environment: TMUX_PANE (and any harness session id) identify the
session exactly as they do for `parley say` and the Stop hook.

Hand-rolled JSON-RPC over newline-delimited stdio rather than an MCP SDK: the
protocol surface needed here is four methods, and Parley stays dependency-light.
"""
import json
import os
import sys

from parley import __version__, config, hooks

PROTOCOL_VERSION = "2025-06-18"

SPEAK_TOOL = {
    "name": "speak",
    "title": "Speak aloud",
    "description": (
        "Say something out loud to the user through Parley. This is the ONLY "
        "way the user hears you: your chat text is not read aloud. Pass plain "
        "conversational speech — what you would say to a colleague standing "
        "next to you — not the written reply. No markdown, code, URLs, or file "
        "paths; describe them instead. Keep it to a few sentences. Call it at "
        "the end of a turn to tell the user the outcome or ask what you need, "
        "and mid-task for a heads-up worth hearing. Speech queues behind "
        "anything already playing and returns immediately unless wait is true."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The words to speak, as natural spoken prose.",
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "Block until the speech has finished playing. Rarely "
                    "needed; use it only when the timing of what follows "
                    "depends on the user having heard this."
                ),
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
}


def speak(text, wait=False):
    """Queue text for this session. Returns (spoken, message)."""
    from parley import relay
    from parley.player import detach, drain, enqueue

    text = (text or "").strip()
    if not text:
        return False, "Nothing to speak: text was empty."
    keys = hooks.session_keys()
    if not hooks.is_on(keys):
        return False, ("Parley voice is off for this session, so nothing was "
                       "spoken. The user can turn it on with `parley on`.")
    label = hooks.session_label()
    host = relay.speaker()
    if host:
        if not relay.say(text, session=relay.remote_session(
                relay.self_address(), keys[0]), session_label=label, wait=wait):
            return False, f"Could not reach the Parley speaker {host}."
        return True, "Spoken." if wait else "Queued for speech."
    if not enqueue(text, session=keys[0], session_label=label):
        return False, "Nothing to speak: text was empty."
    if wait:
        drain()
        return True, "Spoken."
    detach(drain)
    return True, "Queued for speech."


def _result(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _call(params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name != SPEAK_TOOL["name"]:
        return _result(f"Unknown tool: {name}", error=True)
    text = arguments.get("text")
    if not isinstance(text, str):
        return _result("`text` must be a string.", error=True)
    try:
        spoken, message = speak(text, wait=bool(arguments.get("wait")))
    except Exception as exc:  # never take the server down with one bad call
        config.log(f"mcp speak failed: {exc!r}")
        return _result(f"Speech failed: {exc}", error=True)
    return _result(message, error=not spoken)


def respond(message):
    """The JSON-RPC response for one incoming message, or None."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # a notification (initialized, cancelled, ...)
    params = message.get("params") or {}

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "parley", "version": __version__},
            "instructions": (
                "Parley speaks to the user out loud. When the user has voice "
                "on, use the speak tool for anything they should hear."
            ),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [SPEAK_TOOL]}
    elif method == "tools/call":
        result = _call(params)
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _reap():
    """Collect finished speech drainers; this process outlives many of them."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = respond(message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
        _reap()
