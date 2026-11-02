# parley

[![CI](https://github.com/c2keesey/parley/actions/workflows/ci.yml/badge.svg)](https://github.com/c2keesey/parley/actions/workflows/ci.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Your coding agent, out loud

Parley turns terminal coding agents into collaborators you can actually talk
with. Give an agent a task, lean back or walk away, and hear its response when
it is ready. Say “okay computer” to reply, then “send it” when you are done.
No keyboard required.

It works with [Claude Code](https://claude.com/claude-code) and
[Codex](https://developers.openai.com/codex/cli), and it is designed for the
way people really use them:

- **Keep your eyes on the work, not the terminal.** Parley reads completed
  replies aloud and lets an agent speak during a long-running task.
- **Have a real back-and-forth.** Dictate a response, interrupt naturally, or
  permanently stop speech with a dedicated voice command.
- **Run several agents without losing the thread.** Responses announce their
  session when output switches agents, without repeating it every turn.
- **Choose the voice quality you want.** Use the built-in local voice for free,
  or add OpenAI or ElevenLabs for more natural speech.
- **Keep always-on listening local.** Wake and control phrases are recognized
  on your Mac; cloud transcription happens only after you choose to send a
  dictated message.

Parley is per-session, so you decide which agent receives your hands-free
dictation and which agents can speak. Turn it on when you want a conversation
and leave every other terminal quiet.

## Install

```sh
uv tool install git+https://github.com/c2keesey/parley.git
parley install              # wires up every agent it finds
```

The distribution is named `parley-voice` because the `parley` name on PyPI
belongs to an unrelated project. The installed command remains `parley`.
Source installation is the supported path until the first PyPI release.

`parley install` writes a `Stop` hook and the `parley` skill into each agent
present on the machine, and for Claude Code also installs the status band
(below):

| Agent | Hook | Skill | Status |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | `~/.claude/skills/parley/` | `parley@parley` band above the prompt (tmux badge elsewhere) |
| Codex | `~/.codex/hooks.json` | `~/.codex/skills/parley/` | tmux badge |

It is idempotent, leaves any hooks you already have alone, and backs up the
settings file before writing. Pass `--harness claude-code` or `--harness codex`
to target one. To undo it:

```sh
parley uninstall            # optionally --harness <name>
```

With the skill installed you can just say "voice on" or "voice off" to the
agent, in either tool, instead of running the command yourself.

API keys are optional. With no keys, Parley uses macOS speech synthesis and a
local Whisper model for full-message transcription. An OpenAI key upgrades the
fallback voice to Onyx and enables cloud transcription.

For higher-quality speech, store an ElevenLabs key in macOS Keychain under
service `parley-elevenlabs-api-key` and your macOS account name. You can also
set it in the environment or the shared env file. In the default `auto` mode,
its presence switches speech output to ElevenLabs while OpenAI remains
available for hands-free transcription. In `auto` mode the fallback chain is
George, then OpenAI's Onyx voice, then local macOS speech. Failed paid services
are retried periodically:

```sh
ELEVENLABS_API_KEY=...
OPENAI_API_KEY=sk-...        # optional cloud fallback and transcription
```

Parley starts with ElevenLabs' George voice and `eleven_v3_conversational`. Run
`parley voices` after adding the key to see the voices in your account, then
set `PARLEY_ELEVENLABS_VOICE_ID` to the one you want. Set
`PARLEY_TTS_PROVIDER=openai` at any time to keep using OpenAI for speech.

macOS only for now — playback uses `afplay`. Basic spoken output works without
`ffmpeg`; hands-free listening requires it for microphone capture and for
resuming the unspoken remainder after you interrupt a reply.

## Use

```sh
parley on                 # spoken replies + hands-free input in this pane
parley off                # stop both for this pane
parley status             # report both states

parley listen on          # input only
parley listen off
parley output on          # output only
parley output off

parley say "Tests are green, deploying now."
parley say --voice nova "Heads up, that migration looks wrong."
parley say --wait "..."   # block until spoken

parley voices             # audition OpenAI voices, or list ElevenLabs voices
parley stop               # drop the queue, silence everything
parley default on         # new sessions start in output-only mode
```

Turning it on mid-session works without a restart. `parley on` requires tmux,
starts spoken output, and starts or retargets the single hands-free listener to
the invoking pane. In Agent Deck, that pane is the fallback: the listener then
follows the session shown by the most recently active human terminal client and
rechecks the destination at send time. Background Deck control clients are
ignored. The confirmation `parley on` prints is the same instruction the startup
hook injects, so it lands in the transcript as context. Use `parley output on`
when spoken output without an active microphone is intentional.

## Hands-free input

`parley on` closes the loop: say the wake phrase, dictate, say the send phrase,
and the message is typed into your agent's pane and submitted. The lower-level
`parley listen` commands remain available for explicitly moving or inspecting
the single listener without changing speech output.

When you switch the session displayed by your terminal in Agent Deck, Parley
moves the listener target with it. The visible session's badge should therefore
settle on `THIS PANE`; a pane explicitly armed outside Agent Deck remains a
static target.

While listening is active, every session with Parley turned on shows its
state. In Claude Code it is a band directly above the prompt, and the tmux
badge stands aside for that pane; everywhere else (Codex, or a Claude Code
without the band) it is a badge in the tmux status bar. Both draw from the same `parley indicator --json <pane>` snapshot, so they
always agree:
`🎙 PARLEY READY` while the wake detector is armed, `🔴 PARLEY LISTENING` while
capturing dictation, `⏳ PARLEY SENDING` during transcription and submission,
`🎧 PARLEY RECONNECTING` while macOS has no usable input route, and
`🔊 PARLEY SPEAKING · MIC READY` during spoken output. It disappears if the
listener stops. The receiving pane says `THIS PANE`; another
Parley-enabled pane shows `⚠ SENDS TO <name>` so a stale target cannot look
like a dead microphone. Panes where Parley is off stay uncluttered. `parley
listen status` reports the target name together with the raw pane id.

```sh
brew install whisper-cpp     # one-time
parley listen on       # explicitly start or retarget input to this pane
parley listen status
parley listen off
parley enroll          # optional, personalized local trigger matching
```

Parley follows macOS audio routing by default. Connecting or disconnecting
AirPods makes a running listener reopen the new default input without changing
its target pane. If the route disappears during dictation, that partial turn is
discarded rather than joining audio from two microphones; listening resumes as
soon as an input is available. Set `PARLEY_MIC` to an AVFoundation device name
or index only when you intentionally want to pin one device. A pinned device is
retried until it returns and does not follow the system default.

Each spoken block starts on macOS' then-current default output. If that default
changes while a block is playing, Parley stops the stale `afplay` process and
resumes the unheard portion on the new route. macOS still controls Bluetooth
profile negotiation and the physical route; Parley handles its own capture and
playback process lifecycle around those changes.

Defaults: say **"okay computer"**, talk, then **"send it"**. A quiet low tone
confirms it woke, a soft two-note cue confirms it sent, and a falling tone
means capture expired. Say **"scratch that"** or **"scrap that"** as a
standalone utterance to discard the capture locally. The narrow matcher does
not discard ordinary dictation such as "scratch that migration." Change the
wake, send, and original cancel phrases with `PARLEY_WAKE`, `PARLEY_SEND`, and
`PARLEY_CANCEL`.

The local recognizer is biased toward these control phrases, and the audio gate
is tuned to retain fast command-sized utterances. `send it` ends capture only
when it is trailing, so the same words inside message content are preserved;
ordinary dictation such as `Sunday` never submits a message.

If trigger recognition is unreliable, run `parley enroll`. Parley guides you
through several natural repetitions of each control phrase and trains a local
speaker-personalized acoustic profile. Raw recordings remain in memory; only
normalized feature templates are saved under `~/.parley/triggers`, with private
permissions. Personalized matching runs beside tiny Whisper, tolerates changes
in pace, and considers send/cancel controls only on command-sized utterances so
mentions inside ordinary dictation remain content.

If you say **"okay computer"** again while dictating, the wake tone plays again
as an audible confirmation that capture is still active. Parley keeps
listening and removes the repeated wake phrase from the message it sends.

The cue meanings are deliberately distinct: one warm tone means capture is
open, a soft rising pair means the message was sent, two low taps confirm that
speech was stopped, one quiet dot means speech playback finished, and a darker
falling pair means capture was discarded or expired. Each played cue is logged
by name and source in `~/.parley/speak.log`.

While Parley is speaking, **"okay computer, stop talking"** is a dedicated
local voice-control command. It immediately skips only the current spoken
block, keeps later queued blocks and future messages, plays the two-tap stop
confirmation, and is never transcribed or sent as chat.
The local matcher tolerates narrow transcription variants such as
`computer`/`computers`; other wake-phrase input keeps the normal
dictate-and-send flow.

Ordinary **"okay computer"** is a conversational pause, not a permanent stop.
Parley pauses the current response at its present position, gives the
microphone an exclusive turn, and resumes the same response after the dictated
message is sent or cancelled. Any newly queued speech waits silently behind
that microphone turn. A possible wake utterance reserves the microphone from
its first voiced frame, so a reply finishing synthesis cannot begin halfway
through the phrase before local recognition catches up. Non-wake speech
releases that provisional reservation immediately. The explicit stop-talking
command skips only the block currently being synthesized or played.

Wake and send cues remain tiny bundled files rather than live AI generations.
ElevenLabs' [sound-effects API](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert)
is useful for design-time exploration, but its minimum generated duration is
0.5 seconds and [each call is metered](https://elevenlabs.io/pricing/api).
Runtime generation would make a local control cue slower, network-dependent,
and billable without improving its reliability.

### Why it doesn't over-trigger, or cost much

Most always-on voice modes either fire constantly or stream your room to a
paid API. This runs three layers, cheapest first:

1. **An energy gate** over raw microphone frames. No model, no network. Silence
   costs nothing, which is the whole reason always-on is affordable.
2. **Local trigger detection.** A tiny Whisper recognizes control phrases and,
   after optional enrollment, a speaker-personalized acoustic matcher provides
   a second path when transcription gets the words wrong. Neither leaves the
   machine.
3. **The accurate cloud model**, once, on the message you actually dictated.

So the recurring cost is a fraction of a cent per message you send, and zero
while you are quiet or merely talking to someone else in the room.

The guardrails are the point. A wake phrase is required before capture begins,
an explicit send phrase is required before anything is submitted, and capture
expires by itself after two minutes. During playback, overlapping audio may be
examined locally for a wake phrase so you can interrupt naturally; it is never
submitted unless that wake phrase opens a microphone turn and you later use the
send command. Pick a wake phrase you would not use in ordinary conversation.

### What it needs from you

The microphone permission is granted to your **terminal application**, not to
this tool, and it applies to everything that runs in that terminal afterwards.
macOS will prompt the first time. Revoke it in System Settings under Privacy
and Security, Microphone.

Input is typed with `tmux send-keys` against the pane you armed it from, so
the session must be running inside tmux. That is also why this is agent
agnostic: it types into the session the same way you would.

### The Claude Code status band

The band is a Claude Code mod that ships inside Parley
(`src/parley/claude_mod/`). It stays quiet while Parley waits and turns vivid
only while the microphone is open:

```text
○ parley  say okay computer                      waiting for the wake phrase
● parley listening 0:04  send it sends           capturing; the dot breathes
◑ parley sending                                  transcribing and submitting
♪ parley speaking  stop talking skips            reading a reply aloud
↻ parley finding a microphone                    no usable macOS input yet
○ parley  say okay computer  ⚠ dictation goes to windy-falcon
```

Colors come from the Claude Code theme (red while listening, amber while
sending, blue while speaking, grey at idle), so light and dark themes both
hold. Two other styles are in `/config` under the plugin's options: `chip`, a
filled pill in the state's color, and `glyph`, the mark and timer alone.

It needs no tmux status bar, so it keeps working where a statusline or Agent
Deck hides that bar, and while it is drawn the tmux badge stands aside for its
pane so the state never shows twice (the band keeps a marker in
`~/.parley/bands/` fresh; Codex and other panes keep the badge). It updates
within about a quarter second of a wake, send or reply: rather than run Parley
on a timer, it stats the few files Parley rewrites on every transition and asks
`parley indicator --json` again only when one changes, with a four-second
heartbeat for a listener that died without cleaning up. An idle band never
redraws.

`parley install` adds it with `claude plugin install parley --marketplace
<parley's claude_mod folder>`, which Claude Code reads in place, so upgrading
Parley upgrades the band. To add or remove it by hand:

```sh
parley install --harness claude-code      # prints the claude command if it cannot run it
claude plugin uninstall parley@parley     # parley uninstall does this too
```

New Claude Code sessions pick it up; in a running one, `/reload-plugins`.
Within fifteen seconds of Claude exiting (at once on a clean exit) the pane's
tmux badge returns.

## Configuration

Every knob is an environment variable.

| Variable | Default | Notes |
|---|---|---|
| `PARLEY_TTS_PROVIDER` | `auto` | `auto`, `openai`, `elevenlabs`, or `macos`; auto falls through in that quality order |
| `PARLEY_VOICE` | `fable` | one of the eleven OpenAI voices |
| `PARLEY_OPENAI_FALLBACK_VOICE` | `onyx` | OpenAI voice used after ElevenLabs fails |
| `PARLEY_MACOS_VOICE` | Eddy (US) | zero-key local fallback voice |
| `PARLEY_MACOS_RATE` | `210` | local words per minute |
| `PARLEY_MODEL` | `gpt-4o-mini-tts-2025-12-15` | falls back if retired |
| `PARLEY_ELEVENLABS_VOICE_ID` | George | ElevenLabs voice ID; `parley voices` lists yours |
| `PARLEY_ELEVENLABS_MODEL` | `eleven_v3_conversational` | expressive, low-latency ElevenLabs text-to-speech model |
| `PARLEY_ELEVENLABS_KEYCHAIN_SERVICE` | `parley-elevenlabs-api-key` | generic-password service used for secure key lookup |
| `PARLEY_ELEVENLABS_KEYCHAIN_ACCOUNT` | current macOS user | generic-password account used for secure key lookup |
| `PARLEY_SPEED` | `1.2` | OpenAI speech speed |
| `PARLEY_INSTRUCTIONS` | conversational | delivery notes for `gpt-*` models |
| `PARLEY_MAX_CHARS` | `3000` | maximum characters per TTS request; longer replies are sentence-aware chunks |
| `PARLEY_SESSION_NAME` | tmux session name | spoken when automatic output switches sessions |
| `PARLEY_ENV` | — | extra file to read the API key from |
| `PARLEY_WAKE` | `okay computer` | phrase that starts capture |
| `PARLEY_SEND` | `send it` | phrase that submits |
| `PARLEY_STOP_TALKING` | `stop talking` | local interrupt used after the wake phrase during playback |
| `PARLEY_MIC` | `default` | macOS default input; a name/index pins one device |
| `PARLEY_MIC_THRESHOLD` | `500` | raise it in a noisy room |
| `PARLEY_MIN_SPEECH` | `0.10` | minimum voiced seconds retained for trigger detection |
| `PARLEY_STT_MODEL` | `gpt-4o-transcribe` | cloud model when an OpenAI key exists |
| `PARLEY_LOCAL_STT_MODEL` | `ggml-base.en.bin` | zero-key local message model, downloaded on first use |
| `PARLEY_STATE` | `~/.parley` | queue, logs, session flags |

## How it works

A skill, a hook, and a queue.

The **skill** carries one line telling the model its replies are spoken. It is
a skill rather than a session-start hook because voice gets switched on and off
mid-session, and only the sessions that asked for it should carry the
instruction. That line is the whole prompt — no summarizer, no markdown
stripper. Asking for speakable prose up front beats repairing prose afterwards.

The **hook** fires when a reply finishes. Agents report that reply differently:
Codex hands over the text directly, Claude Code hands over a path to a
transcript. Both shapes are read, so one handler serves either.

Finding *which* reply is subtler than it looks in the transcript case. The hook
can fire before the reply is flushed, and the previous turn's reply looks
equally new. So the reply is anchored to the last real user message — tool
results are recorded as user entries and are excluded — and the transcript is
allowed to settle before anything is read.

A session is keyed by its **terminal pane**, not by any agent's session id. The
pane is what voice actually addresses: it is where the reply is spoken and
where a dictated message is typed back. It also means this works under an agent
that exposes no session id at all.

Utterances become files in a spool directory named by nanosecond timestamp.
Any process may enqueue; exactly one drains, chosen by an exclusive `flock`.
That is what makes overlap impossible rather than merely unlikely.

Automatic replies carry their stable pane identity through that queue. Parley
remembers the session of the last successfully played block and announces a
name only when the next block belongs to a different session. If sessions
interleave, each switch is announced; consecutive blocks and replies from one
session are not.

Playback starts with six-tenths of a second of silence, because Bluetooth
headphones take about that long to switch profiles and will otherwise swallow
your first few words. The warm-up mitigates profile-switch latency; it cannot
make an unavailable AirPods route usable before macOS has connected it.

`~/.parley/speak.log` records what was spoken, in which voice, and how
long synthesis took. It records message lengths and operational events, not
the text you dictate or the text spoken back.

## Privacy

Parley has no analytics, account system, or Parley-operated server. The energy
gate, trigger recognition, personalized matching, cue playback, and session
routing stay on your Mac. Before the wake phrase, speech bursts may be examined
by the bundled local trigger recognizer but are not retained.

When you explicitly send a dictated message, its captured audio is sent to
OpenAI for transcription. When speech output is enabled, the reply text is sent
to the configured speech provider: ElevenLabs in `auto` mode when its key is
available, otherwise OpenAI. Their respective data policies apply. Temporary
audio, queue items, logs, and personalized trigger features live under
`~/.parley`; Parley creates sensitive state with user-only permissions. Raw
enrollment recordings are never saved.

See [PRIVACY.md](PRIVACY.md) for the complete data-flow and deletion details.

## Contributing

Bug reports and focused pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md), and report vulnerabilities through GitHub's
private vulnerability reporting rather than a public issue.

## License

MIT
