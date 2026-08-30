#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
package_dir=$(dirname -- "$script_dir")
app_dir="$package_dir/.build/ParleyMenuBar.app"
contents_dir="$app_dir/Contents"
version=$("$script_dir/release.py" version)

swift build --package-path "$package_dir" -c release
binary_dir=$(swift build --package-path "$package_dir" -c release --show-bin-path)

mkdir -p "$contents_dir/MacOS" "$contents_dir/Resources"
cp -f "$binary_dir/ParleyMenuBar" "$contents_dir/MacOS/ParleyMenuBar"
cp -f "$package_dir/Resources/Info.plist" "$contents_dir/Info.plist"
plutil -replace CFBundleShortVersionString -string "$version" "$contents_dir/Info.plist"
plutil -replace CFBundleVersion -string "$version" "$contents_dir/Info.plist"
chmod 755 "$contents_dir/MacOS/ParleyMenuBar"
codesign --force --sign - --timestamp=none "$app_dir"
"$script_dir/release.py" verify-app \
  --app "$app_dir" \
  --version "$version" \
  --architectures "$(uname -m)" \
  --signature ad-hoc

echo "$app_dir"
