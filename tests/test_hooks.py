import hashlib
import json
import stat
from types import SimpleNamespace

import pytest

from parley import config, hooks


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(config, "SPOKEN", tmp_path / "spoken")
    monkeypatch.setattr(config, "CANCELLATIONS", tmp_path / "cancellations")
    monkeypatch.setattr(config, "DEFAULT", tmp_path / "default")
    monkeypatch.setattr(hooks, "TARGETS", {"claude-code": tmp_path / "settings.json",
                                           "codex": tmp_path / "hooks.json"})
    monkeypatch.setattr(hooks, "SKILL_DIRS", {
        "claude-code": tmp_path / "claude-skills",
        "codex": tmp_path / "codex-skills",
    })
    for var in hooks.SESSION_VARS + ("TMUX_PANE",):
        monkeypatch.delenv(var, raising=False)


def test_pane_is_the_preferred_identity(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    assert hooks.session_keys()[0] == "pane-_42"


def test_pane_badge_uses_the_same_marker_as_parley_on():
    hooks.turn_on([hooks.pane_key("%42")])

    assert hooks.pane_is_on("%42")
    assert not hooks.pane_is_on("%43")


def test_falls_back_to_a_harness_session_id(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-9")
    assert hooks.session_keys() == ["id-thread-9"]


def test_payload_identity_is_accepted_when_the_environment_is_bare():
    assert hooks.session_keys({"session_id": "s1"}) == ["id-s1"]


def test_agent_deck_session_name_becomes_a_spoken_label(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr(hooks.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(stdout="agentdeck_windy-falcon_2ae0c860\n"))
    assert hooks.session_label() == "windy-falcon"
    assert hooks.label_reply("Tests are green.") == (
        "Session windy-falcon. Tests are green.")


def test_explicit_session_name_wins(monkeypatch):
    monkeypatch.setenv("PARLEY_SESSION_NAME", "release captain")
    assert hooks.session_label({"title": "ignored"}) == "release captain"


def test_plain_tmux_session_name_is_preserved(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr(hooks.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(stdout="my_project\n"))
    assert hooks.session_label() == "my project"


def test_matching_any_identity_counts(monkeypatch):
    """Arming from a shell and firing from a hook must agree even if only one
    of them can see the pane."""
    monkeypatch.setenv("TMUX_PANE", "%42")
    hooks.turn_on(hooks.session_keys({"session_id": "s1"}))
    monkeypatch.delenv("TMUX_PANE")
    assert hooks.is_on(hooks.session_keys({"session_id": "s1"}))


def test_target_aliases_share_one_bounded_opaque_owner(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%42-secret-name")
    monkeypatch.setenv("CODEX_THREAD_ID", "private-thread-title")
    keys = hooks.session_keys()

    hooks.turn_on(keys)

    owners = [(config.SESSIONS / key).read_text() for key in keys]
    assert len(set(owners)) == 1
    assert hooks.OWNER_PATTERN.fullmatch(owners[0])
    assert "%42" not in owners[0]
    assert "private-thread-title" not in owners[0]
    assert stat.S_IMODE(config.SESSIONS.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE((config.SESSIONS / key).stat().st_mode) == 0o600
        for key in keys
    )

    hooks.turn_off([keys[0]])

    assert not any((config.SESSIONS / key).exists() for key in keys)


def test_fresh_targets_get_unrelated_random_owners_and_rearming_rotates(
        monkeypatch):
    values = iter(["1" * 32, "2" * 32, "3" * 32])
    monkeypatch.setattr(hooks.secrets, "token_hex", lambda size: next(values))
    first_keys = ["pane-first", "id-first"]
    second_keys = ["pane-second", "id-second"]

    hooks.turn_on(first_keys)
    first_owner = hooks.session_owner(first_keys)
    hooks.turn_on(second_keys)
    second_owner = hooks.session_owner(second_keys)

    assert first_owner == "owner-" + "1" * 32
    assert second_owner == "owner-" + "2" * 32
    assert first_owner != second_owner
    assert first_owner != (
        "owner-" + hashlib.sha256(first_keys[0].encode()).hexdigest()[:32]
    )
    assert second_owner != (
        "owner-" + hashlib.sha256(second_keys[0].encode()).hexdigest()[:32]
    )

    hooks.turn_off([first_keys[0]])
    hooks.turn_on(first_keys)

    assert hooks.session_owner(first_keys) == "owner-" + "3" * 32
    assert hooks.session_owner(first_keys) != first_owner


def test_empty_legacy_marker_hashes_only_until_next_turn_on(monkeypatch):
    config.SESSIONS.mkdir(parents=True)
    (config.SESSIONS / "pane-_42").touch()

    first = hooks.session_owner(["pane-_42"])
    second = hooks.session_owner(["pane-_42"])

    assert first == second
    assert hooks.OWNER_PATTERN.fullmatch(first)
    assert first == (
        "owner-" + hashlib.sha256(b"pane-_42").hexdigest()[:32]
    )

    monkeypatch.setattr(hooks.secrets, "token_hex", lambda size: "f" * 32)
    hooks.turn_on(["pane-_42"])

    assert hooks.session_owner(["pane-_42"]) == "owner-" + "f" * 32


def test_turning_off_unarmed_target_preserves_other_empty_legacy_marker():
    config.SESSIONS.mkdir(parents=True)
    legacy = config.SESSIONS / "pane-legacy"
    legacy.touch()

    hooks.turn_off(["pane-never-armed"])

    assert legacy.exists()


def test_corrupt_nonempty_marker_is_not_turned_into_correlatable_owner(
        monkeypatch):
    config.SESSIONS.mkdir(parents=True)
    marker = config.SESSIONS / "pane-corrupt"
    marker.write_text("pane name accidentally persisted")

    assert hooks.session_owner(["pane-corrupt"]) == ""

    monkeypatch.setattr(hooks.secrets, "token_hex", lambda size: "e" * 32)
    hooks.turn_on(["pane-corrupt"])

    assert marker.read_text() == "owner-" + "e" * 32


def test_reply_read_from_a_direct_message():
    """Codex hands over the text; there is no transcript to parse."""
    reply_id, text = hooks.reply_from({"last-assistant-message": "all done"})
    assert text == "all done"
    assert reply_id.startswith("direct-")


def test_direct_reply_ids_are_stable_and_content_addressed():
    first, _ = hooks.reply_from({"last-assistant-message": "same"})
    second, _ = hooks.reply_from({"last-assistant-message": "same"})
    third, _ = hooks.reply_from({"last-assistant-message": "different"})
    assert first == second != third


def test_reply_read_from_a_transcript(tmp_path):
    """Claude Code hands over a path."""
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "uuid": "a1",
         "message": {"content": [{"type": "text", "text": "done"}]}},
    ]))
    assert hooks.reply_from({"transcript_path": str(path)}) == ("a1", "done")


def test_unknown_payload_yields_nothing():
    assert hooks.reply_from({"nothing": "useful"}) == ("", "")


def test_install_is_idempotent(monkeypatch):
    path = hooks.TARGETS["claude-code"]
    path.write_text(json.dumps({"hooks": {}}))
    assert hooks.install("claude-code") == ["claude-code"]
    assert hooks.install("claude-code") == []


def test_install_uses_parley_skill_name():
    hooks.install_skill("codex")
    skill = hooks.SKILL_DIRS["codex"] / "parley" / "SKILL.md"
    assert skill.exists()
    assert "name: parley" in skill.read_text()
    assert not (hooks.SKILL_DIRS["codex"] / "voice").exists()


def test_install_leaves_existing_hooks_alone():
    path = hooks.TARGETS["claude-code"]
    path.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "some-other-tool"}]}]}}))
    hooks.install("claude-code")
    commands = [h["command"] for h in
                json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"]]
    assert "some-other-tool" in commands and hooks.COMMAND in commands


def test_uninstall_removes_only_our_hook():
    path = hooks.TARGETS["claude-code"]
    path.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "some-other-tool"}]}]}}))
    hooks.install("claude-code")
    hooks.uninstall("claude-code")
    commands = [h["command"] for h in
                json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"]]
    assert commands == ["some-other-tool"]


def test_install_backs_up_before_writing():
    path = hooks.TARGETS["codex"]
    path.write_text(json.dumps({"hooks": {}}))
    hooks.install("codex")
    assert path.with_suffix(path.suffix + ".parley-backup").exists()


def test_hook_stays_silent_when_the_session_is_off(monkeypatch):
    spoke = []
    monkeypatch.setattr(hooks, "detach", lambda fn: spoke.append(fn))
    hooks.handle(argv=[json.dumps(
        {"session_id": "s1", "last-assistant-message": "hello"})])
    assert spoke == []


def test_hook_speaks_when_the_session_is_on(monkeypatch):
    spoke = []
    monkeypatch.setattr(hooks, "detach", lambda fn: spoke.append(fn))
    hooks.turn_on(["id-s1"])
    hooks.handle(argv=[json.dumps(
        {"session_id": "s1", "last-assistant-message": "hello"})])
    assert len(spoke) == 1


def test_automatic_reply_is_enqueued_with_its_session_name(monkeypatch):
    queued = []
    monkeypatch.setattr(hooks, "reply_from", lambda payload: ("reply-1", "hello"))
    monkeypatch.setattr(hooks, "session_label", lambda payload: "windy-falcon")
    monkeypatch.setattr(
        hooks,
        "enqueue",
        lambda text, **metadata: queued.append((text, metadata)),
    )
    monkeypatch.setattr(hooks, "drain", lambda: None)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)

    owner = "owner-" + "a" * 32
    hooks.speak_reply("pane-42", {}, owner, "123")

    assert queued == [(
        "Session windy-falcon. hello",
        {"owner": owner, "revision": "123"},
    )]


def test_malformed_payload_is_survivable(monkeypatch):
    monkeypatch.setattr(hooks, "detach", lambda fn: pytest.fail("should not speak"))
    hooks.handle(argv=["not json at all"])
    hooks.handle(argv=[json.dumps(["a", "list"])])
