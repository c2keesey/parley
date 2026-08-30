import json
import multiprocessing
import os
import stat
from types import SimpleNamespace

import pytest

from parley import config, hooks


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(config, "SPOKEN", tmp_path / "spoken")
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


def test_reply_read_from_a_direct_message():
    """Codex hands over the text; there is no transcript to parse."""
    reply_id, text = hooks.reply_from({
        "thread-id": "thread-1",
        "turn-id": "turn-1",
        "last-assistant-message": "all done",
    })
    assert text == "all done"
    assert reply_id == "turn:thread-1:turn-1"


def test_current_codex_stop_payload_uses_underscore_turn_id():
    assert hooks.reply_from({
        "session_id": "thread-1",
        "turn_id": "turn-1",
        "last_assistant_message": "all done",
    }) == ("turn:thread-1:turn-1", "all done")


def test_direct_reply_ids_use_supported_turn_identity_not_content():
    first, _ = hooks.reply_from({
        "thread-id": "thread-1", "turn-id": "turn-1",
        "last-assistant-message": "same",
    })
    duplicate, _ = hooks.reply_from({
        "thread-id": "thread-1", "turn-id": "turn-1",
        "last-assistant-message": "same",
    })
    distinct, _ = hooks.reply_from({
        "thread-id": "thread-1", "turn-id": "turn-2",
        "last-assistant-message": "same",
    })
    assert first == duplicate != distinct


def test_direct_reply_without_a_supported_turn_identity_yields_nothing():
    assert hooks.reply_from({"last-assistant-message": "same"}) == ("", "")


def test_reply_read_from_a_transcript(tmp_path):
    """Claude Code hands over a path."""
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "uuid": "a1",
         "message": {"content": [{"type": "text", "text": "done"}]}},
    ]))
    assert hooks.reply_from({"transcript_path": str(path)}) == ("a1", "done")


def test_claude_direct_text_uses_transcript_identity(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "uuid": "a1",
         "message": {"content": [{"type": "text", "text": "done"}]}},
    ]))
    assert hooks.reply_from({
        "last_assistant_message": "done",
        "transcript_path": str(path),
    }) == ("a1", "done")


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
        hooks, "enqueue", lambda text, receipt=None: queued.append(text))
    monkeypatch.setattr(hooks, "drain", lambda: None)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)

    hooks.speak_reply("pane-42", {})

    assert queued == ["Session windy-falcon. hello"]


def test_transcript_reply_still_settles_to_the_final_message(monkeypatch):
    queued = []
    replies = iter([
        ("draft-id", "draft reply"),
        ("final-id", "final reply"),
        ("final-id", "final reply"),
    ])
    monkeypatch.setattr(hooks, "reply_from", lambda payload: next(replies))
    monkeypatch.setattr(hooks, "session_label", lambda payload: "session")
    monkeypatch.setattr(
        hooks, "enqueue", lambda text, receipt=None: queued.append(text))
    monkeypatch.setattr(hooks, "drain", lambda: None)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)

    hooks.speak_reply("pane-42", {})

    assert queued == ["Session session. final reply"]


def test_distinct_turns_with_identical_text_are_each_enqueued(monkeypatch):
    queued = []
    monkeypatch.setattr(
        hooks, "enqueue", lambda text, receipt=None: queued.append(text))
    monkeypatch.setattr(hooks, "drain", lambda: None)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)
    common = {
        "thread-id": "thread-1",
        "last-assistant-message": "same reply",
    }

    hooks.speak_reply("pane-42", {**common, "turn-id": "turn-1"})
    hooks.speak_reply("pane-42", {**common, "turn-id": "turn-2"})

    assert len(queued) == 2


def test_duplicate_delivery_of_one_turn_is_enqueued_once(monkeypatch):
    queued = []
    monkeypatch.setattr(
        hooks, "enqueue", lambda text, receipt=None: queued.append(text))
    monkeypatch.setattr(hooks, "drain", lambda: None)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)
    payload = {
        "thread-id": "thread-1",
        "turn-id": "turn-1",
        "last-assistant-message": "one reply",
    }

    hooks.speak_reply("pane-42", payload)
    hooks.speak_reply("pane-42", payload)

    assert len(queued) == 1


def test_concurrent_hook_processes_atomically_enqueue_once(monkeypatch, tmp_path):
    context = multiprocessing.get_context("fork")
    both_resolved = context.Barrier(2)
    enqueues = tmp_path / "enqueues"

    def reply(payload):
        if not payload.get("resolved"):
            payload["resolved"] = True
            both_resolved.wait(timeout=5)
        return "turn:thread-1:turn-1", "race reply"

    def enqueue(text, receipt=None):
        descriptor = os.open(
            enqueues, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, b"queued\n")
        finally:
            os.close(descriptor)

    monkeypatch.setattr(hooks, "reply_from", reply)
    monkeypatch.setattr(hooks, "enqueue", enqueue)
    monkeypatch.setattr(hooks, "drain", lambda: None)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)
    workers = [context.Process(
        target=hooks.speak_reply, args=("pane-42", {})) for _ in range(2)]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=6)

    assert [worker.exitcode for worker in workers] == [0, 0]
    assert enqueues.read_text().splitlines() == ["queued"]


def test_dedup_history_is_bounded_and_private(monkeypatch):
    monkeypatch.setattr(hooks, "DEDUP_HISTORY", 2)
    monkeypatch.setattr(hooks, "enqueue", lambda text, receipt=None: True)
    monkeypatch.setattr(hooks, "drain", lambda: None)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)

    for index in range(5):
        hooks.speak_reply("pane-42", {
            "thread-id": "thread-1",
            "turn-id": f"turn-{index}",
            "last-assistant-message": "private reply",
        })

    session = config.SPOKEN / "pane-42.dedup"
    receipts = [entry for entry in session.iterdir() if entry.is_dir()]
    assert len(receipts) == 2
    assert stat.S_IMODE(session.stat().st_mode) == 0o700
    assert stat.S_IMODE((session / ".lock").stat().st_mode) == 0o600
    for receipt in receipts:
        assert stat.S_IMODE(receipt.stat().st_mode) == 0o700
        assert stat.S_IMODE((receipt / "committed").stat().st_mode) == 0o600
        assert "private reply" not in receipt.name


def test_malformed_payload_is_survivable(monkeypatch):
    monkeypatch.setattr(hooks, "detach", lambda fn: pytest.fail("should not speak"))
    hooks.handle(argv=["not json at all"])
    hooks.handle(argv=[json.dumps(["a", "list"])])
