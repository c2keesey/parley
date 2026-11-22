import io
import json

import pytest

from parley import cli, config, hooks, mcp_server, relay


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(config, "SPOKEN", tmp_path / "spoken")
    monkeypatch.setattr(config, "OUTPUT_MODE", tmp_path / "output-mode")
    monkeypatch.delenv("PARLEY_OUTPUT_MODE", raising=False)
    monkeypatch.setattr(hooks, "session_label", lambda payload=None: "demo")
    monkeypatch.setattr(hooks, "CODEX_CONFIG", tmp_path / "codex" / "config.toml")
    monkeypatch.setattr(hooks, "_claude", lambda: None)
    for var in hooks.SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TMUX_PANE", "%9")


@pytest.fixture
def queued(monkeypatch):
    calls = []
    import parley.player as player

    monkeypatch.setattr(player, "enqueue", lambda text, **kw: calls.append(
        (text, kw)) or True)
    monkeypatch.setattr(player, "detach", lambda fn: None)
    monkeypatch.setattr(player, "drain", lambda: None)
    return calls


def rpc(*messages):
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def call_speak(**arguments):
    return {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "speak", "arguments": arguments}}


def test_handshake_and_tool_listing():
    init, listing = rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["serverInfo"]["name"] == "parley"
    tools = listing["result"]["tools"]
    assert [t["name"] for t in tools] == ["speak"]
    assert tools[0]["inputSchema"]["required"] == ["text"]


def test_speak_queues_for_this_pane_when_voice_is_on(queued):
    hooks.turn_on(hooks.session_keys())
    (reply,) = rpc(call_speak(text="Tests are green."))
    assert reply["result"]["isError"] is False
    assert queued == [("Tests are green.",
                       {"session": "pane-_9", "session_label": "demo"})]


def test_speak_refuses_politely_when_voice_is_off(queued):
    (reply,) = rpc(call_speak(text="Hello"))
    assert reply["result"]["isError"] is True
    assert "voice is off" in reply["result"]["content"][0]["text"]
    assert queued == []


def test_speak_rejects_empty_or_bad_text(queued):
    hooks.turn_on(hooks.session_keys())
    empty, bad = rpc(call_speak(text="  "), call_speak(text=5))
    assert empty["result"]["isError"] and bad["result"]["isError"]
    assert queued == []


def test_speak_relays_on_a_remote(monkeypatch, queued):
    hooks.turn_on(hooks.session_keys())
    monkeypatch.setattr(relay, "speaker", lambda: "me@mac")
    monkeypatch.setattr(relay, "self_address", lambda: "me@box")
    sent = []
    monkeypatch.setattr(relay, "say", lambda text, **kw: sent.append(text) or True)
    (reply,) = rpc(call_speak(text="From the box."))
    assert reply["result"]["isError"] is False
    assert sent == ["From the box."] and queued == []


def test_unknown_method_and_garbage_lines_get_errors():
    stdout = io.StringIO()
    mcp_server.serve(io.StringIO("not json\n" + json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": "resources/list"}) + "\n"), stdout)
    parse, unknown = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert parse["error"]["code"] == -32700
    assert unknown["error"]["code"] == -32601 and unknown["id"] == 7


def test_mode_defaults_to_reply_and_persists_tool(capsys):
    assert config.output_mode() == "reply"
    cli.main(["mode", "tool"])
    assert config.output_mode() == "tool"
    assert "speak tool" in capsys.readouterr().out
    cli.main(["mode", "reply"])
    assert config.output_mode() == "reply"
    assert not config.OUTPUT_MODE.exists()


def test_environment_overrides_the_saved_mode(monkeypatch):
    config.set_output_mode("tool")
    monkeypatch.setenv("PARLEY_OUTPUT_MODE", "reply")
    assert config.output_mode() == "reply"


def test_prompt_follows_the_mode():
    assert config.prompt() == config.PROMPT
    config.set_output_mode("tool")
    assert config.prompt() == config.TOOL_PROMPT
    assert "speak" in config.TOOL_PROMPT


def test_stop_hook_stays_silent_in_tool_mode(monkeypatch):
    hooks.turn_on(hooks.session_keys())
    detached = []
    monkeypatch.setattr(hooks, "detach", detached.append)
    hooks.handle(argv=[json.dumps({"last_assistant_message": "Done."})])
    assert len(detached) == 1
    config.set_output_mode("tool")
    hooks.handle(argv=[json.dumps({"last_assistant_message": "Done again."})])
    assert len(detached) == 1


def test_codex_registration_is_idempotent_and_reversible():
    path = hooks.CODEX_CONFIG
    path.parent.mkdir(parents=True)
    path.write_text('model = "o4"\n')
    assert hooks.install_mcp("codex") == ["codex"]
    assert hooks.install_mcp("codex") == []
    text = path.read_text()
    assert text.count("[mcp_servers.parley]") == 1
    assert text.startswith('model = "o4"\n')
    assert hooks.uninstall_mcp("codex") == ["codex"]
    assert path.read_text().strip() == 'model = "o4"'


def test_claude_registration_uses_the_cli(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(hooks, "_claude", lambda: "/bin/claude")
    monkeypatch.setattr(hooks, "TARGETS", {"claude-code": hooks.CODEX_CONFIG,
                                           "codex": hooks.CODEX_CONFIG})
    calls = []

    def fake(claude, *args):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[1] == "get" else 0,
                               stdout="", stderr="")

    monkeypatch.setattr(hooks, "_claude_cli", fake)
    assert hooks.install_mcp("claude-code") == ["claude-code"]
    assert calls[-1] == ("mcp", "add", "--scope", "user", "parley",
                         "--", "parley", "mcp")
