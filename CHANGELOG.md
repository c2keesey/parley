# Changelog

All notable user-facing changes are documented here. Parley follows semantic
versioning while it is in beta.

## Unreleased

- Redesigned the Claude Code band: quiet at idle (a grey mark and the wake
  phrase), vivid while the microphone is open (a breathing red dot and an
  elapsed timer), with theme colors that hold in light and dark terminals and
  the phrase to say next in italic. `chip` and `glyph` styles are available
  in the plugin's options.
- The tmux badge now stands aside for a pane the Claude Code band covers, so
  a Claude session never shows the state twice; Codex and other panes keep it.
- Rebuilt the microphone gate. A fixed RMS 500 threshold only opened on the
  loudest syllables from a MacBook's built-in microphone (room noise there is
  about RMS 25), so most wake attempts were dropped as "short audio". The gate
  now learns the noise floor, uses WebRTC voice-activity detection, has
  hysteresis, and keeps 300 ms of pre-roll so onsets are not clipped ("send
  it" heard as "end it").
- Short bursts said during dictation are now part of the message instead of
  being discarded.
- The personalized matcher only adds recall; it no longer rejects bursts
  before Whisper sees them. It matches the wake phrase at the start of an
  utterance and send/cancel at its end, trims relative to the burst's own
  level, rejects implausible enrollment samples, and calibrates thresholds
  against the other phrases. Earlier profiles never matched and are ignored:
  run `parley enroll` again.
- Parley no longer wakes or stops itself when a reply says a trigger phrase.
- Dictated messages are pasted into the agent as one bracketed paste. Typing
  long messages key by key lost their beginning in Claude Code.
- When the cloud transcript drops the words just before "send it", the words
  local recognition heard there are restored.
- Added `parley listen meter`, a live microphone level meter, and
  `parley debug-triggers`, an offline hit/false-wake evaluation over recorded
  or synthetic bursts. Personalized near-misses are logged with their score
  and threshold.

- Added a Claude Code status band: a mod, shipped with Parley and installed by
  `parley install`, that shows the listener's live state (READY, LISTENING,
  SENDING, SPEAKING, RECONNECTING), where dictation goes and the next phrase
  to say, directly above the prompt. It updates within about a quarter second
  and does not depend on the tmux status bar being visible. The tmux badge
  remains for Codex and as the fallback.
- Added `parley indicator --json [pane]`, the state snapshot the badge and the
  band both draw from.
- `parley uninstall` removes the band as well.

## 0.6.0 - 2026-09-04

- Made `parley on` start the complete two-way experience: spoken replies plus
  hands-free listening routed to the invoking tmux pane.
- Made `parley off` stop that pane's listener without disturbing a listener
  owned by another session.
- Added `parley output on|off` and `parley listen on|off` as the explicit
  controls for either half of the experience. The earlier `parley speak`
  spelling remains as an undocumented compatibility alias.
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
