# parley

Two-way voice for terminal coding agents. Works with
[Claude Code](https://claude.com/claude-code) and
[Codex](https://developers.openai.com/codex/cli).

Both ship voice *input* of some kind. Neither speaks back. This adds the other
half: replies are read aloud as they land, the agent can talk to you while it
is still working, and you can answer it hands-free.

Nothing in parley is specific to one agent. A session is identified by its
terminal pane, so anything that runs in a pane and fires a Stop-style hook is
supported — the two above use the same hooks schema, and `parley install`
wires up whichever it finds.

## What it does

- **Reads each reply aloud** as soon as it is finished, in a natural voice.
- **Tells the model it is being listened to.** The `voice` skill carries one
  line of context, so the agent writes speakable prose instead of markdown and
  file paths. Nothing is post-processed — what it writes is what you hear.
- **Lets the agent speak mid-task.** `parley say "..."` queues a line from
  anywhere, so a long job can narrate itself instead of going silent.
- **Listens back.** Say a wake phrase, talk, say a send phrase, and the message
  is typed into the session. Layered so always-on listening stays cheap.
- **Never overlaps.** Everything goes through one queue drained by a single
  player. Replies, status updates and voice samples all take turns.
- **Names the speaker.** Every automatic reply starts with its session name,
  so several voice-enabled agents can share one audio queue without ambiguity.
- **Per-session.** One session can speak while the rest stay quiet.

## Install

```sh
uv tool install parley      # or: pipx install parley
parley install              # wires up every agent it finds
```

`parley install` writes a `Stop` hook and the `voice` skill into each agent
present on the machine:

| Agent | Hook | Skill |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | `~/.claude/skills/voice/` |
| Codex | `~/.codex/hooks.json` | `~/.codex/skills/voice/` |

It is idempotent, leaves any hooks you already have alone, and backs up the
settings file before writing. Pass `--harness claude-code` or `--harness codex`
to target one. To undo it:

```sh
parley uninstall            # optionally --harness <name>
```

With the skill installed you can just say "voice on" or "voice off" to the
agent, in either tool, instead of running the command yourself.

For OpenAI speech or hands-free transcription, provide `OPENAI_API_KEY` in the
environment or in `~/.config/parley/env`.

For higher-quality speech, store an ElevenLabs key in macOS Keychain under
service `parley-elevenlabs-api-key` and your macOS account name. You can also
set it in the environment or the shared env file. In the default `auto` mode,
its presence switches speech output to ElevenLabs while OpenAI remains
available for hands-free transcription:

```sh
ELEVENLABS_API_KEY=...
OPENAI_API_KEY=sk-...        # still needed only if you use voice input
```

Parley starts with ElevenLabs' George voice and `eleven_v3`. Run
`parley voices` after adding the key to see the voices in your account, then
set `PARLEY_ELEVENLABS_VOICE_ID` to the one you want. Set
`PARLEY_TTS_PROVIDER=openai` at any time to keep using OpenAI for speech.

macOS only for now — playback uses `afplay`. `ffmpeg` is optional; without it
you lose only the Bluetooth warm-up described below.

## Use

```sh
parley on                 # speak replies in this session
parley off                # stop
parley status

parley say "Tests are green, deploying now."
parley say --voice nova "Heads up, that migration looks wrong."
parley say --wait "..."   # block until spoken

parley voices             # audition OpenAI voices, or list ElevenLabs voices
parley stop               # drop the queue, silence everything
parley default on         # new sessions start speaking
```

Turning it on mid-session works without a restart: the confirmation it prints
is the same instruction the startup hook injects, so it lands in the transcript
as context.

## Hands-free input

Voice output alone still leaves you typing. `parley listen` closes the
loop: say the wake phrase, dictate, say the send phrase, and the message is
typed into your agent's pane and submitted.

While listening is active, every tmux session shows a high-contrast persistent
`🎙 PARLEY LISTENING → <target>` badge. The badge checks the listener process
on every status refresh, disappears if it stops or crashes, and follows the
pane that will receive dictated messages.

```sh
brew install whisper-cpp     # one-time
parley listen on       # from inside the tmux pane running the agent
parley listen status
parley listen off
```

Defaults: say **"okay computer"**, talk, then **"send it"**. A quiet low tone
confirms it woke, a soft two-note cue confirms it sent, and a falling tone
means capture expired. Say **"scratch that"** or **"scrap that"** as a
standalone utterance to discard the capture locally. The narrow matcher does
not discard ordinary dictation such as "scratch that migration." Change the
wake, send, and original cancel phrases with `PARLEY_WAKE`, `PARLEY_SEND`, and
`PARLEY_CANCEL`.

The local recognizer is biased toward these control phrases and accepts the
demonstrated standalone tiny-Whisper rendering `Sunday` as `send it`; the
fallback never matches `Sunday` inside ordinary dictation.

If you say **"okay computer"** again while dictating, the wake tone plays again
as an audible confirmation that capture is still active. Parley keeps
listening and removes the repeated wake phrase from the message it sends.

The cue meanings are deliberately distinct: one warm tone means capture is
open, a soft rising pair means the message was sent, one quiet dot means speech
playback finished, and a darker falling pair means capture was discarded or
expired. Each played cue is logged by name and source in `~/.parley/speak.log`.

While Parley is speaking, **"okay computer, stop talking"** is a dedicated
local voice-control command. It immediately silences playback, clears queued
speech, makes no confirmation sound, and is never transcribed or sent as chat.
The local matcher tolerates narrow transcription variants such as
`computer`/`computers`; other wake-phrase input keeps the normal
dictate-and-send flow.

Ordinary **"okay computer"** is a conversational pause, not a permanent stop.
Parley pauses the current response at its present position, gives the
microphone an exclusive turn, and resumes the same response after the dictated
message is sent or cancelled. Any newly queued speech waits silently behind
that microphone turn. Only the explicit stop-talking command discards speech.

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
2. **A tiny local whisper**, run on each burst of speech, looking only for the
   wake or send phrase. It runs when you talk, not when the clock ticks, and
   nothing leaves the machine.
3. **The accurate cloud model**, once, on the message you actually dictated.

So the recurring cost is a fraction of a cent per message you send, and zero
while you are quiet or merely talking to someone else in the room.

The guardrails are the point. A wake phrase is required before any audio is
kept, an explicit send phrase is required before anything is submitted, capture
expires by itself after two minutes, and the microphone is ignored entirely
while Claude is speaking — so it can never wake itself up on its own voice.
Pick a wake phrase you would not say while talking *about* Claude.

### What it needs from you

The microphone permission is granted to your **terminal application**, not to
this tool, and it applies to everything that runs in that terminal afterwards.
macOS will prompt the first time. Revoke it in System Settings under Privacy
and Security, Microphone.

Input is typed with `tmux send-keys` against the pane you armed it from, so
the session must be running inside tmux. That is also why this is agent
agnostic: it types into the session the same way you would.

## Configuration

Every knob is an environment variable.

| Variable | Default | Notes |
|---|---|---|
| `PARLEY_TTS_PROVIDER` | `auto` | `auto`, `openai`, or `elevenlabs`; auto prefers ElevenLabs when its key exists |
| `PARLEY_VOICE` | `fable` | one of the eleven OpenAI voices |
| `PARLEY_MODEL` | `gpt-4o-mini-tts-2025-12-15` | falls back if retired |
| `PARLEY_ELEVENLABS_VOICE_ID` | George | ElevenLabs voice ID; `parley voices` lists yours |
| `PARLEY_ELEVENLABS_MODEL` | `eleven_v3` | ElevenLabs text-to-speech model |
| `PARLEY_ELEVENLABS_KEYCHAIN_SERVICE` | `parley-elevenlabs-api-key` | generic-password service used for secure key lookup |
| `PARLEY_ELEVENLABS_KEYCHAIN_ACCOUNT` | current macOS user | generic-password account used for secure key lookup |
| `PARLEY_SPEED` | `1.2` | OpenAI speech speed |
| `PARLEY_INSTRUCTIONS` | conversational | delivery notes for `gpt-*` models |
| `PARLEY_MAX_CHARS` | `3000` | caps a single utterance |
| `PARLEY_SESSION_NAME` | tmux session name | spoken before every automatic reply |
| `PARLEY_ENV` | — | extra file to read the API key from |
| `PARLEY_WAKE` | `okay computer` | phrase that starts capture |
| `PARLEY_SEND` | `send it` | phrase that submits |
| `PARLEY_STOP_TALKING` | `stop talking` | local interrupt used after the wake phrase during playback |
| `PARLEY_MIC` | `0` | avfoundation input device index |
| `PARLEY_MIC_THRESHOLD` | `500` | raise it in a noisy room |
| `PARLEY_STT_MODEL` | `gpt-4o-transcribe` | transcribes the dictated message |
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

Playback starts with six-tenths of a second of silence, because Bluetooth
headphones take about that long to switch profiles and will otherwise swallow
your first few words.

`~/.parley/speak.log` records what was spoken, in which voice, and how
long synthesis took.

## License

MIT
