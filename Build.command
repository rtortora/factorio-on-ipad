#!/bin/bash
# Double-click to open the build window; it builds on first run, using only the Swift that comes with Xcode.
set -eu
cd "$(dirname "$0")"
app=build/Installer.app
source=tools/installer/Installer.swift
if [ ! -x "$app/Contents/MacOS/Installer" ] || [ "$source" -nt "$app/Contents/MacOS/Installer" ]; then
    echo 'Compiling the build window...'
    mkdir -p "$app/Contents/MacOS"
    swiftc -O -parse-as-library -swift-version 5 "$source" -o "$app/Contents/MacOS/Installer" || {
        echo 'Compiling failed. The full Xcode must be installed and selected (see README).' >&2
        exit 1
    }
    cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>Installer</string>
<key>CFBundleIdentifier</key><string>local.factorio.installer</string>
<key>CFBundleName</key><string>Factorio for iPad</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>RepoRoot</key><string>$PWD</string>
</dict></plist>
PLIST
fi
open "$app"
