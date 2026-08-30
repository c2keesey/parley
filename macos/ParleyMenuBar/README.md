# ParleyMenuBar

A native SwiftUI control surface over the existing Parley CLI.

## Architecture

- `ParleyCore` decodes the versioned local JSON snapshot, derives one of seven
  visual states, and maps enabled controls to existing CLI arguments.
- `ParleyMenuBar` owns the `MenuBarExtra`, status window, one-second polling,
  keyboard shortcuts, and VoiceOver descriptions.
- `ParleyCLIClient` launches only the `parley` executable. Status has a
  three-second timeout, control commands have bounded timeouts, and stdout is
  decoded as data rather than evaluated as shell input.
- Target-bound controls pass only `TMUX_PANE` to the child process. The app
  neither imports Python code nor discovers speech-provider credentials.

The process boundary is intentionally narrow. `parley status --json` returns
only process liveness, listener state, speech activity, target identity, and
per-target voice state. Existing human-readable CLI output is unchanged.

## Development build and test

```sh
swift run --package-path macos/ParleyMenuBar ParleyCoreTests
swift build --package-path macos/ParleyMenuBar
macos/ParleyMenuBar/Scripts/build-app.sh
```

The test runner is an executable target because Apple's standalone Command Line
Tools distribution on the development machine does not include XCTest or the
Swift Testing module. It exits nonzero on the first failed status or command
assertion and requires no third-party package.

`build-app.sh` still creates an ad-hoc-signed, host-architecture accessory
bundle at `.build/ParleyMenuBar.app`. It now derives both bundle version fields
from the one canonical source, `pyproject.toml`, and verifies the plist,
architecture, and signature before returning. For checkout testing, point the
bundle at the local CLI before launch:

```sh
PARLEY_CLI="$PWD/.venv/bin/parley" \
  macos/ParleyMenuBar/.build/ParleyMenuBar.app/Contents/MacOS/ParleyMenuBar
```

## Release contract

The Python distribution and native app ship with the same semantic version.
`pyproject.toml` is the only editable version source: Python reads the installed
distribution metadata, and both app plist version fields are rendered from it.
The builder requires the requested version to match and refuses a dirty Git
checkout.

Runtime compatibility is governed by `parley status --json`'s integer
`contract_version`, not by patch-version equality. The native app rejects an
unknown contract version before enabling controls. `cli_version` is included
for diagnostics; additions to an existing contract must remain optional or a
new contract version is required. Release artifacts record both the package
version and contract version in their JSON manifest.

The supported release target is a universal app containing `arm64` and
`x86_64`, with a minimum deployment target of macOS 13. The canonical artifact
name is `ParleyMenuBar-VERSION-macos-universal2.zip`, accompanied by a
`.zip.sha256` file and JSON manifest. This is normalized, reproducible-input
packaging: the ZIP writer fixes entry order and timestamps to the commit time
(or `SOURCE_DATE_EPOCH`) and the verifier restores and checks recorded modes.
It is not a claim that signed output is bit-for-bit reproducible. Developer ID
timestamp signatures and Apple's notarization ticket are intentionally
nondeterministic.

### Credential-free local release validation

Commit the intended inputs first, then build an explicitly marked ad-hoc
artifact. It cannot be mistaken for a distributable release and cannot be
notarized:

```sh
version=$(macos/ParleyMenuBar/Scripts/release.py version)
macos/ParleyMenuBar/Scripts/release.py build \
  --version "$version" --ad-hoc
macos/ParleyMenuBar/Scripts/release.py verify \
  --artifact ".artifacts/release/ParleyMenuBar-$version-macos-universal2-adhoc.zip" \
  --checksum ".artifacts/release/ParleyMenuBar-$version-macos-universal2-adhoc.zip.sha256" \
  --version "$version" --architectures arm64 x86_64 --signature ad-hoc \
  --smoke-launch
```

`--smoke-launch` directly starts the archived executable, confirms it remains
running, and terminates it; it does not install the app. Ad-hoc artifacts are
expected to fail Gatekeeper because they have no trusted Developer ID.

### Developer ID signing and notarization

Signing uses an identity already available to `codesign`; the builder never
imports certificates or changes Keychain. Set the non-secret identity selector
in the environment or pass it explicitly:

```sh
export PARLEY_SIGNING_IDENTITY='Developer ID Application: Example (TEAMID)'
macos/ParleyMenuBar/Scripts/release.py build \
  --version "$version" --notarize
```

Notarization accepts exactly one of these input sets, and commands never print
their arguments:

- `PARLEY_NOTARY_KEYCHAIN_PROFILE`, optionally
  `PARLEY_NOTARY_KEYCHAIN`, for a profile created outside this workflow. This
  is the preferred route.
- `PARLEY_NOTARY_KEY`, `PARLEY_NOTARY_KEY_ID`, and
  `PARLEY_NOTARY_ISSUER_ID` for an App Store Connect API key file.

Raw Apple-ID passwords are deliberately unsupported: accepting one would put
it in `notarytool`'s process arguments. If Apple-ID authentication is needed,
store it in a `notarytool` keychain profile outside this builder and pass only
the profile name.

In CI, map protected secret values directly to those environment names, keep
the Developer ID certificate/key in an ephemeral job keychain, and retain only
the ZIP, checksum, and manifest. The builder submits with `notarytool --wait`,
staples the accepted ticket, validates it, verifies the signature and exact
architectures, and runs `spctl`. It never publishes or uploads a release.

### Install, update, rollback, and uninstall

After independently checking the `.sha256` file, unzip the app and move it to
`~/Applications/ParleyMenuBar.app` (per-user) or `/Applications` (managed
system-wide install). Install the manifest's exact CLI distribution version
with `uv tool install 'parley-voice==VERSION'`; source installs remain the
supported public path until the first package release. Launch the app directly
once so Gatekeeper can assess the stapled ticket. No installer may edit shell
configuration.

For an update, retain the prior ZIP and wheel/source revision, stop the app,
update the CLI and app to the same version, then launch and confirm the displayed
CLI version. Roll back both components together from the retained artifacts if
the status contract is rejected. To uninstall, quit and remove only the app and
run `uv tool uninstall parley-voice`. User settings, enrollment data, queues,
and logs under `${PARLEY_STATE:-~/.parley}` are preserved by default; remove
that directory only as a separate, explicit data-deletion action.

Clean-machine installation, real Developer ID signing, Apple notarization, and
Gatekeeper's first-launch UI must be validated on release hardware before this
process is considered fully accepted.
