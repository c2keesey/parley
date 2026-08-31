# ParleyMenuBar

A native SwiftUI control surface over the existing Parley CLI.

## Architecture

- `ParleyCore` decodes the versioned local JSON snapshot, derives operational
  and microphone recovery states, and maps enabled controls to existing CLI
  arguments.
- `ParleyMenuBar` owns the `MenuBarExtra`, status window, one-second polling,
  keyboard shortcuts, and VoiceOver descriptions.
- `ParleyCLIClient` launches only the `parley` executable. Status has a
  three-second timeout, control commands have bounded timeouts, and stdout is
  decoded as data rather than evaluated as shell input.
- Target-bound controls pass only `TMUX_PANE` to the child process. The app
  neither imports Python code nor discovers speech-provider credentials.
- Microphone inventory comes from `parley mic devices --json`, which enumerates
  metadata without opening capture. Selecting a device explicitly starts the
  listener with its stable selector. The app never records diagnostic audio,
  changes permission, or displays raw capture-backend errors.

The process boundary is intentionally narrow. `parley status --json` returns
only process liveness, listener state, allow-listed microphone capture outcome,
selected device metadata, speech activity, target identity, and per-target
voice state. Capture readiness comes from fresh listener evidence, not a PID.

## Build and test

```sh
swift run --package-path macos/ParleyMenuBar ParleyCoreTests
swift build --package-path macos/ParleyMenuBar
macos/ParleyMenuBar/Scripts/build-app.sh
```

The test runner is an executable target because Apple's standalone Command Line
Tools distribution on the development machine does not include XCTest or the
Swift Testing module. It exits nonzero on the first failed status or command
assertion and requires no third-party package.

`build-app.sh` creates an ad-hoc-signed accessory bundle at
`.build/ParleyMenuBar.app`. For checkout testing, point the bundle at the local
CLI before launch:

```sh
PARLEY_CLI="$PWD/.venv/bin/parley" \
  macos/ParleyMenuBar/.build/ParleyMenuBar.app/Contents/MacOS/ParleyMenuBar
```
