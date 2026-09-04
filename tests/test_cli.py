"""Command-line behavior tests."""

from types import SimpleNamespace

from parley import cli, listen, triggers


def _stub_live_mode(monkeypatch, pane="%392", target=""):
    monkeypatch.setenv("TMUX_PANE", pane)
    monkeypatch.setattr(listen, "whisper_bin", lambda: "/opt/whisper-cli")
    monkeypatch.setattr(listen, "ensure_model", lambda: True)
    monkeypatch.setattr(listen, "get_target", lambda: target)
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "is_agent_deck_pane", lambda pane: True)
    monkeypatch.setattr(listen, "set_follow_active", lambda enabled: None)
    monkeypatch.setattr(listen, "stop", lambda: True)
    monkeypatch.setattr(listen.indicator, "refresh", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        cli,
        "subprocess",
        SimpleNamespace(Popen=lambda *args, **kwargs: None, DEVNULL=None),
    )


def test_on_starts_speech_and_hands_free_input_for_invoking_pane(
        monkeypatch, capsys):
    _stub_live_mode(monkeypatch)
    targets = []
    speech = []
    monkeypatch.setattr(listen, "set_target", targets.append)
    monkeypatch.setattr(cli.hooks, "turn_on", lambda keys: speech.append(keys))
    monkeypatch.setattr(cli, "_report", lambda keys: None)

    cli.main(["on"])

    assert targets == ["%392"]
    assert len(speech) == 1
    assert "listening: on" in capsys.readouterr().out


def test_on_does_not_leave_speech_only_when_listener_cannot_start(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    enabled = []
    disabled = []
    monkeypatch.setattr(cli.hooks, "turn_on", lambda keys: enabled.append(keys))
    monkeypatch.setattr(cli.hooks, "turn_off", lambda keys: disabled.append(keys))
    monkeypatch.setattr(listen.indicator, "refresh", lambda: None)

    try:
        cli.main(["on"])
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("parley on should fail outside tmux")

    assert enabled == []
    assert len(disabled) == 1


def test_on_does_not_enable_speech_when_listener_exits_during_startup(
        monkeypatch):
    _stub_live_mode(monkeypatch)
    monkeypatch.setattr(listen, "is_running", lambda: 0)
    enabled = []
    disabled = []
    monkeypatch.setattr(cli.hooks, "turn_on", lambda keys: enabled.append(keys))
    monkeypatch.setattr(cli.hooks, "turn_off", lambda keys: disabled.append(keys))

    try:
        cli.main(["on"])
    except SystemExit as error:
        assert str(error) == "hands-free listener exited during startup"
    else:
        raise AssertionError("parley on should fail with a dead listener")

    assert enabled == []
    assert len(disabled) == 1


def test_off_stops_listener_owned_by_invoking_pane(monkeypatch):
    _stub_live_mode(monkeypatch, target="%392")
    stopped = []
    monkeypatch.setattr(listen, "stop", lambda: stopped.append(True) or True)
    monkeypatch.setattr(cli.hooks, "turn_off", lambda keys: None)
    monkeypatch.setattr(cli, "stop", lambda: None)
    monkeypatch.setattr(cli, "_report", lambda keys: None)

    cli.main(["off"])

    assert stopped == [True]


def test_off_does_not_stop_listener_owned_by_another_pane(monkeypatch):
    _stub_live_mode(monkeypatch, target="%511")
    monkeypatch.setattr(
        listen,
        "stop",
        lambda: (_ for _ in ()).throw(AssertionError("must not stop listener")),
    )
    monkeypatch.setattr(cli.hooks, "turn_off", lambda keys: None)
    monkeypatch.setattr(cli, "stop", lambda: None)
    monkeypatch.setattr(cli, "_report", lambda keys: None)

    cli.main(["off"])


def test_output_on_enables_only_spoken_output(monkeypatch, capsys):
    speech = []
    monkeypatch.setattr(cli.hooks, "turn_on", lambda keys: speech.append(keys))
    monkeypatch.setattr(cli.hooks, "is_on", lambda keys: True)
    monkeypatch.setattr(listen.indicator, "refresh", lambda: None)
    monkeypatch.setattr(
        listen,
        "set_target",
        lambda pane: (_ for _ in ()).throw(AssertionError("must not listen")),
    )

    cli.main(["output", "on"])

    assert len(speech) == 1
    assert "output: on" in capsys.readouterr().out


def test_output_off_disables_output_without_touching_listener(
        monkeypatch):
    disabled = []
    stopped = []
    monkeypatch.setattr(
        cli.hooks, "turn_off", lambda keys: disabled.append(keys))
    monkeypatch.setattr(cli, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(listen, "stop", lambda: (_ for _ in ()).throw(
        AssertionError("output off must not stop listening")))
    monkeypatch.setattr(cli, "_report_output", lambda keys: None)
    monkeypatch.setattr(listen.indicator, "refresh", lambda: None)

    cli.main(["output", "off"])

    assert len(disabled) == 1
    assert stopped == [True]


def test_speak_remains_a_compatibility_alias(monkeypatch):
    speech = []
    monkeypatch.setattr(cli.hooks, "turn_on", lambda keys: speech.append(keys))
    monkeypatch.setattr(cli, "_report_output", lambda keys: None)
    monkeypatch.setattr(listen.indicator, "refresh", lambda: None)

    cli.main(["speak", "on"])

    assert len(speech) == 1


def test_say_carries_session_identity_into_output_queue(monkeypatch):
    queued = []
    monkeypatch.setattr(cli.hooks, "session_keys", lambda: ["pane-_42"])
    monkeypatch.setattr(cli.hooks, "session_label", lambda: "morning refresh")
    monkeypatch.setattr(
        cli,
        "enqueue",
        lambda text, **kwargs: queued.append((text, kwargs)),
    )
    monkeypatch.setattr(cli, "detach", lambda fn: None)

    cli.main(["say", "Still working."])

    assert queued == [(
        "Still working.",
        {
            "voice": None,
            "model": None,
            "session": "pane-_42",
            "session_label": "morning refresh",
        },
    )]


def test_listen_status_names_target_session_and_pane(monkeypatch, capsys):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%531")
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen.triggers, "enrolled", lambda: True)
    monkeypatch.setattr(
        listen.indicator, "session_label", lambda pane: "ivory-lynx")

    cli.main(["listen", "status"])

    assert "sends to ivory-lynx (pane %531)" in capsys.readouterr().out


def test_listen_on_starts_only_input(monkeypatch):
    started = []
    monkeypatch.setattr(
        cli, "_start_listener", lambda listener, device: started.append(device))
    monkeypatch.setattr(
        cli.hooks,
        "turn_on",
        lambda keys: (_ for _ in ()).throw(
            AssertionError("listen on must not enable output")),
    )

    cli.main(["listen", "on"])

    assert started == ["default"]


def test_listen_off_stops_only_input(monkeypatch):
    stopped = []
    monkeypatch.setattr(listen, "stop", lambda: stopped.append(True) or True)
    monkeypatch.setattr(
        cli.hooks,
        "turn_off",
        lambda keys: (_ for _ in ()).throw(
            AssertionError("listen off must not disable output")),
    )
    monkeypatch.setattr(
        cli,
        "stop",
        lambda: (_ for _ in ()).throw(
            AssertionError("listen off must not clear spoken output")),
    )

    cli.main(["listen", "off"])

    assert stopped == [True]


def test_listener_daemon_enables_device_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr(
        listen,
        "run",
        lambda device, recover_devices=False: calls.append(
            (device, recover_devices)),
    )

    cli.main(["listen", "run"])

    assert calls == [("default", True)]


def _stub_enrollment(monkeypatch):
    monkeypatch.setattr(triggers, "collect", lambda device: "samples")
    monkeypatch.setattr(
        triggers,
        "save",
        lambda samples: {"thresholds": {"wake": 0.42}},
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)


def test_enroll_retargets_running_listener_to_invoking_pane(monkeypatch):
    """A successful trigger must not submit into a stale prior session."""
    _stub_enrollment(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")

    stopped = []
    targets = []
    launches = []
    monkeypatch.setattr(listen, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(listen, "set_target", targets.append)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )

    cli.main(["enroll"])

    assert stopped == [True]
    assert targets == ["%392"]
    assert len(launches) == 1


def test_enroll_preserves_target_when_run_outside_tmux(monkeypatch):
    _stub_enrollment(monkeypatch)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")
    monkeypatch.setattr(listen, "stop", lambda: None)

    targets = []
    monkeypatch.setattr(listen, "set_target", targets.append)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: None)

    cli.main(["enroll"])

    assert targets == ["%511"]


def test_enroll_does_not_start_listener_that_was_off(monkeypatch):
    _stub_enrollment(monkeypatch)
    monkeypatch.setenv("TMUX_PANE", "%392")
    monkeypatch.setattr(listen, "is_running", lambda: None)
    monkeypatch.setattr(listen, "get_target", lambda: "%511")
    monkeypatch.setattr(
        listen,
        "set_target",
        lambda pane: (_ for _ in ()).throw(AssertionError("must not retarget")),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not start listener")
        ),
    )

    cli.main(["enroll"])
