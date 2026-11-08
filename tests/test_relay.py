"""Remote relay: agents on one machine, voice on another."""
import io
import json
import sys
import time
from types import SimpleNamespace

import pytest

from parley import cli, config, hooks, indicator, listen, player, relay


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(config, "SPOKEN", tmp_path / "spoken")
    monkeypatch.setattr(listen, "TARGET", tmp_path / "target")
    monkeypatch.setattr(indicator, "refresh", lambda: None)
    monkeypatch.setattr(indicator, "ensure", lambda: 0)
    return tmp_path


def test_remote_targets_round_trip_and_local_panes_stay_local():
    target = relay.remote_target("c2k@c2k-ms02", "%12")
    assert target == "remote:c2k@c2k-ms02/%12"
    assert relay.split_target(target) == ("c2k@c2k-ms02", "%12")
    assert relay.split_target("%12") == (None, "%12")
    assert relay.split_target("") == (None, "")
    assert relay.short_host("c2k@c2k-ms02.tail.net") == "c2k-ms02"


def test_speaker_comes_from_config_and_env_can_override(monkeypatch):
    assert relay.speaker() is None
    relay.configure("me@mac", "me@box")
    assert relay.speaker() == "me@mac"
    assert relay.self_address() == "me@box"
    assert (relay.RELAY_FILE.stat().st_mode & 0o777) == 0o600
    monkeypatch.setenv("PARLEY_RELAY", "off")
    assert relay.speaker() is None
    monkeypatch.setenv("PARLEY_RELAY", "other@mac")
    assert relay.speaker() == "other@mac"
    monkeypatch.delenv("PARLEY_RELAY")
    assert relay.disable()
    assert relay.speaker() is None


def test_call_reads_the_last_json_line_and_surfaces_refusals(monkeypatch):
    monkeypatch.setattr(relay, "_ensure_master", lambda host: None)
    outputs = iter([
        SimpleNamespace(stdout='motd noise\n{"ok": true, "v": 1}\n',
                        stderr="", returncode=0),
        SimpleNamespace(stdout='{"ok": false, "error": "no mic"}\n',
                        stderr="", returncode=0),
        SimpleNamespace(stdout="", stderr="zsh: command not found: parley",
                        returncode=127),
    ])
    seen = []
    monkeypatch.setattr(relay.subprocess, "run",
                        lambda argv, **kw: seen.append((argv, kw)) or next(outputs))

    assert relay.call("me@mac", ["say"], {"text": "hi"})["v"] == 1
    argv, kwargs = seen[0]
    assert argv[0] == "ssh" and "me@mac" in argv
    assert argv[-1].endswith("remote say")
    assert json.loads(kwargs["input"]) == {"text": "hi"}
    with pytest.raises(relay.RelayError, match="no mic"):
        relay.call("me@mac", ["listen-on"])
    with pytest.raises(relay.RelayError, match="not installed"):
        relay.call("me@mac", ["ping"])


def test_agent_answers_focus_labels_injection_and_mirrors_state(
        state, monkeypatch):
    monkeypatch.setattr(listen, "local_deck_focus", lambda: (500, "%7"))
    monkeypatch.setattr(indicator, "session_label", lambda pane: f"deck {pane}")
    pasted = []
    monkeypatch.setattr(listen, "paste_into",
                        lambda pane, text: pasted.append((pane, text)) or True)
    requests = [
        {"op": "hello"},
        {"op": "active"},
        {"op": "label", "pane": "%7"},
        {"op": "inject", "pane": "%7", "text": "ship it"},
        {"op": "mirror", "state": {"state": "listening", "pane": "%7"}},
    ]
    stdin = io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in requests))

    class Out(io.BytesIO):
        mirrored = None

        def flush(self):
            # Capture the mirror while the connection is still open.
            Out.mirrored = Out.mirrored or relay.mirror()

    stdout = Out()
    relay.serve_agent(stdin, stdout)

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert all(reply["ok"] for reply in replies)
    assert replies[1] == {"ok": True, "pane": "%7", "activity": 500}
    assert replies[2]["label"] == "deck %7"
    assert pasted == [("%7", "ship it")]
    assert Out.mirrored["state"] == "listening"
    # The speaker hanging up must not leave a ghost listener on screen.
    assert relay.mirror() is None


def test_stale_or_off_mirror_is_ignored(state):
    mirror = state / "relay-listener.json"
    mirror.write_text(json.dumps({"state": "ready", "received": time.time() - 60}))
    assert relay.mirror() is None
    mirror.write_text(json.dumps({"state": "off", "received": time.time()}))
    assert relay.mirror() is None
    mirror.write_text(json.dumps({"state": "ready", "received": time.time()}))
    assert relay.mirror()["state"] == "ready"


def test_agent_link_talks_to_a_real_agent_process(state, monkeypatch):
    """The speaker's client against a real `parley remote agent` subprocess."""
    script = (
        "from parley import relay, listen, indicator;"
        "listen.local_deck_focus = lambda: (42, '%3');"
        "indicator.ensure = lambda: 0; indicator.refresh = lambda: None;"
        "relay.serve_agent()"
    )
    monkeypatch.setenv("PARLEY_STATE", str(state / "remote"))
    monkeypatch.setattr(relay, "agent_argv",
                        lambda host: [sys.executable, "-c", script])
    link = relay.Agent("box")
    try:
        assert link.call({"op": "active"}) == {
            "ok": True, "pane": "%3", "activity": 42}
        assert link.call({"op": "mirror", "state": {"state": "ready"}})["ok"]
        assert (state / "remote" / "relay-listener.json").exists()
    finally:
        link.close()
    mirrored = state / "remote" / "relay-listener.json"
    deadline = time.time() + 5
    while time.time() < deadline and mirrored.exists():
        time.sleep(0.05)
    assert not mirrored.exists()


def test_agent_link_backs_off_when_the_remote_is_unreachable(monkeypatch):
    monkeypatch.setattr(relay, "agent_argv",
                        lambda host: [sys.executable, "-c", "import sys; sys.exit(1)"])
    monkeypatch.setattr(relay, "CONNECT_TIMEOUT", 2)
    link = relay.Agent("gone")
    assert link.call({"op": "active"}) is None
    started = time.monotonic()
    assert link.call({"op": "active"}) is None  # backing off, no new attempt
    assert time.monotonic() - started < 0.5


def test_mirrored_state_drives_the_remote_band(state, monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 0)
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)
    monkeypatch.setattr(indicator, "_session", lambda pane: ("$1", "deck title"))
    (state / "relay-listener.json").write_text(json.dumps({
        "state": "listening", "pane": "%7", "wake": "okay computer",
        "send": "send it", "stop": "stop talking", "speaker": "mac",
        "received": time.time()}))

    here = indicator.snapshot("%7")
    assert here["state"] == "listening" and here["route"] == "this"
    assert here["send"] == "send it"
    assert indicator.text("%7") == " 🔴 PARLEY LISTENING · THIS PANE "
    elsewhere = indicator.snapshot("%9")
    assert elsewhere["route"] == "other" and elsewhere["warning"]


def test_mirror_names_a_target_on_the_speaker(state, monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 0)
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)
    (state / "relay-listener.json").write_text(json.dumps({
        "state": "ready", "pane": "", "label": "c2k on mac",
        "received": time.time()}))
    snap = indicator.snapshot("%7")
    assert snap["route"] == "other" and snap["label"] == "c2k on mac"


def test_speaker_indicator_names_a_remote_target(state, monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen, "speaking", lambda: False)
    listen.set_target(relay.remote_target("c2k@box", "%7"))
    (state / "relay-target-label").write_text("parley relay on box")
    assert indicator.text() == " 🎙 PARLEY READY · SENDS TO parley relay on box "


def test_dictation_for_a_remote_pane_goes_over_the_link(monkeypatch):
    sent = []
    monkeypatch.setattr(listen, "sync_target",
                        lambda: relay.remote_target("c2k@box", "%7"))
    monkeypatch.setattr(
        relay, "inject",
        lambda host, pane, text: sent.append((host, pane, text)) or True)
    monkeypatch.setattr(listen, "paste_into",
                        lambda *a: pytest.fail("must not paste locally"))
    monkeypatch.setattr(listen.config, "log", lambda message: None)

    assert listen.inject("deploy   the\nfix")
    assert sent == [("c2k@box", "%7", "deploy the fix")]


def test_focus_follows_the_most_recent_client_across_machines(state, monkeypatch):
    monkeypatch.setattr(listen, "local_deck_focus", lambda: (100, "%2"))
    assert listen.active_deck_target() == "%2"  # no remotes: unchanged
    relay.add_host("c2k@box")
    assert listen.active_deck_target() == ""  # remote report overdue: hold
    relay._focus["c2k@box"] = (200, "%7", time.monotonic())
    assert listen.active_deck_target() == "remote:c2k@box/%7"
    relay._focus["c2k@box"] = (50, "%7", time.monotonic())
    assert listen.active_deck_target() == "%2"


def test_local_focus_ignores_a_pane_that_is_a_window_onto_another_machine(
        monkeypatch):
    output = (
        "/dev/ttys001\t%mosh\t900\tagentdeck_box_11111111\tmosh-client\n"
        "/dev/ttys002\t%real\t200\tagentdeck_real_22222222\tclaude\n"
    )
    monkeypatch.setattr(
        listen.subprocess, "run",
        lambda *a, **k: listen.subprocess.CompletedProcess(a[0], 0, stdout=output))
    assert listen.local_deck_focus() == (200, "%real")


def test_stopping_the_listener_forgets_linked_remotes(state, monkeypatch):
    monkeypatch.setattr(player, "resume", lambda: False)
    relay.add_host("c2k@box")
    listen.stop()
    assert relay.remote_hosts() == []


def test_hook_on_a_remote_forwards_the_reply_to_the_speaker(monkeypatch):
    relay.configure("me@mac", "me@box")
    said = []
    monkeypatch.setattr(relay, "say", lambda text, **kw: said.append((text, kw)))
    monkeypatch.setattr(hooks, "enqueue", lambda *a, **k: pytest.fail("local"))
    monkeypatch.setattr(hooks, "session_label", lambda payload=None: "deck one")
    monkeypatch.setattr(hooks.time, "sleep", lambda s: None)

    hooks.speak_reply("pane-_7", {"last_assistant_message": "All green."})

    assert said == [("All green.", {
        "session": "remote:me@box/pane-_7", "session_label": "deck one"})]


def test_speaker_say_spools_then_delivers_in_the_gui_session(state, monkeypatch):
    spawned = []
    monkeypatch.setattr(relay, "gui_spawn", lambda argv: spawned.append(argv) or "tmux")
    answer = relay._say({"text": "Tests pass.", "session": "remote:b/pane-_7",
                         "session_label": "deck one"})
    assert answer == {"ok": True, "queued": True, "via": "tmux"}
    assert spawned[0][-2:] == ["remote", "deliver"]

    queued = []
    monkeypatch.setattr(player, "enqueue", lambda text, **kw: queued.append((text, kw)))
    monkeypatch.setattr(player, "drain", lambda: True)
    relay.deliver()
    assert queued == [("Tests pass.", {
        "voice": None, "model": None, "session": "remote:b/pane-_7",
        "session_label": "deck one"})]
    assert not list((state / "relay-inbox").iterdir())


def test_listen_on_from_a_remote_retargets_a_running_listener(state, monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(relay, "gui_spawn",
                        lambda argv: pytest.fail("must not restart the mic"))
    answer = relay._listen_on("c2k@box", "%7", True, "default")
    assert answer["ok"] and answer["started"] is False
    assert listen.get_target() == "remote:c2k@box/%7"
    assert listen.follows_active()
    assert relay.remote_hosts() == ["c2k@box"]


def test_listen_on_refuses_without_a_gui_session(state, monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 0)
    monkeypatch.setattr(relay, "gui_available", lambda: False)
    answer = relay._listen_on("c2k@box", "%7", True, "default")
    assert not answer["ok"] and "microphone" in answer["error"]


def test_listen_off_only_stops_a_listener_owned_by_that_remote(state, monkeypatch):
    stops = []
    monkeypatch.setattr(listen, "stop", lambda: stops.append(1) or True)
    listen.set_target(relay.remote_target("c2k@box", "%7"))
    assert relay._listen_off("c2k@other", "%7", False) == {"ok": True, "stopped": False}
    assert relay._listen_off("c2k@box", "%9", False) == {"ok": True, "stopped": False}
    assert relay._listen_off("c2k@box", "%9", True) == {"ok": True, "stopped": True}
    assert relay._listen_off("c2k@box", "%7", False) == {"ok": True, "stopped": True}


def test_gui_spawn_hands_ssh_work_to_the_terminal_tmux_server(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")
    monkeypatch.setattr(relay, "tmux_bin", lambda: "/opt/homebrew/bin/tmux")
    runs = []
    monkeypatch.setattr(relay.subprocess, "run",
                        lambda argv, **kw: runs.append(argv)
                        or SimpleNamespace(returncode=0))
    monkeypatch.setattr(relay.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not start under sshd"))
    assert relay.gui_spawn(["/u/parley", "remote", "deliver"]) == "tmux"
    assert runs[0][:3] == ["/opt/homebrew/bin/tmux", "run-shell", "-b"]
    assert runs[0][3].startswith("/u/parley remote deliver ")


def test_parley_on_in_a_relay_remote_arms_the_speakers_listener(monkeypatch, capsys):
    relay.configure("me@mac", "me@box")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(listen, "is_agent_deck_pane", lambda pane: True)
    calls = []
    monkeypatch.setattr(relay, "listen_on", lambda pane, follow, device="default":
                        calls.append((pane, follow))
                        or {"ok": True, "wake": "okay computer", "send": "send it"})
    cli.main(["on"])
    out = capsys.readouterr().out
    assert calls == [("%7", True)]
    assert hooks.pane_is_on("%7")
    assert "the microphone is on me@mac" in out
    assert config.PROMPT in out


def test_parley_on_in_a_relay_remote_stays_off_when_the_speaker_fails(monkeypatch):
    relay.configure("me@mac", "me@box")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(listen, "is_agent_deck_pane", lambda pane: False)

    def refuse(*args, **kwargs):
        raise relay.RelayError("asleep")

    monkeypatch.setattr(relay, "listen_on", refuse)
    with pytest.raises(SystemExit, match="asleep"):
        cli.main(["on"])
    assert not hooks.pane_is_on("%7")
