# ParleyMenuBar

A native SwiftUI control surface over the existing Parley CLI.

## Architecture

- `ParleyCore` decodes the versioned local JSON snapshot, derives one of seven
  visual states, and maps enabled controls to existing CLI arguments.
- `ParleyMenuBar` owns the `MenuBarExtra`, status window, one-second polling,
  keyboard shortcuts, VoiceOver descriptions, and the native login-item
  lifecycle.
- `ParleyCLIClient` launches only the `parley` executable. Status has a
  three-second timeout, control commands have bounded timeouts, and stdout is
  decoded as data rather than evaluated as shell input.
- Target-bound controls pass only `TMUX_PANE` to the child process. The app
  neither imports Python code nor discovers speech-provider credentials.

The process boundary is intentionally narrow. `parley status --json` returns
only process liveness, listener state, speech activity, target identity, and
per-target voice state. Existing human-readable CLI output is unchanged.

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

## Launch at Login

Launch at Login is off until the user enables it. The toggle calls
`SMAppService.mainApp.register()` and `unregister()`; it never writes shell
configuration, cron entries, or LaunchAgent plists. The UI distinguishes the
system's not-registered, enabled, approval-required, and not-found states and
shows native registration errors without pretending the preference changed.

The ad-hoc development bundle is useful for exercising the UI and signature,
but macOS may decide that a development bundle's location or signature is not
eligible for registration. Parley reports that failure or unavailable state in
the menu and status window. Install and sign the app normally for distribution;
do not bypass ServiceManagement during development.

The lifecycle controls are deliberately independent: **Disable Voice** disables
spoken replies for the selected terminal target, stops current speech, and
clears queued speech; **Stop Listener** stops hands-free microphone routing;
**Stop Speech Now** clears current and queued playback without disabling future
voice; and **Quit Menu Bar App** closes only the native interface. Launch at
Login retains its current setting when the interface quits.
