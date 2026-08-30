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


def test_preflight_covers_both_harnesses_without_mutation(capsys):
    plans = hooks.preflight()

    assert [plan.name for plan in plans] == ["claude-code", "codex"]
    output = capsys.readouterr().out
    for name, settings in hooks.TARGETS.items():
        skill = hooks.SKILL_DIRS[name] / "parley" / "SKILL.md"
        assert str(settings) in output
        assert str(settings.with_suffix(settings.suffix + ".parley-backup")) in output
        assert str(skill) in output
        assert str(skill.with_suffix(skill.suffix + ".parley-backup")) in output
        assert not settings.exists()
        assert not skill.exists()
    assert "Prerequisites for hands-free input" in output


def test_install_dry_run_reports_both_harnesses_without_mutation(capsys):
    assert hooks.install(dry_run=True) == ["claude-code", "codex"]

    assert "dry-run; no changes" in capsys.readouterr().out
    assert all(not settings.exists() for settings in hooks.TARGETS.values())
    assert all(
        not (root / "parley" / "SKILL.md").exists()
        for root in hooks.SKILL_DIRS.values()
    )


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_new_install_files_have_deliberate_modes(harness):
    hooks.install(harness)

    settings = hooks.TARGETS[harness]
    skill = hooks.SKILL_DIRS[harness] / "parley" / "SKILL.md"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert stat.S_IMODE(skill.stat().st_mode) == 0o644


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_install_preserves_unrelated_json_backups_and_modes(harness):
    path = hooks.TARGETS[harness]
    original = {
        "theme": "midnight",
        "hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "some-other-tool", "timeout": 3}
        ]}]},
    }
    original_bytes = json.dumps(original).encode()
    path.write_bytes(original_bytes)
    path.chmod(0o640)
    skill = hooks.SKILL_DIRS[harness] / "parley" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("old Parley skill\n")
    skill.chmod(0o604)

    assert hooks.install(harness) == [harness]

    installed = json.loads(path.read_text())
    assert installed["theme"] == "midnight"
    assert installed["hooks"]["Stop"][0]["matcher"] == ""
    assert any(
        entry.get("command") == "some-other-tool"
        for entry in installed["hooks"]["Stop"][0]["hooks"]
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    backup = path.with_suffix(path.suffix + ".parley-backup")
    assert backup.read_bytes() == original_bytes
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    assert stat.S_IMODE(skill.stat().st_mode) == 0o604
    skill_backup = skill.with_suffix(skill.suffix + ".parley-backup")
    assert skill_backup.read_text() == "old Parley skill\n"
    assert stat.S_IMODE(skill_backup.stat().st_mode) == 0o604


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_install_update_uninstall_preserves_original_recovery_backup(harness):
    path = hooks.TARGETS[harness]
    original = b'{"unrelated":{"keep":"original"}}\n'
    path.write_bytes(original)
    path.chmod(0o640)

    hooks.install(harness)
    backup = path.with_suffix(path.suffix + ".parley-backup")
    assert backup.read_bytes() == original

    installed = json.loads(path.read_text())
    installed["hooks"]["Stop"][0]["hooks"][-1]["timeout"] = 1
    path.write_text(json.dumps(installed))
    hooks.install(harness, operation="update")
    assert backup.read_bytes() == original

    hooks.uninstall(harness)

    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    assert json.loads(path.read_text()) == {"unrelated": {"keep": "original"}}


def test_invalid_json_in_one_harness_prevents_all_harness_mutation():
    claude = hooks.TARGETS["claude-code"]
    codex = hooks.TARGETS["codex"]
    original = b'{"theme": "keep-me"}'
    claude.write_bytes(original)
    codex.write_text('{"hooks":')

    with pytest.raises(SystemExit, match="no changes made"):
        hooks.install()

    assert claude.read_bytes() == original
    assert not claude.with_suffix(".json.parley-backup").exists()
    assert all(
        not (root / "parley" / "SKILL.md").exists()
        for root in hooks.SKILL_DIRS.values()
    )


@pytest.mark.parametrize("malformed", [
    [],
    {"hooks": None},
    {"hooks": []},
    {"hooks": {"Stop": None}},
    {"hooks": {"Stop": {}}},
    {"hooks": {"Stop": [{"hooks": None}]}},
    {"hooks": {"Stop": [{"hooks": {}}]}},
    {"hooks": {"Stop": [{"hooks": [{"command": 42}]}]}},
])
def test_structurally_malformed_settings_fail_without_mutation(malformed):
    path = hooks.TARGETS["codex"]
    original = json.dumps(malformed).encode()
    path.write_bytes(original)

    with pytest.raises(SystemExit, match="no changes made"):
        hooks.install("codex")

    assert path.read_bytes() == original
    assert not path.with_suffix(".json.parley-backup").exists()
    assert not (hooks.SKILL_DIRS["codex"] / "parley" / "SKILL.md").exists()


def test_partial_install_with_hook_only_repairs_skill_without_rewriting_json():
    path = hooks.TARGETS["claude-code"]
    installed_hook = {
        "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": hooks.COMMAND,
             "timeout": hooks.TIMEOUT}
        ]}]}
    }
    original = json.dumps(installed_hook).encode()
    path.write_bytes(original)

    hooks.install("claude-code")

    assert path.read_bytes() == original
    assert not path.with_suffix(".json.parley-backup").exists()
    assert (hooks.SKILL_DIRS["claude-code"] / "parley" / "SKILL.md").exists()


def test_partial_install_with_skill_only_repairs_hook_without_rewriting_skill():
    path = hooks.TARGETS["codex"]
    path.write_text(json.dumps({"unrelated": True}))
    skill = hooks.SKILL_DIRS["codex"] / "parley" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(hooks._skill_source().read_bytes())
    skill.chmod(0o600)

    hooks.install("codex")

    assert json.loads(path.read_text())["unrelated"] is True
    assert stat.S_IMODE(skill.stat().st_mode) == 0o600
    assert not skill.with_suffix(".md.parley-backup").exists()


def test_update_repairs_duplicate_and_stale_parley_hooks():
    path = hooks.TARGETS["claude-code"]
    stale = {"type": "command", "command": hooks.COMMAND, "timeout": 1}
    path.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [stale]}, {"hooks": [stale.copy()]},
    ]}}))

    hooks.install("claude-code", operation="update")

    parley_hooks = [
        entry
        for matcher in json.loads(path.read_text())["hooks"]["Stop"]
        for entry in matcher.get("hooks", [])
        if entry.get("command") == hooks.COMMAND
    ]
    assert parley_hooks == [{
        "type": "command", "command": hooks.COMMAND, "timeout": hooks.TIMEOUT
    }]


def test_uninstall_removes_exact_owned_entries_and_leaves_runtime_data():
    path = hooks.TARGETS["codex"]
    path.write_text(json.dumps({
        "unrelated": {"keep": True},
        "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": hooks.COMMAND,
             "timeout": hooks.TIMEOUT},
            {"type": "command", "command": "wrapper parley hook"},
            {"type": "command", "command": "some-other-tool"},
        ]}]},
    }))
    path.chmod(0o640)
    skill = hooks.SKILL_DIRS["codex"] / "parley" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(hooks._skill_source().read_bytes())
    runtime = config.STATE / "runtime-sentinel"
    runtime.write_text("preserve me")

    assert hooks.uninstall("codex") == ["codex"]

    remaining = json.loads(path.read_text())
    commands = remaining["hooks"]["Stop"][0]["hooks"]
    assert [entry["command"] for entry in commands] == [
        "wrapper parley hook", "some-other-tool"
    ]
    assert remaining["unrelated"] == {"keep": True}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not skill.exists()
    assert runtime.read_text() == "preserve me"


def test_uninstall_retains_a_modified_skill(capsys):
    skill = hooks.SKILL_DIRS["claude-code"] / "parley" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("user-modified skill")

    assert hooks.uninstall("claude-code") == []

    assert skill.read_text() == "user-modified skill"
    assert "retain modified file" in capsys.readouterr().out


def test_uninstall_dry_run_does_not_remove_anything():
    hooks.install("claude-code")
    path = hooks.TARGETS["claude-code"]
    skill = hooks.SKILL_DIRS["claude-code"] / "parley" / "SKILL.md"
    original = path.read_bytes()

    assert hooks.uninstall("claude-code", dry_run=True) == []

    assert path.read_bytes() == original
    assert skill.exists()


def test_microphone_permission_guidance_is_macos_contextual(monkeypatch, capsys):
    monkeypatch.setattr(hooks.platform, "system", lambda: "Darwin")

    hooks.preflight("claude-code")

    assert "System Settings > Privacy & Security > Microphone" in (
        capsys.readouterr().out
    )


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
    monkeypatch.setattr(hooks, "enqueue", queued.append)
    monkeypatch.setattr(hooks, "drain", lambda: None)
    monkeypatch.setattr(hooks.time, "sleep", lambda seconds: None)

    hooks.speak_reply("pane-42", {})

    assert queued == ["Session windy-falcon. hello"]


def test_malformed_payload_is_survivable(monkeypatch):
    monkeypatch.setattr(hooks, "detach", lambda fn: pytest.fail("should not speak"))
    hooks.handle(argv=["not json at all"])
    hooks.handle(argv=[json.dumps(["a", "list"])])
