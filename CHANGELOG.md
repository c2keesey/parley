# Changelog

All notable user-facing changes are documented here. Parley follows semantic
versioning while it is in beta.

## 0.5.1 - 2026-08-27

- Fixed personalized trigger enrollment restarting hands-free input on a stale
  session instead of the tmux pane where enrollment was run.
- Renamed the bundled agent skill from `voice` to `parley`.

## 0.5.0 - 2026-08-26

- Added optional private, local personalized trigger enrollment.
- Added distinct ready, listening, sending, and speaking indicators.
- Made wake, send, cancel, and stop-talking controls resilient to realistic
  transcription variants and fast utterances.
- Added pause-and-resume barge-in while keeping explicit stop permanent.
- Added ElevenLabs speech output with George as the default voice.
- Added named, documented audio cues and safe cue logging.

## 0.4.0 - 2026-08-24

- Added hands-free wake, dictate, send, and cancel controls.
- Added multi-session speech labels and exclusive queued playback.
- Added Claude Code and Codex installation support.
