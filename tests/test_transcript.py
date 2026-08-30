import json

from parley.transcript import final_reply


def write(tmp_path, entries):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries))
    return str(path)


def user(text):
    return {"type": "user", "message": {"content": text}}


def tool_result(uid="t1"):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": uid, "content": "ok"}]}}


def assistant(uuid, text):
    return {"type": "assistant", "uuid": uuid,
            "message": {"content": [{"type": "text", "text": text}]}}


def test_picks_the_reply_to_the_current_turn(tmp_path):
    path = write(tmp_path, [
        user("first question"),
        assistant("a1", "first answer"),
        user("second question"),
        assistant("a2", "second answer"),
    ])
    assert final_reply(path) == ("a2", "second answer")


def test_tool_results_are_not_turn_boundaries(tmp_path):
    """Claude Code records tool results as user entries."""
    path = write(tmp_path, [
        user("do the thing"),
        assistant("a1", "working on it"),
        tool_result(),
        assistant("a2", "all done"),
    ])
    assert final_reply(path) == ("a2", "all done")


def test_empty_when_the_turn_has_no_reply_yet(tmp_path):
    """The hook must wait rather than read out the previous turn."""
    path = write(tmp_path, [
        user("first question"),
        assistant("a1", "first answer"),
        user("second question"),
    ])
    assert final_reply(path) == ("", "")


def test_ignores_tool_only_entries_and_subagents(tmp_path):
    path = write(tmp_path, [
        user("go"),
        {"type": "assistant", "uuid": "sub", "isSidechain": True,
         "message": {"content": [{"type": "text", "text": "subagent chatter"}]}},
        {"type": "assistant", "uuid": "tool", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}},
        assistant("a1", "the answer"),
    ])
    assert final_reply(path) == ("a1", "the answer")


def test_missing_file_is_not_an_error(tmp_path):
    assert final_reply(str(tmp_path / "nope.jsonl")) == ("", "")


def test_reply_text_is_never_used_as_a_fallback_identifier(tmp_path):
    path = write(tmp_path, [
        user("go"),
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "private reply"}]}},
    ])
    assert final_reply(path) == ("", "")
