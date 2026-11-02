---
name: parley
description: Turn Parley's two-way voice on or off for this session — replies read aloud, and hands-free dictation back. Use when the user says "voice on", "voice off", "Parley on", "Parley off", "talk to me", "read that aloud", "turn on voice mode", "stop talking", or invokes /parley.
---

Run `parley on` to start two-way voice for the current tmux pane: spoken
replies and hands-free listening. Run `parley off` to stop both for this pane.
Use `parley listen on` or `parley listen off` for input only, and `parley
output on` or `parley output off` for spoken output only. `parley status`
reports both states. Say which you did, then continue with the work. Do not run
`parley listen on` separately after `parley on`; use it only when the user
explicitly wants input without output or wants to reroute the listener.

While voice is on:

- Your reply is read aloud after Parley announces this session's name when
  output switches sessions. Consecutive replies from the same session omit the
  repeated name. Answer in a few sentences of plain speech — no markdown, code, URLs, links,
  citations, or file paths. If attribution matters, name the source in plain
  speech without linking it.
- Never use AskUserQuestion. It blocks on a dialog that voice cannot answer;
  ask in your reply instead.
- Use `parley say "..."` to speak during a long task, so the user is not left
  in silence. It queues behind whatever is already speaking.

For hands-free input, the user says "okay computer", speaks, then "send it",
and the message is typed into the session. "scratch that" or "scrap that" as
a standalone utterance discards a message in progress. A status indicator in
Parley-enabled sessions (in Claude Code, a band above the prompt, which
replaces the tmux badge for that pane; elsewhere, a tmux badge) distinguishes
ready, listening (with an elapsed timer in the band), sending, and speaking
with the mic still open, and warns when dictation would go to another
session. Reconnecting means Parley is waiting for a usable macOS input after
a device change. Panes with Parley turned off show no indicator.
In Agent Deck, the listener follows the session shown by the most recently
active human terminal client and rechecks it when sending; background control
clients cannot redirect dictation. In plain tmux it stays on the pane where
listening was started.
Repeating "okay computer" during capture replays the wake tone, keeps capture
active, and removes that repeated wake phrase from the message.

Parley follows the macOS default input and output as AirPods connect or
disconnect. It keeps the listener alive and retries input without changing the
target pane; never restart or retarget listening merely to repair an audio
device change. If input disappears during dictation, that partial turn is
discarded. Active speech resumes its unheard portion on a newly selected
default output.

If the user reports unreliable trigger recognition, check `parley listen
status`. Use `parley enroll` when they explicitly want personalized triggers;
it records guided local phrase samples, saves only private acoustic features,
and restarts an active listener on its previous target.

While speech is playing, "okay computer, stop talking" is a local interrupt:
it skips only the current spoken block, preserves later queued blocks and
future messages, plays a short two-tap confirmation tone, and is never sent as
chat.
Ordinary "okay computer" pauses the current response for an exclusive
dictation turn; speech resumes from that position after send or cancel, and
newly queued speech waits until the microphone turn ends. Parley provisionally
reserves the microphone from the first voiced frame while it checks for the
wake phrase, preventing a synthesized reply from starting mid-utterance.

Parley automatically uses ElevenLabs speech when its key is in macOS Keychain
or `ELEVENLABS_API_KEY` is configured. `parley voices` lists available voice
IDs; George is the default. Never ask the user to paste an API key into chat.
Long replies are split at sentence boundaries into bounded TTS requests and
played completely in order; "stop talking" skips only the active chunk.
