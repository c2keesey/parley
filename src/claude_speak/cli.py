"""Command line interface."""
import argparse
import os
import subprocess
import sys
import time

from claude_speak import __version__, config, hooks
from claude_speak.player import detach, drain, enqueue, stop


def _session_id():
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")

def _require_session():
    session_id = _session_id()
    if not session_id:
        print(
            "No CLAUDE_CODE_SESSION_ID — run this inside a Claude Code session.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return session_id


def _report(session_id):
    on = hooks.is_on(session_id)
    print(f"claude-speak: {'on' if on else 'off'} for this session "
          f"(voice={config.VOICE}, model={config.MODEL})")
    if on:
        # Printed so that turning voice on mid-session lands in the transcript
        # as context, the same instruction the SessionStart hook injects.
        print(config.PROMPT)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="claude-speak",
        description="Voice output for Claude Code.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("on", help="speak replies in this session")
    sub.add_parser("off", help="stop speaking in this session")
    sub.add_parser("toggle", help="flip this session")
    sub.add_parser("status", help="show this session's state")

    default = sub.add_parser("default", help="what new sessions start as")
    default.add_argument("state", nargs="?", choices=["on", "off"])

    say = sub.add_parser("say", help="queue a line (status updates, asides)")
    say.add_argument("text", nargs="+")
    say.add_argument("--voice", choices=config.VOICES)
    say.add_argument("--model")
    say.add_argument("--wait", action="store_true",
                     help="block until it has finished speaking")

    voices = sub.add_parser("voices", help="audition every voice")
    voices.add_argument("text", nargs="*")

    listen = sub.add_parser("listen", help="hands-free voice input")
    listen.add_argument("state", nargs="?", choices=["on", "off", "status", "run"])
    listen.add_argument("--device", default=os.environ.get("CLAUDE_SPEAK_MIC", "0"),
                        help="avfoundation audio device index")

    sub.add_parser("stop", help="drop the queue and stop playing")
    sub.add_parser("install", help="add the hooks to Claude Code settings")
    sub.add_parser("uninstall", help="remove the hooks")
    sub.add_parser("hook", help="hook entry point (payload on stdin)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    command = args.command or "hook"

    if command == "hook":
        hooks.handle()
        return

    if command == "install":
        hooks.install()
        return

    if command == "uninstall":
        hooks.uninstall()
        stop()
        return

    if command == "default":
        config.STATE.mkdir(parents=True, exist_ok=True)
        if args.state == "on":
            config.DEFAULT.touch()
        elif args.state == "off":
            config.DEFAULT.unlink(missing_ok=True)
        print(f"claude-speak default: "
              f"{'on' if config.DEFAULT.exists() else 'off'}")
        return

    if command in ("on", "off", "toggle", "status"):
        session_id = _require_session()
        if command == "toggle":
            command = "off" if hooks.is_on(session_id) else "on"
        if command == "on":
            hooks.turn_on(session_id)
        elif command == "off":
            hooks.turn_off(session_id)
            stop()
        _report(session_id)
        return

    if command == "listen":
        from claude_speak import listen as listener

        state = args.state or "status"
        if state == "run":
            listener.run(args.device)
            return
        if state == "off":
            print("listening: stopped" if listener.stop() else "listening: not running")
            return
        if state == "status":
            pid = listener.is_running()
            target = listener.get_target()
            print(f"listening: {'on (pid ' + str(pid) + ')' if pid else 'off'}")
            if pid:
                print(f"  wake {listener.WAKE!r} -> speak -> {listener.SEND!r}")
                print(f"  sends to tmux pane {target or '(none)'}")
            return

        pane = os.environ.get("TMUX_PANE", "")
        if not pane:
            print("Not inside tmux — there is no pane to send the message to.",
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
        print(f"  sending to tmux pane {pane}")
        return

    if command == "stop":
        stop()
        print("claude-speak: queue cleared")
        return

    if command == "say":
        enqueue(" ".join(args.text), voice=args.voice, model=args.model)
        if args.wait:
            drain()
        else:
            detach(drain)
        return

    if command == "voices":
        sample = " ".join(args.text) or "This is how I sound reading your replies aloud."
        for voice in config.VOICES:
            enqueue(f"{voice}. {sample}", voice=voice)
        print("Queued " + ", ".join(config.VOICES))
        print("Choose one with: export CLAUDE_SPEAK_VOICE=<name>")
        detach(drain)
        return
