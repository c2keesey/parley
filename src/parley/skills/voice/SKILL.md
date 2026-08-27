---
name: voice
description: Turn two-way voice on or off for this session — replies read aloud, and hands-free dictation back. Use when the user says "voice on", "voice off", "talk to me", "read that aloud", "turn on voice mode", "stop talking", or invokes /voice.
---

Run `parley on` to start, `parley off` to stop. `parley status` reports the
current state. Say which you did, then continue with the work.

While voice is on:

- Your reply is read aloud after Parley announces this session's name. Answer
  in a few sentences of plain speech — no markdown, code, or file paths.
- Never use AskUserQuestion. It blocks on a dialog that voice cannot answer;
  ask in your reply instead.
- Use `parley say "..."` to speak during a long task, so the user is not left
  in silence. It queues behind whatever is already speaking.

For hands-free input, `parley listen on` from inside tmux. The user says
"okay computer", speaks, then "send it", and the message is typed into the
session. "scratch that" or "scrap that" as a standalone utterance discards a
message in progress. A persistent tmux badge
shows when listening is live and which session will receive the message.

While speech is playing, "okay computer, stop talking" is a local interrupt:
it stops playback and is never sent as chat. Other wake-phrase input keeps the
normal dictate-and-send flow.

Parley automatically uses ElevenLabs speech when its key is in macOS Keychain
or `ELEVENLABS_API_KEY` is configured. `parley voices` lists available voice
IDs; George is the default. Never ask the user to paste an API key into chat.
