#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$(dirname -- "$(dirname -- "$script_dir")")")
release="$script_dir/release.py"
version=$(uv run --project "$repo_dir" python "$release" version)

uv run --project "$repo_dir" python "$release" version --expect "$version" >/dev/null

if uv run --project "$repo_dir" python "$release" version --expect 999.0.0 >/dev/null 2>&1; then
  echo "expected mismatched version to fail" >&2
  exit 1
fi

if uv run --project "$repo_dir" python "$release" build --version "$version" >/dev/null 2>&1; then
  echo "expected missing signing mode to fail" >&2
  exit 1
fi

if uv run --project "$repo_dir" python "$release" build \
  --version "$version" --ad-hoc --notarize >/dev/null 2>&1; then
  echo "expected ad-hoc notarization to fail" >&2
  exit 1
fi

echo "release CLI failure-path tests passed"
