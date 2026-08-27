# Contributing to Parley

Thanks for helping make hands-free agent work more reliable.

## Before opening a change

- Search existing issues first.
- Keep microphone privacy and local-only control semantics intact.
- Never add recorded speech, API keys, transcripts, or personal trigger
  profiles to fixtures or bug reports.
- For behavioral changes, include tests for both the intended phrase and nearby
  language that must not trigger.

## Development

Parley requires Python 3.11 or newer. The runtime is currently macOS-only, but
most tests do not access audio hardware.

```sh
git clone https://github.com/c2keesey/parley.git
cd parley
uv sync --dev
uv run pytest
uv run ruff check .
uv build
```

To test the local checkout without replacing your regular installation:

```sh
uv run parley --help
PARLEY_STATE="$(mktemp -d)" uv run parley status
```

Hardware tests can play audio, request microphone permission, call paid APIs,
or type into a tmux pane. Run them deliberately with test credentials and a
disposable terminal session; never include credentials or captured audio in an
issue.

## Pull requests

Explain the user-visible behavior, the failure mode being fixed, and the checks
you ran. Keep unrelated cleanup separate. By contributing, you agree that your
contribution is licensed under the repository's MIT license.
