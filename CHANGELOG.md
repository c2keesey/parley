# Changelog

All notable user-facing changes are documented here. Parley follows semantic
versioning while it is in beta.

## 0.6.0 - 2026-09-04

- Made `parley on` start the complete two-way experience: spoken replies plus
  hands-free listening routed to the invoking tmux pane.
- Made `parley off` stop that pane's listener without disturbing a listener
  owned by another session.
- Added `parley speak on|off|status` for intentional output-only use, while
  retaining `parley listen` as the explicit low-level input control.
- Made hands-free routing follow the Agent Deck session displayed by the most
  recently active human terminal client, ignoring background control clients
  and rechecking focus immediately before submission.
- Follow the macOS default input instead of volatile AVFoundation device zero,
  and keep the listener alive while input devices disconnect or change.
- Detect default-output changes during active speech and resume the unheard
  portion on the new route; retry unexpected `afplay` failures instead of
  silently treating them as successful playback.
- Announce the speaking session only when output switches sessions, avoiding
  repeated names across consecutive replies from the same session.

## 0.5.4 - 2026-08-27

- Switched George's default ElevenLabs model to v3 Conversational for lower
  latency and half the per-character API cost of regular v3.

## 0.5.3 - 2026-08-27

- Kept spoken replies free of URLs, hyperlinks, and citation links so speech
  output does not read link text aloud.

## 0.5.2 - 2026-08-27

- Added automatic OpenAI speech fallback when ElevenLabs is temporarily
  unavailable or out of quota, with periodic retries for the preferred voice.
- Added zero-key macOS speech and local full-message Whisper transcription as
  the final fallback, so Parley remains functional without API credentials.

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
