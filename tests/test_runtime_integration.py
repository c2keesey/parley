import json
from pathlib import Path

from parley import config, indicator, player, processes, runtime


def identity(pid):
    return f"linux:00000000-0000-0000-0000-000000000000:{pid}"


def configure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "macos")
    monkeypatch.setattr(config, "STATE", tmp_path)
    monkeypatch.setattr(config, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(config, "LOCK", tmp_path / "player.lock")
    monkeypatch.setattr(config, "PIDFILE", tmp_path / "playing.pid")
    monkeypatch.setattr(config, "CUE_PROCESSES", tmp_path / "cue-processes")
    monkeypatch.setattr(config, "SPEECH_PID", tmp_path / "speech.json")
    monkeypatch.setattr(config, "DRAIN_PID", tmp_path / "drainer.json")
    monkeypatch.setattr(config, "MIC_TURN", tmp_path / "microphone-turn.json")
    monkeypatch.setattr(config, "PAUSE", tmp_path / "pause")
    monkeypatch.setattr(config, "LOG", tmp_path / "speak.log")
    monkeypatch.setattr(config, "INTERRUPT", tmp_path / "interrupt")
    monkeypatch.setattr(config, "SKIP", tmp_path / "skip")
    monkeypatch.setattr(processes, "process_identity", identity)
    monkeypatch.setattr(indicator, "refresh", lambda: None)


def test_visible_failure_uses_only_runtime_status_store(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    private = "sk-private /Users/person/project raw-provider-body"
    monkeypatch.setattr(
        player,
        "synthesize",
        lambda *args: (_ for _ in ()).throw(RuntimeError(private)),
    )
    monkeypatch.setattr("parley.cues.play", lambda *args, **kwargs: None)
    player.enqueue("private spoken response")

    assert player.drain() is False

    snapshot = runtime.snapshot()
    serialized = json.dumps(snapshot)
    assert snapshot["schema"] == "parley.runtime-status"
    assert snapshot["version"] == 1
    assert snapshot["speech"]["synthesis"] == "degraded"
    assert snapshot["errors"][-1] == {
        "code": "synthesis_failed",
        "component": "speech",
        "stage": "synthesize",
        "at": snapshot["errors"][-1]["at"],
        "provider": "macos",
    }
    assert private not in serialized
    assert not (tmp_path / "speech-error.json").exists()
    assert not (tmp_path / "listener.state").exists()


def test_status_failure_does_not_kill_speech_or_expose_runtime_inputs(
        tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    voice = "private-voice-canary"
    model = "private-model-canary"
    spoken = []
    monkeypatch.setattr(
        runtime,
        "_write_unlocked",
        lambda snapshot: (_ for _ in ()).throw(OSError("private disk path")),
    )
    monkeypatch.setattr(
        player, "synthesize", lambda text, *args: text.encode())
    monkeypatch.setattr(
        player, "play", lambda audio, *args, **kwargs: spoken.append(audio.decode()))
    monkeypatch.setattr(player, "_wake_output", lambda: None)
    monkeypatch.setattr("parley.cues.play", lambda *args, **kwargs: None)

    player.enqueue("still speak", voice=voice, model=model)
    assert player.drain() is True

    assert spoken == ["still speak"]
    log = config.LOG.read_text()
    assert voice not in log
    assert model not in log
    assert "private disk path" not in log


def test_only_process_module_signals_stored_pids():
    source = Path(__file__).parents[1] / "src" / "parley"
    offenders = []
    for module in source.glob("*.py"):
        if module.name == "processes.py":
            continue
        if "os.kill(" in module.read_text():
            offenders.append(module.name)
    assert offenders == []
