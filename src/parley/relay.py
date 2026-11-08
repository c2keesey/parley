"""Remote relay: agents on another machine, voice on this Mac.

Parley's microphone and speakers belong to one machine, the *speaker*: the Mac
you sit at. Agents often run somewhere else, a *remote* such as a Linux box
reached over Mosh or SSH, inside its own tmux and Agent Deck. The relay gives
those agents the same two-way voice as a local one:

* A finished reply on the remote is forwarded to the speaker and joins its
  ordinary queue, announced by session like any other.
* The speaker's listener types dictation back into the remote pane, follows
  whichever remote session is on screen, and mirrors its live state to the
  remote, so the Claude Code band and tmux badge there stay truthful.

Everything rides on plain SSH; both machines already reach each other over the
tailnet. The remote calls the speaker with one short ``parley remote`` command
per event. The speaker's listener keeps a single long-lived ``parley remote
agent`` connection to each remote and talks JSON lines over it, so following
focus and mirroring state cost no process start.

macOS does not grant an SSH session the microphone or the login keychain. Work
that needs either is therefore started inside the Terminal-owned tmux server on
the Mac (``tmux run-shell -b``), which holds both grants, rather than in the
SSH session that asked for it.
"""
import getpass
import json
import os
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from parley import __version__, config

RELAY_FILE = Path(os.environ.get(
    "PARLEY_RELAY_CONFIG", Path.home() / ".config" / "parley" / "relay.json"))
REMOTE_PREFIX = "remote:"
# The remote trusts a mirrored state this long without a heartbeat.
MIRROR_FRESH_SECONDS = 15
# A remote's focus report is used for routing only while this fresh.
FOCUS_FRESH_SECONDS = 3
TICK_SECONDS = 0.25
FOCUS_SECONDS = 0.5
HEARTBEAT_SECONDS = 5
RETRY_SECONDS = 3
CONNECT_TIMEOUT = 15

SSH_OPTIONS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
]
# Non-interactive SSH gets a bare PATH; uv tools and Homebrew live here.
REMOTE_PATH = "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
TMUX_CANDIDATES = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux")


class RelayError(RuntimeError):
    """The other machine could not be reached or refused the request."""


# --------------------------------------------------------------------------
# Configuration


def _hosts_file():
    return config.STATE / "relay-hosts"


def _mirror_file():
    return config.STATE / "relay-listener.json"


def _label_file():
    return config.STATE / "relay-target-label"


def _inbox():
    return config.STATE / "relay-inbox"


def settings():
    try:
        data = json.loads(RELAY_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def speaker():
    """The SSH address of the machine that speaks and listens, if relayed."""
    override = os.environ.get("PARLEY_RELAY", "").strip()
    if override:
        return None if override.lower() in {"off", "0", "none", "local"} else override
    value = settings().get("speaker")
    return value.strip() if isinstance(value, str) and value.strip() else None


def self_address():
    """How the speaker reaches this machine back over SSH."""
    override = os.environ.get("PARLEY_RELAY_SELF", "").strip()
    if override:
        return override
    value = settings().get("self")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}"


def configure(speaker_host, self_host=None):
    data = {"speaker": speaker_host, "self": self_host or self_address()}
    config.private_write(RELAY_FILE, json.dumps(data, indent=2) + "\n")
    return data


def disable():
    existed = RELAY_FILE.exists()
    RELAY_FILE.unlink(missing_ok=True)
    return existed


# --------------------------------------------------------------------------
# Targets: a dictation target on another machine is "remote:<host>/<pane>".


def remote_target(host, pane):
    return f"{REMOTE_PREFIX}{host}/{pane}"


def split_target(target):
    """(host, pane) for a remote target, (None, pane) for a local one."""
    target = target or ""
    if not target.startswith(REMOTE_PREFIX):
        return None, target
    host, _, pane = target[len(REMOTE_PREFIX):].rpartition("/")
    return (host or None), pane


def short_host(host):
    return (host or "").rsplit("@", 1)[-1].split(".")[0]


def remote_session(host, key):
    """A queue session key that cannot collide with a local pane's."""
    return remote_target(host, key)


def remote_hosts():
    try:
        lines = _hosts_file().read_text().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip()]


def add_host(host):
    hosts = remote_hosts()
    if host not in hosts:
        hosts.append(host)
        config.private_write(_hosts_file(), "\n".join(hosts) + "\n")


def clear_hosts():
    _hosts_file().unlink(missing_ok=True)
    _label_file().unlink(missing_ok=True)


def target_label():
    try:
        return _label_file().read_text().strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------
# SSH plumbing


def parley_argv():
    """How to start this same Parley again as a detached process."""
    first = sys.argv[0] if sys.argv else ""
    if first and os.path.basename(first).startswith("parley") and os.path.exists(first):
        return [os.path.abspath(first)]
    found = shutil.which("parley")
    return [found] if found else [sys.executable, "-m", "parley"]


def remote_command(args):
    binary = os.environ.get("PARLEY_REMOTE_BIN", "parley")
    quoted = " ".join(shlex.quote(str(arg)) for arg in args)
    return f'PATH="{REMOTE_PATH}" {shlex.quote(binary)} remote {quoted}'


def _control_options():
    directory = config.private_directory(config.STATE / "ssh")
    return ["-o", f"ControlPath={directory}/%C"]


def _ensure_master(host):
    """Keep one multiplexed connection so per-event calls skip the handshake.

    Started explicitly with its stdio on /dev/null: an implicit
    ``ControlPersist`` master would inherit a caller's captured pipes and hold
    them open after the call that created it returned.
    """
    options = SSH_OPTIONS + _control_options()
    try:
        check = subprocess.run(
            ["ssh", *options, "-O", "check", host],
            capture_output=True, timeout=5, check=False)
        if check.returncode == 0:
            return
        subprocess.run(
            ["ssh", *options, "-o", "ControlMaster=yes",
             "-o", "ControlPersist=600", "-f", "-N", host],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=CONNECT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _last_json(text):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def call(host, args, payload=None, timeout=20):
    """Run ``parley remote <args>`` on host and return its JSON answer."""
    if not host:
        raise RelayError("no relay host configured")
    _ensure_master(host)
    argv = ["ssh", *SSH_OPTIONS, *_control_options(), "-o", "ControlMaster=no",
            host, remote_command(args)]
    try:
        result = subprocess.run(
            argv, input=json.dumps(payload) if payload is not None else "",
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RelayError(f"{host} did not answer within {timeout:.0f}s")
    except OSError as exc:
        raise RelayError(f"could not run ssh: {exc}")
    answer = _last_json(result.stdout)
    if answer is None:
        detail = (result.stderr or result.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"ssh exited {result.returncode}"
        if result.returncode == 127 or "not found" in reason:
            reason = f"parley is not installed on {host} ({reason})"
        raise RelayError(reason)
    if not answer.get("ok", False):
        raise RelayError(answer.get("error") or f"{host} refused the request")
    return answer


# --------------------------------------------------------------------------
# Remote side: forwarding to the speaker


def say(text, session=None, session_label=None, voice=None, model=None,
        wait=False):
    """Queue speech on the speaker. Returns False when it cannot be reached."""
    host = speaker()
    payload = {"text": text, "session": session, "session_label": session_label,
               "voice": voice, "model": model, "wait": bool(wait)}
    try:
        call(host, ["say"], payload, timeout=600 if wait else 20)
    except RelayError as exc:
        config.log(f"relay say to {host} failed: {exc}")
        return False
    config.log(f"relayed {len(text)}c to {host}")
    return True


def stop():
    try:
        call(speaker(), ["stop"])
        return True
    except RelayError as exc:
        config.log(f"relay stop failed: {exc}")
        return False


def listen_on(pane, follow, device="default"):
    args = ["listen-on", "--host", self_address(), "--pane", pane,
            "--device", device]
    if follow:
        args.append("--follow")
    return call(speaker(), args, timeout=60)


def listen_off(pane="", force=False):
    args = ["listen-off", "--host", self_address(), "--pane", pane]
    if force:
        args.append("--force")
    return call(speaker(), args)


def listen_status():
    return call(speaker(), ["listen-status"])


def ping(host=None):
    return call(host or speaker(), ["ping"])


def ping_back():
    """Ask the speaker to reach this machine, proving the return path."""
    return call(speaker(), ["ping-back", "--host", self_address()], timeout=40)


# --------------------------------------------------------------------------
# Speaker side: the GUI-session trampoline


def tmux_bin():
    found = shutil.which("tmux")
    if found:
        return found
    return next((path for path in TMUX_CANDIDATES if os.path.exists(path)), None)


def in_gui_session():
    """Whether this process already holds the Mac's mic and keychain grants."""
    if os.environ.get("TMUX"):
        return True
    return not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))


def gui_available():
    if in_gui_session():
        return True
    tmux = tmux_bin()
    if not tmux:
        return False
    try:
        return subprocess.run(
            [tmux, "list-sessions"], capture_output=True, timeout=3,
            check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def gui_spawn(argv):
    """Start argv detached where macOS grants the microphone and keychain.

    Returns ``direct`` when this process already has them, ``tmux`` when the
    command was handed to the Terminal-owned tmux server, or ``ssh`` when
    neither was possible and it was started here anyway.
    """
    if not in_gui_session():
        tmux = tmux_bin()
        if tmux:
            command = shlex.join(argv) + " </dev/null >/dev/null 2>&1"
            try:
                handed = subprocess.run(
                    [tmux, "run-shell", "-b", command],
                    capture_output=True, timeout=5, check=False)
                if handed.returncode == 0:
                    return "tmux"
            except (OSError, subprocess.SubprocessError):
                pass
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    return "direct" if in_gui_session() else "ssh"


# --------------------------------------------------------------------------
# Speaker side: endpoints run over SSH as `parley remote <op>`


def _emit(answer):
    print(json.dumps(answer, ensure_ascii=False), flush=True)


def _read_payload(stream):
    try:
        raw = stream.read()
    except OSError:
        return {}
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _say(payload):
    from parley import player

    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": True, "queued": False}
    inbox = config.private_directory(_inbox())
    name = f"{time.time_ns():020d}-{os.getpid()}.json"
    item = inbox / name
    config.private_write(inbox / (name + ".tmp"), json.dumps(payload))
    (inbox / (name + ".tmp")).rename(item)
    via = gui_spawn(parley_argv() + ["remote", "deliver"])
    if payload.get("wait"):
        deadline = time.time() + 600
        time.sleep(0.5)
        while time.time() < deadline and (
                item.exists() or player.active() or player.output_playing()):
            time.sleep(0.2)
    return {"ok": True, "queued": True, "via": via}


def deliver():
    """Move relayed speech from the inbox into the queue, then play it."""
    from parley import player

    inbox = config.private_directory(_inbox())
    for item in sorted(p for p in inbox.iterdir() if p.suffix == ".json"):
        claimed = item.with_suffix(".claimed")
        try:
            item.rename(claimed)
        except OSError:
            continue  # another deliverer took it
        try:
            payload = json.loads(claimed.read_text())
        except (OSError, ValueError):
            payload = {}
        finally:
            claimed.unlink(missing_ok=True)
        player.enqueue(
            str(payload.get("text") or ""),
            voice=payload.get("voice"),
            model=payload.get("model"),
            session=payload.get("session"),
            session_label=payload.get("session_label"),
        )
    player.drain()


def _listener_summary():
    from parley import listen

    pid = listen.is_running()
    target = listen.get_target()
    host, pane = split_target(target)
    if host:
        label = target_label() or f"{pane} on {short_host(host)}"
    else:
        label = listen.indicator.session_label(pane) if pane else ""
    return {
        "ok": True,
        "running": bool(pid),
        "pid": pid,
        "state": listen.listener_state() if pid else "off",
        "target": target,
        "host": host or socket.gethostname().split(".")[0],
        "pane": pane,
        "label": label,
        "follow": listen.follows_active(),
        "wake": listen.WAKE,
        "send": listen.SEND,
        "personalized": listen.triggers.enrolled(),
        "speaker": socket.gethostname().split(".")[0],
    }


def _listen_on(host, pane, follow, device):
    from parley import listen

    if not host or not pane:
        return {"ok": False, "error": "listen-on needs --host and --pane"}
    running = listen.is_running()
    if not running:
        if not gui_available():
            return {"ok": False, "error": (
                "the Mac has no Terminal-owned tmux server, and macOS denies "
                "the microphone to SSH sessions; open a terminal tab running "
                "tmux on the Mac (Agent Deck does) and try again")}
        if not listen.whisper_bin():
            return {"ok": False, "error": (
                "whisper-cli not found on the Mac; install it with: "
                "brew install whisper-cpp")}
        if not listen.ensure_model():
            return {"ok": False, "error": "could not download the wake-word model"}
    add_host(host)
    listen.set_follow_active(follow)
    listen.set_target(remote_target(host, pane))
    _label_file().unlink(missing_ok=True)
    if running:
        listen.indicator.refresh()
        answer = _listener_summary()
        answer["started"] = False
        return answer
    via = gui_spawn(parley_argv() + ["listen", "run", "--device", device])
    deadline = time.time() + 4
    while time.time() < deadline and not listen.is_running():
        time.sleep(0.1)
    if not listen.is_running():
        return {"ok": False, "error": "the Mac's listener exited during startup; "
                "see ~/.parley/speak.log there"}
    answer = _listener_summary()
    answer["started"] = True
    answer["via"] = via
    return answer


def _listen_off(host, pane, force):
    from parley import listen

    target_host, target_pane = split_target(listen.get_target())
    owned = target_host == host and (force or target_pane == pane)
    if not owned:
        return {"ok": True, "stopped": False}
    return {"ok": True, "stopped": bool(listen.stop())}


def serve(op, host="", pane="", follow=False, force=False, device="default",
          stream=None):
    """Dispatch one ``parley remote`` endpoint and print its JSON answer."""
    from parley import player

    stream = stream or sys.stdin
    try:
        if op == "agent":
            serve_agent()
            return
        if op == "deliver":
            deliver()
            return
        if op == "ping":
            from parley import listen

            answer = {"ok": True, "version": __version__,
                      "host": socket.gethostname().split(".")[0],
                      "platform": sys.platform, "gui": gui_available(),
                      "listener": listen.is_running(),
                      "whisper": bool(listen.whisper_bin())}
        elif op == "ping-back":
            back = call(host, ["ping"], timeout=30)
            answer = {"ok": True, "back": back}
        elif op == "say":
            answer = _say(_read_payload(stream))
        elif op == "stop":
            player.stop()
            answer = {"ok": True}
        elif op == "listen-on":
            answer = _listen_on(host, pane, follow, device)
        elif op == "listen-off":
            answer = _listen_off(host, pane, force)
        elif op == "listen-status":
            answer = _listener_summary()
        else:
            answer = {"ok": False, "error": f"unknown relay operation {op!r}"}
    except RelayError as exc:
        answer = {"ok": False, "error": str(exc)}
    except Exception as exc:  # the caller needs an answer, not a traceback
        config.log(f"relay {op} failed: {exc!r}")
        answer = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _emit(answer)


# --------------------------------------------------------------------------
# Remote side: the agent the speaker's listener keeps connected


def mirror():
    """The speaker's mirrored listener state, while it is fresh."""
    try:
        data = json.loads(_mirror_file().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        fresh = time.time() - float(data.get("received", 0)) < MIRROR_FRESH_SECONDS
    except (TypeError, ValueError):
        return None
    return data if fresh and data.get("state", "off") != "off" else None


def _agent_reply(request):
    from parley import indicator, listen

    op = request.get("op")
    if op == "hello":
        return {"ok": True, "version": __version__,
                "host": socket.gethostname().split(".")[0]}
    if op == "active":
        activity, pane = listen.local_deck_focus()
        return {"ok": True, "pane": pane, "activity": activity}
    if op == "label":
        return {"ok": True, "label": indicator.session_label(
            str(request.get("pane") or ""))}
    if op == "inject":
        return {"ok": listen.paste_into(
            str(request.get("pane") or ""), str(request.get("text") or ""))}
    if op == "mirror":
        state = request.get("state")
        if not isinstance(state, dict) or state.get("state", "off") == "off":
            _mirror_file().unlink(missing_ok=True)
        else:
            state = dict(state, received=time.time())
            config.private_write(_mirror_file(), json.dumps(state))
        indicator.refresh()
        return {"ok": True}
    return {"ok": False, "error": f"unknown agent operation {op!r}"}


def serve_agent(stdin=None, stdout=None):
    """Answer JSON-line requests from the speaker's listener until it hangs up."""
    from parley import indicator

    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    last_ensure = 0.0

    def hang_up(signum, frame):
        raise SystemExit(0)

    # A dropped SSH connection arrives as SIGHUP; clean up the mirror anyway.
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(signum, hang_up)
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                reply = _agent_reply(request if isinstance(request, dict) else {})
            except Exception as exc:
                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            stdout.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
            stdout.flush()
            if time.time() - last_ensure >= HEARTBEAT_SECONDS:
                # Give sessions launched since the last check their badge.
                indicator.ensure()
                last_ensure = time.time()
    except (OSError, ValueError):
        pass
    finally:
        _mirror_file().unlink(missing_ok=True)
        indicator.refresh()


# --------------------------------------------------------------------------
# Speaker side: the persistent link held by the listener process


def agent_argv(host):
    """A dedicated connection: the link outlives any multiplexing master."""
    return ["ssh", *SSH_OPTIONS, host, remote_command(["agent"])]


class Agent:
    """One long-lived ``parley remote agent`` connection, safe across threads."""

    def __init__(self, host):
        self.host = host
        self._lock = threading.Lock()
        self._proc = None
        self._buffer = b""
        self._retry_at = 0.0
        self._failed = False

    def call(self, request, timeout=5.0):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                if time.monotonic() < self._retry_at:
                    return None
                if not self._connect():
                    self._retry_at = time.monotonic() + RETRY_SECONDS
                    return None
            try:
                return self._exchange(request, timeout)
            except (OSError, ValueError, TimeoutError) as exc:
                config.log(f"relay link to {self.host} dropped: {exc}")
                self._close()
                self._retry_at = time.monotonic() + RETRY_SECONDS
                return None

    def _connect(self):
        argv = agent_argv(self.host)
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
        except OSError as exc:
            config.log(f"relay link to {self.host} failed: {exc}")
            self._proc = None
            return False
        self._buffer = b""
        try:
            hello = self._exchange({"op": "hello"}, CONNECT_TIMEOUT)
        except (OSError, ValueError, TimeoutError) as exc:
            if not self._failed:
                config.log(f"relay link to {self.host} failed: {exc}")
            self._failed = True
            self._close()
            return False
        self._failed = False
        config.log(f"relay link to {self.host} up "
                   f"(remote parley {hello.get('version', '?')})")
        return True

    def _exchange(self, request, timeout):
        self._proc.stdin.write(json.dumps(request).encode() + b"\n")
        self._proc.stdin.flush()
        deadline = time.monotonic() + timeout
        fd = self._proc.stdout.fileno()
        while True:
            while b"\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition(b"\n")
                line = line.strip()
                if line.startswith(b"{"):  # skip anything a login shell printed
                    return json.loads(line)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no answer to {request.get('op')}")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise OSError("connection closed")
            self._buffer += chunk

    def _close(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except (OSError, AttributeError):
                pass
        try:
            # End of input lets the remote agent clear its mirror and exit.
            proc.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass

    def close(self):
        with self._lock:
            self._close()


_agents = {}
_agents_lock = threading.Lock()
_focus = {}  # host -> (activity, pane, monotonic time fetched)


def agent(host):
    with _agents_lock:
        if host not in _agents:
            _agents[host] = Agent(host)
        return _agents[host]


def remote_focus():
    """Fresh focus reports from connected remotes, for Deck-following.

    Returns ``None`` when a remote that armed the listener has not reported
    recently: routing must not swing to a local pane merely because the link
    to the remote the user is looking at blinked.
    """
    hosts = remote_hosts()
    candidates = []
    now = time.monotonic()
    for host in hosts:
        report = _focus.get(host)
        if not report or now - report[2] > FOCUS_FRESH_SECONDS:
            return None
        activity, pane, _ = report
        if pane:
            candidates.append((activity, remote_target(host, pane)))
    return candidates


def inject(host, pane, text):
    reply = agent(host).call(
        {"op": "inject", "pane": pane, "text": text}, timeout=15)
    return bool(reply and reply.get("ok"))


class Link(threading.Thread):
    """Follow remote focus and mirror the listener's state to each remote."""

    def __init__(self):
        super().__init__(name="parley-relay", daemon=True)
        self._stopped = threading.Event()
        self._pushed = {}  # host -> (snapshot, monotonic time)
        self._focused_at = {}
        self._labelled = ""
        self._local_label = (None, "")

    def stop(self):
        self._stopped.set()

    def run(self):
        while not self._stopped.is_set():
            try:
                self.tick()
            except Exception as exc:
                config.log(f"relay link error: {exc!r}")
            self._stopped.wait(TICK_SECONDS)
        for host in list(self._pushed):
            agent(host).close()

    def _state(self):
        from parley import listen

        state = listen.listener_state()
        if state == "capturing":
            return "listening"
        if state in ("sending", "reconnecting"):
            return state
        return "speaking" if listen.speaking() else "ready"

    def tick(self):
        from parley import listen

        hosts = remote_hosts()
        for host in [h for h in self._pushed if h not in hosts]:
            agent(host).call({"op": "mirror", "state": {"state": "off"}}, 2)
            agent(host).close()
            self._pushed.pop(host, None)
            _focus.pop(host, None)
        if not hosts:
            return
        target_host, target_pane = split_target(listen.get_target())
        if target_host and listen.get_target() != self._labelled:
            reply = agent(target_host).call(
                {"op": "label", "pane": target_pane}, 3)
            if reply and reply.get("ok"):
                label = reply.get("label") or target_pane
                config.private_write(
                    _label_file(), f"{label} on {short_host(target_host)}")
                self._labelled = listen.get_target()
        elif not target_host:
            self._labelled = ""
        now = time.monotonic()
        state = self._state()
        if target_host:
            elsewhere = target_label()
        elif self._local_label[0] == target_pane:
            elsewhere = self._local_label[1]
        else:
            name = listen.indicator.session_label(target_pane) or target_pane
            elsewhere = f"{name} on {socket.gethostname().split('.')[0]}"
            self._local_label = (target_pane, elsewhere)
        for host in hosts:
            link = agent(host)
            due = now - self._focused_at.get(host, 0) >= FOCUS_SECONDS
            if listen.follows_active() and due:
                self._focused_at[host] = now
                reply = link.call({"op": "active"}, 2)
                if reply and reply.get("ok"):
                    _focus[host] = (int(reply.get("activity") or 0),
                                    str(reply.get("pane") or ""), time.monotonic())
            snap = {
                "state": state,
                "pane": target_pane if target_host == host else "",
                "label": "" if target_host == host else elsewhere,
                "wake": listen.WAKE, "send": listen.SEND,
                "stop": listen.STOP_TALKING,
                "speaker": socket.gethostname().split(".")[0],
            }
            previous = self._pushed.get(host)
            if (previous is None or previous[0] != snap
                    or now - previous[1] >= HEARTBEAT_SECONDS):
                if link.call({"op": "mirror", "state": snap}, 2):
                    self._pushed[host] = (snap, now)
                    if previous is None or previous[0] != snap:
                        listen.indicator.refresh()
