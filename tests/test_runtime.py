import json
import os
import stat
import threading

import pytest

from parley import config, runtime


@pytest.fixture(autouse=True)
def isolated_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(runtime, "_target_available", lambda pane: pane == "%42")
    identity = "linux:00000000-0000-0000-0000-000000000000:"
    monkeypatch.setattr(
        runtime.processes, "process_identity", lambda pid: identity + str(pid))


def status_path():
    return config.STATE / "runtime-status.json"


def test_snapshot_is_versioned_atomic_and_private():
    snapshot = runtime.snapshot()

    assert snapshot["schema"] == "parley.runtime-status"
    assert snapshot["version"] == 1
    assert snapshot["generation"] == 1
    assert stat.S_IMODE(config.STATE.stat().st_mode) == 0o700
    assert stat.S_IMODE(status_path().stat().st_mode) == 0o600
    assert stat.S_IMODE((config.STATE / "runtime-status.lock").stat().st_mode) == 0o600


def test_concurrent_readers_never_observe_partial_json():
    writer = runtime.claim("listener")
    failures = []
    generations = []
    start = threading.Barrier(5)

    def write_states(offset):
        start.wait()
        states = ("ready", "capturing", "sending")
        for index in range(80):
            runtime.set_listener(writer, states[(index + offset) % len(states)])

    def read_file():
        start.wait()
        seen = []
        for _index in range(300):
            try:
                payload = json.loads(status_path().read_text())
                assert runtime._valid(payload)
                seen.append(payload["generation"])
            except Exception as exc:  # the assertion records the race outcome
                failures.append(exc)
        generations.extend(seen)

    threads = [
        threading.Thread(target=write_states, args=(offset,))
        for offset in range(4)
    ] + [threading.Thread(target=read_file)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert not failures
    assert generations
    assert generations == sorted(generations)


def test_replacement_writer_blocks_every_obsolete_component_mutation():
    old_listener = runtime.claim("listener")
    assert runtime.set_listener(old_listener, "ready")
    assert runtime.set_target(old_listener, "%41", available=True)
    new_listener = runtime.claim("listener")

    assert not runtime.set_listener(old_listener, "sending")
    assert not runtime.set_target(old_listener, "%99", available=True)
    assert not runtime.record_error(
        old_listener, "target_unavailable", "target", "submit")
    assert runtime.set_listener(new_listener, "capturing")
    assert runtime.set_target(new_listener, "%42", available=True)

    old_speech = runtime.claim("speech")
    assert runtime.set_provider(old_speech, "elevenlabs")
    new_speech = runtime.claim("speech")

    assert not runtime.set_speech(old_speech, synthesis="active")
    assert not runtime.set_provider(old_speech, "openai", "elevenlabs")
    assert not runtime.record_error(
        old_speech, "provider_fallback", "provider", "synthesize",
        "elevenlabs",
    )
    assert runtime.set_speech(new_speech, synthesis="active")
    assert runtime.set_provider(new_speech, "macos", "elevenlabs")

    snapshot = runtime.snapshot()
    assert snapshot["listener"]["state"] == "capturing"
    assert snapshot["target"] == {
        "kind": "tmux-pane", "id": "%42", "available": True,
    }
    assert snapshot["speech"]["synthesis"] == "active"
    assert snapshot["provider"]["active"] == "macos"
    assert snapshot["provider"]["fallback_from"] == "elevenlabs"
    assert snapshot["errors"] == []


def test_component_capabilities_cannot_cross_mutate_sections():
    listener = runtime.claim("listener")
    speech = runtime.claim("speech")

    assert not runtime.set_speech(listener, synthesis="active")
    assert not runtime.set_listener(speech, "capturing")
    assert not runtime.set_provider(listener, "macos")
    assert not runtime.set_target(speech, "%42", available=True)
    assert runtime.snapshot()["speech"] == {
        "synthesis": "idle", "playback": "idle",
    }


def test_manager_target_replacement_invalidates_old_listener():
    old_listener = runtime.claim("listener")
    assert runtime.set_listener(old_listener, "ready")

    runtime.manager_set_target("%42", available=True)

    assert not runtime.set_target(old_listener, "%99", available=True)
    assert not runtime.set_listener(old_listener, "sending")
    snapshot = runtime.snapshot()
    assert snapshot["listener"]["state"] == "off"
    assert snapshot["target"]["id"] == "%42"


def test_dead_owners_recover_to_degraded_without_pid_reuse(monkeypatch):
    listener = runtime.claim("listener")
    speech = runtime.claim("speech")
    runtime.set_listener(listener, "sending")
    runtime.set_speech(speech, synthesis="active", playback="idle")
    monkeypatch.setattr(runtime.processes, "process_identity", lambda _pid: None)

    snapshot = runtime.snapshot()

    assert snapshot["health"] == "degraded"
    assert snapshot["listener"]["state"] == "degraded"
    assert snapshot["speech"] == {
        "synthesis": "degraded", "playback": "degraded",
    }
    assert snapshot["writers"] == {"listener": None, "speech": None}
    assert {error["code"] for error in snapshot["errors"]} == {
        "listener_process_lost", "speech_process_lost",
    }


def test_listener_turn_and_playback_pause_are_one_coherent_generation():
    listener = runtime.claim("listener")
    speech = runtime.claim("speech")
    runtime.set_listener(listener, "ready")
    runtime.set_speech(speech, synthesis="idle", playback="active")

    runtime.set_listener(listener, "capturing")
    capturing = json.loads(status_path().read_text())
    assert capturing["listener"]["state"] == "capturing"
    assert capturing["speech"]["playback"] == "paused"

    runtime.set_listener(listener, "ready")
    ready = json.loads(status_path().read_text())
    assert ready["listener"]["state"] == "ready"
    assert ready["speech"]["playback"] == "active"


@pytest.mark.parametrize(
    "private_field",
    ["errors", "writers", "provider", "timestamp", "top-level"],
)
def test_corrupt_snapshot_cannot_become_a_private_output_channel(private_field):
    runtime.snapshot()
    payload = json.loads(status_path().read_text())
    secret = "PRIVATE dictated text sk-test /Users/person/secret"
    if private_field == "errors":
        payload["errors"] = [{
            "code": "synthesis_failed",
            "component": "speech",
            "stage": "synthesize",
            "at": 1,
            "detail": secret,
        }]
    elif private_field == "writers":
        payload["writers"]["listener"] = {
            "pid": 12,
            "birth": secret,
            "instance": "0" * 32,
            "heartbeat_at": 1,
        }
    elif private_field == "provider":
        payload["provider"]["active"] = secret
    elif private_field == "timestamp":
        payload["updated_at"] = secret
    else:
        payload["private"] = secret
    config.private_write(status_path(), json.dumps(payload))

    healed = runtime.snapshot()
    serialized = status_path().read_text()

    assert runtime._valid(healed)
    assert secret not in serialized
    assert healed["errors"][-1]["code"] == "snapshot_invalid"


def test_snapshot_never_copies_queue_content_paths_credentials_or_trigger_data():
    queue = config.STATE / "queue"
    config.private_directory(queue)
    private = (
        "dictated transcript reply sk-secret /Users/person/project "
        "trigger-feature-array"
    )
    config.private_write(queue / "0001.json", private)
    runtime.manager_set_target("private-project-name", available=True)

    snapshot = runtime.snapshot()
    serialized = status_path().read_text()

    assert snapshot["queue"]["depth"] == 1
    assert snapshot["target"]["id"] is None
    assert private not in serialized
    for forbidden in (
        "dictated", "transcript", "reply", "sk-secret", "/Users/",
        "trigger-feature",
    ):
        assert forbidden not in serialized


def test_error_api_rejects_arbitrary_content_and_bounds_recent_errors():
    writer = runtime.claim("speech")
    with pytest.raises(ValueError, match="unknown runtime error code"):
        runtime.record_error(
            writer, "private exception text", "speech", "synthesize")

    for index in range(20):
        code = "synthesis_failed" if index % 2 else "playback_failed"
        stage = "synthesize" if index % 2 else "play"
        runtime.record_error(writer, code, "speech", stage, "openai")

    assert len(runtime.snapshot()["errors"]) == runtime.MAX_ERRORS


def test_publication_fsyncs_the_parent_directory(monkeypatch):
    original_fsync = os.fsync
    synced_directory = []

    def fsync(descriptor):
        synced_directory.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        return original_fsync(descriptor)

    monkeypatch.setattr(runtime.os, "fsync", fsync)

    runtime.snapshot()

    assert synced_directory.count(False) >= 1
    assert synced_directory.count(True) >= 1


def test_status_io_and_identity_failures_do_not_take_down_core_work(monkeypatch):
    writer = runtime.claim("listener")
    monkeypatch.setattr(
        runtime, "_write_unlocked",
        lambda _snapshot: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    assert runtime.set_listener(writer, "capturing") is False
    assert runtime.set_target(writer, "%42", available=True) is False

    monkeypatch.setattr(
        runtime, "_locked_update",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unreadable")),
    )
    fallback = runtime.snapshot()
    assert fallback["health"] == "degraded"
    assert fallback["errors"][0]["code"] == "snapshot_invalid"


def test_claim_fails_closed_without_process_birth_identity(monkeypatch):
    monkeypatch.setattr(runtime.processes, "process_identity", lambda _pid: None)

    assert runtime.claim("listener") is None
    assert runtime.snapshot()["listener"]["state"] == "off"
