# claude-speak

Voice output for [Claude Code](https://claude.com/claude-code).

Claude Code ships voice *input* — `/voice` gives you push-to-talk dictation.
There is no voice output. This adds it: replies are read aloud as they land,
and Claude can talk to you while it is still working.

Pair it with dictation and the loop closes. You talk, it talks back, and you
never have to look at the terminal.

## What it does

- **Reads each reply aloud** as soon as it is finished, in a natural voice.
- **Tells the model it is being listened to.** A `SessionStart` hook injects one
  line of context, so Claude writes speakable prose instead of markdown and
  file paths. Nothing is post-processed — what it writes is what you hear.
- **Lets Claude speak mid-task.** `claude-speak say "..."` queues a line from
  anywhere, so a long job can narrate itself instead of going silent.
- **Never overlaps.** Everything goes through one queue drained by a single
  player. Replies, status updates and voice samples all take turns.
- **Per-session.** One session can speak while the rest stay quiet.

## Install

```sh
uv tool install claude-speak      # or: pipx install claude-speak
claude-speak install              # wires the hooks into ~/.claude/settings.json
```

`claude-speak install` is idempotent and leaves any hooks you already have
alone. It backs up `settings.json` before writing. To undo it:

```sh
claude-speak uninstall
```

You need an OpenAI API key, either in `OPENAI_API_KEY` or in a file at
`~/.config/claude-speak/env` containing `OPENAI_API_KEY=sk-...`.

macOS only for now — playback uses `afplay`. `ffmpeg` is optional; without it
you lose only the Bluetooth warm-up described below.

## Use

```sh
claude-speak on                 # speak replies in this session
claude-speak off                # stop
claude-speak status

claude-speak say "Tests are green, deploying now."
claude-speak say --voice nova "Heads up, that migration looks wrong."
claude-speak say --wait "..."   # block until spoken

claude-speak voices             # audition all eleven voices
claude-speak stop               # drop the queue, silence everything
claude-speak default on         # new sessions start speaking
```

Turning it on mid-session works without a restart: the confirmation it prints
is the same instruction the startup hook injects, so it lands in the transcript
as context.

## Configuration

Every knob is an environment variable.

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_SPEAK_VOICE` | `fable` | one of the eleven OpenAI voices |
| `CLAUDE_SPEAK_MODEL` | `gpt-4o-mini-tts-2025-12-15` | falls back if retired |
| `CLAUDE_SPEAK_SPEED` | `1.2` | |
| `CLAUDE_SPEAK_INSTRUCTIONS` | conversational | delivery notes for `gpt-*` models |
| `CLAUDE_SPEAK_MAX_CHARS` | `3000` | caps a single utterance |
| `CLAUDE_SPEAK_ENV` | — | extra file to read the API key from |
| `CLAUDE_SPEAK_STATE` | `~/.claude/speak` | queue, logs, session flags |

## How it works

Two hooks and a queue.

`SessionStart` injects one line telling the model its replies are spoken.
That is the whole prompt — no summarizer, no markdown stripper. Asking for
speakable prose up front beats repairing prose after the fact.

`Stop` fires when a reply finishes. Finding *which* reply is subtler than it
looks: the hook can fire before the reply is flushed to the transcript, and the
previous turn's reply looks equally new. So the reply is anchored to the last
real user message — tool results are recorded as user entries and are excluded
— and the transcript is allowed to settle before anything is read.

Utterances become files in a spool directory named by nanosecond timestamp.
Any process may enqueue; exactly one drains, chosen by an exclusive `flock`.
That is what makes overlap impossible rather than merely unlikely.

Playback starts with six-tenths of a second of silence, because Bluetooth
headphones take about that long to switch profiles and will otherwise swallow
your first few words.

`~/.claude/speak/speak.log` records what was spoken, in which voice, and how
long synthesis took.

## License

MIT
