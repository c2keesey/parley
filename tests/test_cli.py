"""Command-line behavior tests."""

import io

from parley import cli, listen, triggers


class _UnreadableInput:
    def __init__(self, *, tty):
        self.tty = tty

    def isatty(self):
        return self.tty

    def read(self, *args, **kwargs):
        raise AssertionError("bare parley must not read stdin")


def test_bare_invocation_prints_public_help_without_reading_tty(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cli.sys, "stdin", _UnreadableInput(tty=True))

    cli.main([])

    output = capsys.readouterr().out
    assert "usage: parley" in output
    assert "parley status" in output
    assert "parley on" in output
    for command in (
        "on", "off", "toggle", "status", "default", "say", "voices",
        "listen", "enroll", "stop", "cues", "install", "uninstall",
    ):
        assert f"\n    {command} " in output
    assert "\n    hook " not in output
    assert "\n    indicator " not in output
    assert "==SUPPRESS==" not in output


def test_bare_invocation_does_not_consume_piped_input(monkeypatch, capsys):
    piped = io.StringIO("arbitrary non-hook input\n")
    monkeypatch.setattr(cli.sys, "stdin", piped)

    cli.main([])

    assert piped.tell() == 0
    assert "usage: parley" in capsys.readouterr().out


def test_explicit_hook_still_reads_piped_json(monkeypatch):
    payload = '{"session_id": "compatibility-test"}\n'
    piped = io.StringIO(payload)
    monkeypatch.setattr(cli.sys, "stdin", piped)
    monkeypatch.setattr(cli.hooks, "is_on", lambda keys: False)

    cli.main(["hook"])

    assert piped.tell() == len(payload)


def test_bare_json_argument_still_dispatches_without_reading_stdin(
    monkeypatch,
):
    payload = '{"session_id": "compatibility-test"}'
    handled = []
    original_handle = cli.hooks.handle

    def record_handle(*, argv):
        handled.append(argv)
        original_handle(argv=argv)

    monkeypatch.setattr(cli.sys, "stdin", _UnreadableInput(tty=False))
    monkeypatch.setattr(cli.hooks, "is_on", lambda keys: False)
    monkeypatch.setattr(cli.hooks, "handle", record_handle)

    cli.main([payload])

    assert handled == [[payload]]


def test_hidden_internal_commands_remain_parseable():
    parser = cli.build_parser()

    assert parser.parse_args(["hook"]).command == "hook"
    assert parser.parse_args(["indicator", "%42"]).command == "indicator"


def test_listen_status_names_target_session_and_pane(monkeypatch, capsys):
    monkeypatch.setattr(listen, "is_running", lambda: 123)
    monkeypatch.setattr(listen, "get_target", lambda: "%531")
    monkeypatch.setattr(listen, "listener_state", lambda: "ready")
    monkeypatch.setattr(listen.triggers, "enrolled", lambda: True)
    monkeypatch.setattr(
        listen.indicator, "session_label", lambda pane: "ivory-lynx")

    cli.main(["listen", "status"])

    assert "sends to ivory-lynx (pane %531)" in capsys.readouterr().out


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
