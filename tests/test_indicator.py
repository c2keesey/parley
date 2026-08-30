import os
import shutil
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from parley import config, hooks, indicator, listen


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
    socket = f"parley-indicator-test-{os.getpid()}-{uuid.uuid4().hex}"

    def tmux(*args):
        return subprocess.run(
            ["tmux", "-f", "/dev/null", "-L", socket, *args],
            capture_output=True, text=True, check=False,
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


@pytest.fixture
def isolated_tmux(tmp_path, monkeypatch):
    """A tmux server created and destroyed by this test process only."""
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket = f"parley-9ka14-test-{os.getpid()}-{uuid.uuid4().hex}"

    def tmux(*args):
        return subprocess.run(
            ["tmux", "-f", "/dev/null", "-L", socket, *args],
            capture_output=True, text=True, check=False,
        )

    monkeypatch.setattr(
        indicator, "_run", lambda argv, timeout=2: tmux(*argv[1:]))
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    try:
        yield tmux
    finally:
        tmux("kill-server")


def _tmux_option(tmux, session, option, *, local=False):
    flags = ["show-options", "-qv" if local else "-Av"]
    return tmux(*flags, "-t", session, option).stdout.rstrip("\n")


def test_indicator_lifecycle_is_opt_in_idempotent_and_reversible(isolated_tmux):
    tmux = isolated_tmux
    assert tmux("new-session", "-d", "-s", "opted-in").returncode == 0
    assert tmux("new-session", "-d", "-s", "unrelated").returncode == 0
    pane = tmux(
        "display-message", "-p", "-t", "opted-in", "#{pane_id}",
    ).stdout.strip()
    hooks.turn_on([hooks.pane_key(pane)])

    custom = "USER-CUSTOM " + ("status-content-" * 20) + "#[fg=blue]tail"
    unrelated = "UNRELATED #(parley indicator user-owned) #[fg=green]keep"
    tmux("set-option", "-t", "opted-in", "status-right", custom)
    tmux("set-option", "-t", "opted-in", "status-right-length", "37")
    tmux("set-option", "-t", "unrelated", "status-right", unrelated)
    tmux("set-option", "-t", "unrelated", "status-right-length", "43")

    unrelated_before = (
        _tmux_option(tmux, "unrelated", "status-right"),
        _tmux_option(tmux, "unrelated", "status-right-length"),
    )
    assert indicator.ensure() == 1
    installed = _tmux_option(tmux, "opted-in", "status-right")
    assert installed == f"{custom} {indicator._badge()}"
    assert _tmux_option(tmux, "opted-in", "status-right-length") == "220"
    assert _tmux_option(tmux, "opted-in", indicator.STATE_OPTION, local=True)
    assert (
        _tmux_option(tmux, "unrelated", "status-right"),
        _tmux_option(tmux, "unrelated", "status-right-length"),
    ) == unrelated_before

    assert indicator.ensure() == 0
    assert _tmux_option(tmux, "opted-in", "status-right") == installed

    assert indicator.cleanup() == 1
    assert _tmux_option(tmux, "opted-in", "status-right") == custom
    assert _tmux_option(tmux, "opted-in", "status-right-length") == "37"
    assert not _tmux_option(
        tmux, "opted-in", indicator.STATE_OPTION, local=True)

    # User edits made during the lifecycle win; cleanup removes only our badge.
    assert indicator.ensure() == 1
    late = " #[fg=yellow]USER-LATE"
    active = _tmux_option(tmux, "opted-in", "status-right")
    tmux("set-option", "-t", "opted-in", "status-right", active + late)
    tmux("set-option", "-t", "opted-in", "status-right-length", "73")
    assert indicator.cleanup() == 1
    assert _tmux_option(tmux, "opted-in", "status-right") == custom + late
    assert _tmux_option(tmux, "opted-in", "status-right-length") == "73"

    # A stale opted-in pane disappears without causing unrelated mutations.
    assert tmux("kill-session", "-t", "opted-in").returncode == 0
    assert indicator.ensure() == 0
    assert (
        _tmux_option(tmux, "unrelated", "status-right"),
        _tmux_option(tmux, "unrelated", "status-right-length"),
    ) == unrelated_before


def test_cleanup_restores_inherited_options_without_freezing_them(isolated_tmux):
    tmux = isolated_tmux
    assert tmux("new-session", "-d", "-s", "inherited").returncode == 0
    tmux("set-option", "-g", "status-right", "GLOBAL-BEFORE")
    tmux("set-option", "-g", "status-right-length", "39")
    pane = tmux(
        "display-message", "-p", "-t", "inherited", "#{pane_id}",
    ).stdout.strip()
    hooks.turn_on([hooks.pane_key(pane)])

    assert not _tmux_option(tmux, "inherited", "status-right", local=True)
    assert not _tmux_option(
        tmux, "inherited", "status-right-length", local=True)
    assert indicator.ensure() == 1

    tmux("set-option", "-g", "status-right", "GLOBAL-AFTER")
    tmux("set-option", "-g", "status-right-length", "55")
    assert indicator.cleanup() == 1
    assert not _tmux_option(tmux, "inherited", "status-right", local=True)
    assert not _tmux_option(
        tmux, "inherited", "status-right-length", local=True)
    assert _tmux_option(tmux, "inherited", "status-right") == "GLOBAL-AFTER"
    assert _tmux_option(tmux, "inherited", "status-right-length") == "55"


def test_reconcile_removes_legacy_fragment_without_guessing_length(isolated_tmux):
    tmux = isolated_tmux
    assert tmux("new-session", "-d", "-s", "legacy").returncode == 0
    custom = "LEGACY-CUSTOM #[fg=cyan]preserve"
    legacy_badge = (
        f"{indicator.BADGE_STYLE}#(parley indicator)#[default]")
    tmux(
        "set-option", "-t", "legacy", "status-right",
        f"{custom} {legacy_badge}",
    )
    tmux("set-option", "-t", "legacy", "status-right-length", "61")

    assert indicator.ensure() == 1

    assert _tmux_option(tmux, "legacy", "status-right") == custom
    assert _tmux_option(tmux, "legacy", "status-right-length") == "61"
    assert not _tmux_option(tmux, "legacy", indicator.STATE_OPTION, local=True)
