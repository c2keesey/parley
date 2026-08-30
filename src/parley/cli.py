"""Command line interface."""
import argparse
import json
import os
import subprocess
import sys
import time

from parley import __version__, config, hooks
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


def _status_snapshot():
    """Credential-free process boundary for local status surfaces.

    The listener's persisted target is authoritative because that is where
    dictation will actually be sent. The caller's pane is a fallback only when
    no listener target exists. No provider discovery belongs here: provider
    discovery can consult environment files or Keychain.
    """
    from parley import indicator
    from parley import listen as listener

    pane = listener.get_target() or os.environ.get("TMUX_PANE", "")
    label = indicator.session_label(pane) if pane else ""
    listener_running = bool(listener.is_running())
    return {
        "contract_version": 1,
        "listener_running": listener_running,
        "listener_state": (
            listener.listener_state() if listener_running else "off"
        ),
        "speaking": listener.speaking(),
        "target": {
            "available": bool(label),
            "label": label or None,
            "pane": pane or None,
        },
        "voice_on": hooks.pane_is_on(pane),
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="parley",
        description="Two-way voice for terminal coding agents.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("on", help="speak replies in this session")
    sub.add_parser("off", help="stop speaking in this session")
    sub.add_parser("toggle", help="flip this session")
    status = sub.add_parser("status", help="show this session's state")
    status.add_argument(
        "--json",
        action="store_true",
        help="emit the credential-free local status contract",
    )

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
    listen.add_argument("state", nargs="?", choices=["on", "off", "status", "run"])
    listen.add_argument("--device", default=os.environ.get("PARLEY_MIC", "0"),
                        help="avfoundation audio device index")

    enroll = sub.add_parser(
        "enroll", help="record a local personalized trigger profile")
    enroll.add_argument("--device", default=os.environ.get("PARLEY_MIC", "0"),
                        help="avfoundation audio device index")

    sub.add_parser("stop", help="drop the queue and stop playing")
    sub.add_parser("cues", help="regenerate the notification tones")

    for name, help_text in [("install", "add the hook to your agent's settings"),
                            ("uninstall", "remove the hook")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--harness", choices=sorted(hooks.TARGETS))

    sub.add_parser("hook", help="hook entry point (payload on stdin)")
    indicator = sub.add_parser("indicator", help=argparse.SUPPRESS)
    indicator.add_argument("pane", nargs="?")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # A harness may pass the payload as a bare JSON argument.
    if argv and argv[0].strip().startswith("{"):
        hooks.handle(argv=argv)
        return

    args = build_parser().parse_args(argv)
    command = args.command or "hook"

    if command == "hook":
        hooks.handle(argv=argv)
        return

    if command == "indicator":
        from parley.indicator import text

        print(text(args.pane or ""), end="")
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

    if command in ("on", "off", "toggle", "status"):
        if command == "status" and args.json:
            print(json.dumps(_status_snapshot(), sort_keys=True))
            return
        keys = hooks.session_keys()
        if command == "toggle":
            command = "off" if hooks.is_on(keys) else "on"
        if command == "on":
            hooks.turn_on(keys)
        elif command == "off":
            hooks.turn_off(keys)
            stop()
        from parley import indicator

        indicator.refresh()
        _report(keys)
        return

    if command == "listen":
        from parley import listen as listener

        state = args.state or "status"
        if state == "run":
            listener.run(args.device)
            return
        if state == "off":
            print("listening: stopped" if listener.stop() else "listening: not running")
            return
        if state == "status":
            pid = listener.is_running()
            print(f"listening: {'on (pid ' + str(pid) + ')' if pid else 'off'}")
            if pid:
                print(f"  state {listener.listener_state()}")
                print(
                    f"  personalized triggers "
                    f"{'active' if listener.triggers.enrolled() else 'not enrolled'}")
                print(f"  wake {listener.WAKE!r} -> speak -> {listener.SEND!r}")
                pane = listener.get_target()
                label = listener.indicator.session_label(pane)
                if label:
                    print(f"  sends to {label} (pane {pane})")
                else:
                    print(f"  sends to unavailable pane {pane or '(none)'}")
            return

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
        listener.set_target(pane)
        subprocess.Popen(
            [sys.argv[0], "listen", "run", "--device", args.device],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(0.6)
        print(f"listening: on — say {listener.WAKE!r}, speak, then {listener.SEND!r}")
        print(f"  typing into pane {pane}")
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

    if command == "stop":
        stop()
        print("parley: queue cleared")
        return

    if command == "say":
        enqueue(" ".join(args.text), voice=args.voice, model=args.model)
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
