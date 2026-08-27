from types import SimpleNamespace

from parley import indicator, listen


def result(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode)


def test_indicator_is_blank_when_listener_is_not_alive(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 0)
    monkeypatch.setattr(
        indicator, "_session_label",
        lambda pane: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    assert indicator.text() == ""


def test_indicator_names_the_dictation_target(monkeypatch):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%42")
    monkeypatch.setattr(indicator, "_session_label", lambda pane: "windy-falcon")
    assert indicator.text() == " 🎙 PARLEY LISTENING → windy-falcon "


def test_agent_deck_tmux_identity_becomes_a_readable_label(monkeypatch):
    monkeypatch.setattr(
        indicator, "_run",
        lambda argv, timeout=2: result("agentdeck_c2k-8_695c9762\n"),
    )
    assert indicator._session_label("%42") == "c2k-8"


def test_ensure_appends_badge_to_every_session_and_is_idempotent(monkeypatch):
    calls = []
    bars = {
        "one": "existing one",
        "two": f"existing two {indicator.BADGE}",
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
    assert indicator.ensure() == 1
    assert bars["one"] == f"existing one {indicator.BADGE}"
    assert bars["two"] == f"existing two {indicator.BADGE}"
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
