#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
package_dir=$(dirname -- "$script_dir")
app_dir="$package_dir/.build/ParleyMenuBar.app"
contents_dir="$app_dir/Contents"

swift build --package-path "$package_dir" -c release
binary_dir=$(swift build --package-path "$package_dir" -c release --show-bin-path)

mkdir -p "$contents_dir/MacOS" "$contents_dir/Resources"
cp -f "$binary_dir/ParleyMenuBar" "$contents_dir/MacOS/ParleyMenuBar"
cp -f "$package_dir/Resources/Info.plist" "$contents_dir/Info.plist"
chmod 755 "$contents_dir/MacOS/ParleyMenuBar"
codesign --force --sign - --timestamp=none "$app_dir"

echo "$app_dir"
