# Privacy

Parley is local-first and has no telemetry, analytics, account system, or
Parley-operated backend.

## What stays on the Mac

- The always-on energy gate and trigger-phrase recognition.
- Personalized trigger matching. Enrollment keeps raw recordings in memory and
  saves only normalized acoustic feature arrays under `~/.parley/triggers`.
- Session routing, notification cues, and playback coordination.
- Operational logs. Logs contain timestamps, cue names, sanitized provider and
  failure-stage fields, timing, byte counts, and message lengths—not dictated
  or spoken message text or provider response bodies.
- The last speech failure marker. It contains only an allow-listed provider,
  synthesis/playback stage, and the fixed retry/drop policy so local status can
  remain actionable without retaining response text or provider diagnostics.

Before a wake phrase is accepted, short speech bursts may be processed by the
local recognizer and are then discarded. Once capture begins, audio is held in
memory until the turn is sent, cancelled, or expires.

## What leaves the Mac

- On the explicit send command, captured dictation audio is sent to OpenAI's
  transcription API. The returned text is typed into the selected terminal
  pane.
- For speech output, assistant reply text is sent to the configured provider.
  `auto` uses ElevenLabs when an ElevenLabs key is available and otherwise uses
  OpenAI.
- The first use of hands-free listening downloads the public whisper.cpp tiny
  English model from Hugging Face.

Parley does not proxy these requests. Your relationship and the provider's
terms and data policy govern the data sent directly to that provider.

## Local files and deletion

Runtime state is stored under `~/.parley` by default and sensitive files are
created with user-only permissions. The local whisper model is cached under
`~/.cache/parley`.

Run `parley listen off`, then delete those two directories to remove local
runtime state, logs, personalized trigger features, and the downloaded model.
`parley uninstall` removes agent hooks; it intentionally leaves runtime state
in place so uninstalling does not silently destroy user data.

API keys are read from the process environment, the optional
`~/.config/parley/env` file, or macOS Keychain for ElevenLabs. Parley never
writes API keys to its logs or state directory.
