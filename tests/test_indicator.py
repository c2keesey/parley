import os
import shutil
import subprocess
from types import SimpleNamespace

from parley import hooks, indicator, listen


def result(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode)


def test_indicator_is_blank_when_listener_is_not_alive(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 0)
    monkeypatch.setattr(
        indicator, "_session",
        lambda pane: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    assert indicator.text() == ""


def test_indicator_names_the_dictation_target(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(indicator, "_session", lambda pane: ("$9", "windy-falcon"))
    assert indicator.text() == " 🎙 PARLEY READY · SENDS TO windy-falcon "


def test_indicator_makes_current_and_wrong_session_unmistakable(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(indicator, "_session", lambda pane: ("$9", "windy-falcon"))
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)

    assert indicator.text("%42") == " 🎙 PARLEY READY · THIS PANE "
    assert indicator.text("%77") == (
        " ⚠ 🎙 PARLEY READY · SENDS TO windy-falcon "
    )


def test_indicator_is_blank_when_active_pane_has_parley_off(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: False)
    monkeypatch.setattr(
        indicator, "_session",
        lambda pane: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )

    assert indicator.text("%77") == ""


def test_real_tmux_sessions_surface_a_stale_target(monkeypatch):
    """Exercise the actual tmux identity boundary that made this bug silent."""
    if not shutil.which("tmux"):
        return
    socket = f"parley-indicator-test-{os.getpid()}"

    def tmux(*args):
        return subprocess.run(
            ["tmux", "-L", socket, *args], capture_output=True, text=True,
            check=False,
        )

    assert tmux("new-session", "-d", "-s", "old-target").returncode == 0
    try:
        assert tmux("new-session", "-d", "-s", "current-work").returncode == 0
        target_pane = tmux(
            "display-message", "-p", "-t", "old-target", "#{pane_id}",
        ).stdout.strip()
        monkeypatch.setattr(
            indicator, "_run", lambda argv, timeout=2: tmux(*argv[1:]))
        monkeypatch.setattr(listen, "is_running", lambda: 123)
        monkeypatch.setattr(listen, "get_target", lambda: target_pane)
        monkeypatch.setattr(listen, "listener_state", lambda: "ready")
        monkeypatch.setattr(listen, "speaking", lambda: False)
        monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)

        assert indicator.text(target_pane) == (
            " 🎙 PARLEY READY · THIS PANE "
        )
        current_pane = tmux(
            "display-message", "-p", "-t", "current-work", "#{pane_id}",
        ).stdout.strip()
        assert indicator.text(current_pane) == (
            " ⚠ 🎙 PARLEY READY · SENDS TO old-target "
        )

        # The stored command keeps pane_id as a tmux format, so it follows the
        # active window instead of freezing the pane that ensure() first saw.
        assert "#{pane_id}" in indicator._badge()
        assert tmux(
            "new-window", "-t", "current-work", "-n", "switched",
        ).returncode == 0
        switched_pane = tmux(
            "display-message", "-p", "-t", "current-work", "#{pane_id}",
        ).stdout.strip()
        assert switched_pane != current_pane
    finally:
        tmux("kill-server")


def test_indicator_distinguishes_capture_send_and_speech(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(indicator, "_session", lambda pane: ("$9", "windy-falcon"))
    monkeypatch.setattr(listen, "speaking", lambda: False)

    monkeypatch.setattr(listen, "listener_state", lambda: "capturing")
    assert indicator.text() == " 🔴 PARLEY LISTENING · SENDS TO windy-falcon "
    monkeypatch.setattr(listen, "listener_state", lambda: "sending")
    assert indicator.text() == " ⏳ PARLEY SENDING · SENDS TO windy-falcon "
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen, "speaking", lambda: True)
    assert indicator.text() == (
        " 🔊 PARLEY SPEAKING · MIC READY · SENDS TO windy-falcon "
    )


def test_agent_deck_tmux_identity_becomes_a_readable_label(monkeypatch):
    monkeypatch.setattr(
        indicator, "_run",
        lambda argv, timeout=2: result("$9\tagentdeck_c2k-8_695c9762\n"),
    )
    assert indicator.session_label("%42") == "c2k-8"


def test_ensure_appends_badge_to_every_session_and_is_idempotent(monkeypatch):
    calls = []
    bars = {
        "one": "existing one",
        "two": (
            "existing two #[bg=#ff9e64,fg=#1a1b26,bold]"
            "#(parley indicator)#[default]"
        ),
    }

    def run(argv, timeout=2):
        calls.append(argv)
        if argv[1:3] == ["list-sessions", "-F"]:
            return result("one\ntwo\n")
        if argv[1:4] == ["show-options", "-v", "-t"]:
            session, option = argv[4], argv[5]
            return result((bars[session] if option == "status-right" else "100") + "\n")
        if argv[1:3] == ["set-option", "-t"]:
            session, option, value = argv[3], argv[4], argv[5]
            if option == "status-right":
                bars[session] = value
            return result()
        return result()

    monkeypatch.setattr(indicator, "_run", run)
    assert indicator.ensure() == 2
    assert bars["one"] == f"existing one {indicator._badge()}"
    assert bars["two"] == f"existing two {indicator._badge()}"
    assert "#{pane_id}" in bars["one"]
    length_updates = [
        call for call in calls
        if call[1:3] == ["set-option", "-t"]
        and call[4] == "status-right-length"
    ]
    assert {call[3] for call in length_updates} == {"one", "two"}

    calls.clear()
    assert indicator.ensure() == 0
    assert not any(
        call[1:3] == ["set-option", "-t"] and call[4] == "status-right"
        for call in calls
    )


def test_json_snapshot_carries_the_state_the_band_draws(monkeypatch, capsys):
    import json

    from parley import cli

    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(listen, "listener_state", lambda: "capturing")
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(indicator, "_session", lambda pane: ("$9", "windy-falcon"))
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)

    cli.main(["indicator", "--json", "%42"])
    snap = json.loads(capsys.readouterr().out)

    assert snap["state"] == "listening"
    assert snap["route"] == "this"
    assert snap["warning"] is False
    assert snap["text"] == " 🔴 PARLEY LISTENING · THIS PANE "
    assert snap["wake"] == listen.WAKE and snap["send"] == listen.SEND


def test_json_snapshot_is_off_when_the_listener_is_down(monkeypatch, capsys):
    import json

    from parley import cli

    monkeypatch.setattr(listen, "is_running", lambda: 0)

    cli.main(["indicator", "--json", "%42"])

    assert json.loads(capsys.readouterr().out)["state"] == "off"


def test_speaking_and_other_session_states(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen, "speaking", lambda: True)
    monkeypatch.setattr(indicator, "_session", lambda pane: ("$9", "windy-falcon"))
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)

    snap = indicator.snapshot("%77")

    assert snap["state"] == "speaking"
    assert snap["route"] == "other" and snap["warning"] is True
    assert snap["text"] == " ⚠ 🔊 PARLEY SPEAKING · MIC READY · SENDS TO windy-falcon "


def _live(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen, "speaking", lambda: False)
    monkeypatch.setattr(indicator, "_session", lambda pane: ("$9", "windy-falcon"))
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)


def test_badge_stands_aside_for_a_pane_the_claude_band_covers(
        monkeypatch, tmp_path):
    from parley import config

    _live(monkeypatch)
    monkeypatch.setattr(config, "STATE", tmp_path)
    marker = tmp_path / "bands" / hooks.pane_key("%42")
    marker.parent.mkdir()
    marker.write_text("1")

    assert indicator.text("%42") == ""
    # Codex or a plain shell in another pane keeps its badge.
    assert indicator.text("%77").startswith(" ⚠ 🎙 PARLEY READY")
    # The band's own snapshot is never suppressed.
    assert indicator.snapshot("%42")["state"] == "ready"


def test_badge_returns_once_the_band_marker_is_stale_or_emptied(
        monkeypatch, tmp_path):
    import os
    import time

    from parley import config

    _live(monkeypatch)
    monkeypatch.setattr(config, "STATE", tmp_path)
    marker = tmp_path / "bands" / hooks.pane_key("%42")
    marker.parent.mkdir()
    marker.write_text("1")
    old = time.time() - indicator.BAND_FRESH_SECONDS - 1
    os.utime(marker, (old, old))
    assert indicator.text("%42") == " 🎙 PARLEY READY · THIS PANE "

    marker.write_text("")
    assert indicator.text("%42") == " 🎙 PARLEY READY · THIS PANE "
