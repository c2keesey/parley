"""Command line interface."""
import argparse
import os
import subprocess
import sys
import time

from parley import __version__, config, hooks, relay
from parley.player import detach, drain, enqueue, stop


def _report(keys):
    on = hooks.is_on(keys)
    print(f"parley: {'on' if on else 'off'} for this session "
          f"(provider={config.provider()}, voice={config.active_voice()}, "
          f"model={config.active_model()})")
    if on:
        # Printed so that turning voice on mid-session lands in the agent's
        # transcript as context, without needing a session-start hook.
        print(config.PROMPT)


def _report_output(keys):
    on = hooks.is_on(keys)
    print(f"output: {'on' if on else 'off'} "
          f"(provider={config.provider()}, voice={config.active_voice()}, "
          f"model={config.active_model()})")
    if on:
        print(config.PROMPT)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="parley",
        description="Two-way voice for terminal coding agents.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("on", help="start two-way voice in this tmux pane")
    sub.add_parser("off", help="stop two-way voice in this tmux pane")
    sub.add_parser("toggle", help="flip two-way voice in this tmux pane")
    sub.add_parser("status", help="show speech and listening state")

    output = sub.add_parser("output", help="control spoken output only")
    output.add_argument("state", nargs="?", choices=["on", "off", "status"])

    default = sub.add_parser("default", help="what new sessions start as")
    default.add_argument("state", nargs="?", choices=["on", "off"])

    say = sub.add_parser("say", help="queue a line (status updates, asides)")
    say.add_argument("text", nargs="+")
    say.add_argument("--voice", help="OpenAI voice name or ElevenLabs voice ID")
    say.add_argument("--model")
    say.add_argument("--wait", action="store_true",
                     help="block until it has finished speaking")

    voices = sub.add_parser("voices", help="audition every voice")
    voices.add_argument("text", nargs="*")

    listen = sub.add_parser("listen", help="hands-free voice input")
    listen.add_argument(
        "state", nargs="?", choices=["on", "off", "status", "run", "meter"])
    listen.add_argument(
        "--seconds", type=float,
        help="meter: stop after this many seconds (default: until Ctrl-C)")
    listen.add_argument(
        "--device", default=os.environ.get("PARLEY_MIC", "default"),
        help="avfoundation audio device name/index (default follows macOS)")

    enroll = sub.add_parser(
        "enroll", help="record a local personalized trigger profile")
    enroll.add_argument(
        "--device", default=os.environ.get("PARLEY_MIC", "default"),
        help="avfoundation audio device name/index (default follows macOS)")

    debug = sub.add_parser(
        "debug-triggers",
        help="replay labelled audio and report wake hit / false-wake rates")
    debug.add_argument(
        "directory", nargs="?",
        help="WAV files named *-<label>.wav (default ~/.parley/bursts)")
    debug.add_argument("--synthetic", action="store_true",
                       help="generate test speech with macOS voices instead")
    debug.add_argument("--whisper", action="store_true",
                       help="also run local tiny Whisper (slower, realistic)")
    debug.add_argument("--verbose", action="store_true",
                       help="print one JSON line per clip")
    debug.add_argument("--json", action="store_true")

    sub.add_parser("stop", help="drop the queue and stop playing")
    sub.add_parser("cues", help="regenerate the notification tones")

    for name, help_text in [("install", "add the hook to your agent's settings"),
                            ("uninstall", "remove the hook")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--harness", choices=sorted(hooks.TARGETS))

    relay_cmd = sub.add_parser(
        "relay", help="speak and listen on another machine (agents run here)")
    relay_cmd.add_argument(
        "state", nargs="?", choices=["status", "set", "off"], default="status")
    relay_cmd.add_argument(
        "host", nargs="?", help="set: SSH address of the speaker, e.g. me@my-mac")
    relay_cmd.add_argument(
        "--self", dest="self_host",
        help="set: SSH address the speaker uses to reach this machine")

    remote = sub.add_parser("remote", help=argparse.SUPPRESS)
    remote.add_argument("op")
    remote.add_argument("--host", default="")
    remote.add_argument("--pane", default="")
    remote.add_argument("--follow", action="store_true")
    remote.add_argument("--force", action="store_true")
    remote.add_argument("--device", default="default")

    sub.add_parser("hook", help="hook entry point (payload on stdin)")
    indicator = sub.add_parser("indicator", help=argparse.SUPPRESS)
    indicator.add_argument("pane", nargs="?")
    indicator.add_argument("--json", action="store_true")
    return parser


def _report_listener(listener):
    pid = listener.is_running()
    print(f"listening: {'on (pid ' + str(pid) + ')' if pid else 'off'}")
    if not pid:
        return
    print(f"  state {listener.listener_state()}")
    if listener.triggers.enrolled():
        personalized = "active"
    elif listener.triggers.stale():
        personalized = "profile is from an older matcher; run parley enroll"
    else:
        personalized = "not enrolled"
    print(f"  personalized triggers {personalized}")
    print(f"  wake {listener.WAKE!r} -> speak -> {listener.SEND!r}")
    pane = listener.get_target()
    label = listener.target_label(pane)
    if label:
        print(f"  sends to {label} (pane {pane})")
    else:
        print(f"  sends to unavailable pane {pane or '(none)'}")


def _start_listener(listener, device):
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        print("Not inside tmux — there is no pane to type the message into.",
              file=sys.stderr)
        raise SystemExit(1)
    if not listener.whisper_bin():
        print("whisper-cli not found. Install it with: brew install whisper-cpp",
              file=sys.stderr)
        raise SystemExit(1)
    if not listener.ensure_model():
        raise SystemExit("could not download the wake-word model")
    listener.stop()
    listener.set_follow_active(listener.is_agent_deck_pane(pane))
    listener.set_target(pane)
    subprocess.Popen(
        [sys.argv[0], "listen", "run", "--device", device],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(0.6)
    if not listener.is_running():
        raise SystemExit("hands-free listener exited during startup")
    print(f"listening: on — say {listener.WAKE!r}, speak, then {listener.SEND!r}")
    print(f"  typing into pane {listener.get_target()}")


def _stop_owned_listener(listener):
    """Stop hands-free input only when it belongs to the invoking pane."""
    pane = os.environ.get("TMUX_PANE", "")
    if not pane or listener.get_target() != pane:
        return False
    return listener.stop()


def _relay_report(keys, host, prefix="parley"):
    on = hooks.is_on(keys)
    where = f" (speech relayed to {host})" if on else ""
    print(f"{prefix}: {'on' if on else 'off'} for this session{where}")
    if on:
        print(config.PROMPT)


def _relay_listener_status(host):
    try:
        status = relay.listen_status()
    except relay.RelayError as exc:
        print(f"listening: unknown — speaker {host} unreachable ({exc})")
        return
    if not status.get("running"):
        print(f"listening: off (on {host})")
        return
    print(f"listening: on (pid {status.get('pid')} on {status.get('speaker')})")
    print(f"  state {status.get('state')}")
    print(f"  personalized triggers "
          f"{'active' if status.get('personalized') else 'not enrolled'}")
    print(f"  wake {status.get('wake')!r} -> speak -> {status.get('send')!r}")
    target_host, pane = relay.split_target(status.get("target", ""))
    here = target_host == relay.self_address()
    label = status.get("label") or pane or "(none)"
    if here:
        label = hooks.session_label() if pane == os.environ.get(
            "TMUX_PANE") else label
    print(f"  sends to {label} (pane {pane}"
          f"{'' if here else ' on ' + (target_host or str(status.get('speaker')))})")
    if status.get("follow"):
        print("  follows the Agent Deck session on screen")


def _relay_listen_on(listener, host, device="default"):
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        print("Not inside tmux — there is no pane to type the message into.",
              file=sys.stderr)
        raise SystemExit(1)
    answer = relay.listen_on(
        pane, follow=listener.is_agent_deck_pane(pane), device=device)
    print(f"listening: on — the microphone is on {host}; say "
          f"{answer.get('wake')!r}, speak, then {answer.get('send')!r}")
    print(f"  typing into pane {pane} here")
    return answer


def _relay_main(command, args, host):
    """Commands on a relay remote: voice lives on the speaker."""
    from parley import indicator
    from parley import listen as listener

    keys = hooks.session_keys()
    if command == "toggle":
        command = "off" if hooks.is_on(keys) else "on"

    if command == "on":
        try:
            _relay_listen_on(
                listener, host, os.environ.get("PARLEY_MIC", "default"))
        except relay.RelayError as exc:
            hooks.turn_off(keys)
            indicator.refresh()
            raise SystemExit(f"parley: could not start listening on {host}: {exc}")
        hooks.turn_on(keys)
        indicator.refresh()
        _relay_report(keys, host)
        return
    if command == "off":
        hooks.turn_off(keys)
        relay.stop()
        try:
            if relay.listen_off(os.environ.get("TMUX_PANE", "")).get("stopped"):
                print("listening: stopped")
        except relay.RelayError as exc:
            print(f"listening: speaker {host} unreachable ({exc})")
        indicator.refresh()
        _relay_report(keys, host)
        return
    if command == "status":
        _relay_report(keys, host)
        _relay_listener_status(host)
        return

    if command == "output":
        state = args.state or "status"
        if state == "on":
            hooks.turn_on(keys)
        elif state == "off":
            hooks.turn_off(keys)
            relay.stop()
        indicator.refresh()
        _relay_report(keys, host, prefix="output")
        return

    if command == "listen":
        state = args.state or "status"
        if state in ("run", "meter"):
            raise SystemExit(
                f"parley: the microphone is on {host}; run `parley listen "
                f"{state}` there")
        try:
            if state == "on":
                _relay_listen_on(listener, host, args.device)
            elif state == "off":
                stopped = relay.listen_off(force=True).get("stopped")
                print("listening: stopped" if stopped else "listening: not running")
            else:
                _relay_listener_status(host)
        except relay.RelayError as exc:
            raise SystemExit(f"parley: speaker {host} unreachable: {exc}")
        indicator.refresh()
        return

    if command == "say":
        if not relay.say(
                " ".join(args.text), voice=args.voice, model=args.model,
                session=relay.remote_session(relay.self_address(), keys[0]),
                session_label=hooks.session_label(), wait=args.wait):
            raise SystemExit(
                f"parley: could not reach the speaker {host}; "
                "see ~/.parley/speak.log")
        return

    if command == "stop":
        relay.stop()
        print("parley: queue cleared")
        return

    if command in ("voices", "enroll", "debug-triggers"):
        raise SystemExit(
            f"parley: `parley {command}` uses the speaker's audio; run it on "
            f"{host}")


def _relay_command(args):
    state = args.state
    if state == "off":
        print("relay: off" if relay.disable() else "relay: was not configured")
        return
    if state == "set":
        if not args.host:
            raise SystemExit("parley relay set needs the speaker's SSH address")
        relay.configure(args.host, args.self_host)
    host = relay.speaker()
    hosts = relay.remote_hosts()
    if not host:
        print("relay: off — this machine speaks and listens itself")
        if hosts:
            print("  linked remotes: " + ", ".join(hosts))
        return
    print(f"relay: speech and listening on {host}; "
          f"it reaches this machine as {relay.self_address()}")
    failed = False
    try:
        speaker = relay.ping()
        listening = (f"listener pid {speaker['listener']}"
                     if speaker.get("listener") else "listener off")
        print(f"  speaker: parley {speaker.get('version')} on "
              f"{speaker.get('host')} ({listening})")
        if not speaker.get("gui"):
            failed = True
            print("  ! no Terminal-owned tmux server on the speaker: macOS "
                  "denies SSH sessions the microphone and keychain")
        if not speaker.get("whisper"):
            print("  ! whisper-cli missing on the speaker: brew install whisper-cpp")
    except relay.RelayError as exc:
        print(f"  ! speaker unreachable: {exc}")
        raise SystemExit(1)
    try:
        back = relay.ping_back().get("back", {})
        print(f"  return path: ok (parley {back.get('version')} on "
              f"{back.get('host')})")
    except relay.RelayError as exc:
        failed = True
        print(f"  ! return path failed: the speaker cannot reach "
              f"{relay.self_address()} ({exc}); dictation cannot be delivered")
    if failed:
        raise SystemExit(1)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Preserve older scripts without exposing a seventh user-facing toggle.
    if argv and argv[0] == "speak":
        argv[0] = "output"
    # A harness may pass the payload as a bare JSON argument.
    if argv and argv[0].strip().startswith("{"):
        hooks.handle(argv=argv)
        return

    args = build_parser().parse_args(argv)
    command = args.command or "hook"

    if command == "hook":
        hooks.handle(argv=argv)
        return

    if command == "remote":
        relay.serve(args.op, host=args.host, pane=args.pane,
                    follow=args.follow, force=args.force, device=args.device)
        return

    if command == "relay":
        _relay_command(args)
        return

    host = relay.speaker()
    if host and command in (
            "on", "off", "toggle", "status", "output", "listen", "say",
            "stop", "voices", "enroll", "debug-triggers"):
        _relay_main(command, args, host)
        return

    if command == "indicator":
        from parley import indicator

        if args.json:
            import json

            print(json.dumps(indicator.snapshot(args.pane or ""),
                             ensure_ascii=False))
        else:
            print(indicator.text(args.pane or ""), end="")
        return

    if command == "install":
        hooks.install(args.harness)
        return

    if command == "uninstall":
        hooks.uninstall(args.harness)
        stop()
        return

    if command == "cues":
        from parley import cues

        cues.rebuild()
        print("Regenerated: " + ", ".join(sorted(cues.PATTERNS)))
        return

    if command == "default":
        config.STATE.mkdir(parents=True, exist_ok=True)
        if args.state == "on":
            config.DEFAULT.touch()
        elif args.state == "off":
            config.DEFAULT.unlink(missing_ok=True)
        print(f"parley default: {'on' if config.DEFAULT.exists() else 'off'}")
        return

    if command == "output":
        keys = hooks.session_keys()
        state = args.state or "status"
        if state == "on":
            hooks.turn_on(keys)
        elif state == "off":
            hooks.turn_off(keys)
            stop()
        from parley import indicator

        indicator.refresh()
        _report_output(keys)
        return

    if command in ("on", "off", "toggle", "status"):
        from parley import indicator
        from parley import listen as listener

        keys = hooks.session_keys()
        if command == "toggle":
            command = "off" if hooks.is_on(keys) else "on"
        if command == "on":
            # Launch input first. A failed full-duplex start must not silently
            # leave the session in the split speech-only state.
            try:
                _start_listener(
                    listener, os.environ.get("PARLEY_MIC", "default"))
            except (Exception, SystemExit):
                hooks.turn_off(keys)
                indicator.refresh()
                raise
            hooks.turn_on(keys)
        elif command == "off":
            hooks.turn_off(keys)
            stop()
            if _stop_owned_listener(listener):
                print("listening: stopped")

        indicator.refresh()
        _report(keys)
        if command == "status":
            _report_listener(listener)
        return

    if command == "listen":
        from parley import listen as listener

        state = args.state or "status"
        if state == "run":
            listener.run(args.device, recover_devices=True)
            return
        if state == "meter":
            from parley import evaluate

            evaluate.meter(args.device, args.seconds)
            return
        if state == "off":
            print("listening: stopped" if listener.stop() else "listening: not running")
            return
        if state == "status":
            _report_listener(listener)
            return
        _start_listener(listener, args.device)
        return

    if command == "enroll":
        from parley import listen as listener
        from parley import triggers

        was_running = bool(listener.is_running())
        previous_pane = listener.get_target()
        # Enrollment is interactive and belongs to the pane that invoked it.
        # Prefer that pane over a still-live but stale listener target left by
        # another session. Outside tmux, preserve the previous target.
        pane = os.environ.get("TMUX_PANE", "") or previous_pane
        if was_running:
            listener.stop()
            time.sleep(0.3)
        print("Raw recordings stay in memory; only local acoustic features are saved.")
        try:
            metadata = triggers.save(triggers.collect(args.device))
        finally:
            if was_running and pane:
                listener.set_target(pane)
                subprocess.Popen(
                    [sys.argv[0], "listen", "run", "--device", args.device],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                time.sleep(0.6)
                print(f"Listening restarted for pane {pane}.")
        print("Personalized triggers: active")
        for name, threshold in metadata["thresholds"].items():
            print(f"  {name}: threshold {threshold:.3f}")
        return

    if command == "debug-triggers":
        from parley import evaluate

        evaluate.debug_triggers(
            args.directory, synthetic=args.synthetic, whisper=args.whisper,
            verbose=args.verbose, as_json=args.json)
        return

    if command == "stop":
        stop()
        print("parley: queue cleared")
        return

    if command == "say":
        keys = hooks.session_keys()
        enqueue(
            " ".join(args.text),
            voice=args.voice,
            model=args.model,
            session=keys[0],
            session_label=hooks.session_label(),
        )
        if args.wait:
            drain()
        else:
            detach(drain)
        return

    if command == "voices":
        if config.provider() == "elevenlabs":
            from parley.tts import voices

            active = config.active_voice()
            for item in voices():
                marker = "*" if item["voice_id"] == active else " "
                print(f"{marker} {item.get('name', 'Unnamed')}: {item['voice_id']}")
            print("Choose one with: export PARLEY_ELEVENLABS_VOICE_ID=<id>")
            return
        sample = (
            " ".join(args.text)
            or "This is how I sound reading your replies aloud."
        )
        for voice in config.VOICES:
            enqueue(f"{voice}. {sample}", voice=voice)
        print("Queued " + ", ".join(config.VOICES))
        print("Choose one with: export PARLEY_VOICE=<name>")
        detach(drain)
        return
