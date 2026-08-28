---
name: parley
description: Turn Parley's two-way voice on or off for this session — replies read aloud, and hands-free dictation back. Use when the user says "voice on", "voice off", "Parley on", "Parley off", "talk to me", "read that aloud", "turn on voice mode", "stop talking", or invokes /parley.
---

Run `parley on` to start, `parley off` to stop. `parley status` reports the
current state. Say which you did, then continue with the work.

While voice is on:

- Your reply is read aloud after Parley announces this session's name. Answer
  in a few sentences of plain speech — no markdown, code, URLs, links,
  citations, or file paths. If attribution matters, name the source in plain
  speech without linking it.
- Never use AskUserQuestion. It blocks on a dialog that voice cannot answer;
  ask in your reply instead.
- Use `parley say "..."` to speak during a long task, so the user is not left
  in silence. It queues behind whatever is already speaking.

For hands-free input, `parley listen on` from inside tmux. The user says
"okay computer", speaks, then "send it", and the message is typed into the
session. "scratch that" or "scrap that" as a standalone utterance discards a
message in progress. A persistent tmux badge in Parley-enabled panes
distinguishes READY, LISTENING, SENDING, and SPEAKING · MIC READY, and names
the receiving session. Panes with Parley turned off show no badge.
Repeating "okay computer" during capture replays the wake tone, keeps capture
active, and removes that repeated wake phrase from the message.

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
