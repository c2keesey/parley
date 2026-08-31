import os
import shutil
import subprocess
from types import SimpleNamespace

from parley import hooks, indicator, runtime


def result(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode)


def status(listener="ready", pane="%42", queue=0, synthesis="idle",
           playback="idle", available=True, errors=None):
    return {
        "listener": {"state": listener},
        "target": {"id": pane, "available": available},
        "queue": {"depth": queue},
        "speech": {"synthesis": synthesis, "playback": playback},
        "errors": list(errors or []),
    }


def test_indicator_is_blank_when_listener_is_not_alive(monkeypatch):
    monkeypatch.setattr(runtime, "snapshot", lambda: status(listener="off"))
    monkeypatch.setattr(
        indicator, "_session",
        lambda pane: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    assert indicator.text() == ""


def test_indicator_surfaces_speech_error_without_response_content(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "snapshot",
        lambda: status(listener="off", errors=[{
            "code": "synthesis_failed",
            "component": "speech",
            "stage": "synthesize",
            "provider": "openai",
            "at": 1,
        }]),
    )

    assert indicator.text() == " ⚠ PARLEY SPEECH ERROR · RUN PARLEY STATUS "


def test_indicator_names_the_dictation_target(monkeypatch):
    monkeypatch.setattr(runtime, "snapshot", status)
    assert indicator.text() == " 🎙 PARLEY READY · SENDS TO %42 "


def test_indicator_makes_current_and_wrong_session_unmistakable(monkeypatch):
    monkeypatch.setattr(runtime, "snapshot", status)
    monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)

    assert indicator.text("%42") == " 🎙 PARLEY READY · THIS PANE "
    assert indicator.text("%77") == (
        " ⚠ 🎙 PARLEY READY · SENDS TO %42 "
    )


def test_indicator_is_blank_when_active_pane_has_parley_off(monkeypatch):
    monkeypatch.setattr(runtime, "snapshot", status)
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
        monkeypatch.setattr(
            runtime, "snapshot", lambda: status(pane=target_pane))
        monkeypatch.setattr(hooks, "pane_is_on", lambda pane: True)

        assert indicator.text(target_pane) == (
            " 🎙 PARLEY READY · THIS PANE "
        )
        current_pane = tmux(
            "display-message", "-p", "-t", "current-work", "#{pane_id}",
        ).stdout.strip()
        assert indicator.text(current_pane) == (
            f" ⚠ 🎙 PARLEY READY · SENDS TO {target_pane} "
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
    current = status(listener="capturing")
    monkeypatch.setattr(runtime, "snapshot", lambda: current)
    assert indicator.text() == " 🔴 PARLEY LISTENING · SENDS TO %42 "
    current["listener"]["state"] = "sending"
    assert indicator.text() == " ⏳ PARLEY SENDING · SENDS TO %42 "
    current["listener"]["state"] = "ready"
    current["speech"]["playback"] = "active"
    assert indicator.text() == (
        " 🔊 PARLEY SPEAKING · MIC READY · SENDS TO %42 "
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
