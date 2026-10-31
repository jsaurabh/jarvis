#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
APP_DIR="$PROJECT_DIR/dist/Jarvis.app"
CONTENTS_DIR="$APP_DIR/Contents"
MODULE_CACHE="$PROJECT_DIR/.build/ModuleCache"
ICONSET_DIR="$PROJECT_DIR/.build/Jarvis.iconset"
ASSETS_DIR="$PROJECT_DIR/macos/Assets"
SDK_PATH="/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk"

if [[ ! -d "$SDK_PATH" ]]; then
  SDK_PATH="$(xcrun --show-sdk-path)"
fi

mkdir -p "$CONTENTS_DIR/MacOS" "$CONTENTS_DIR/Resources" "$MODULE_CACHE"
mkdir -p "$ICONSET_DIR"
sips -z 16 16 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_16x16.png" >/dev/null
sips -z 32 32 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_32x32.png" >/dev/null
sips -z 64 64 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_128x128.png" >/dev/null
sips -z 256 256 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_256x256.png" >/dev/null
sips -z 512 512 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_512x512.png" >/dev/null
sips -z 1024 1024 "$ASSETS_DIR/JarvisAppIcon.png" --out "$ICONSET_DIR/icon_512x512@2x.png" >/dev/null
iconutil -c icns "$ICONSET_DIR" -o "$CONTENTS_DIR/Resources/Jarvis.icns"
cp "$ASSETS_DIR/JarvisAppIcon.png" "$CONTENTS_DIR/Resources/JarvisAppIcon.png"
cp "$ASSETS_DIR/JarvisMenuIcon.png" "$CONTENTS_DIR/Resources/JarvisMenuIcon.png"
CLANG_MODULE_CACHE_PATH="$MODULE_CACHE" SWIFT_MODULE_CACHE_PATH="$MODULE_CACHE" \
swiftc -parse-as-library -sdk "$SDK_PATH" -target arm64-apple-macosx14.0 -O \
  -framework AppKit -framework ApplicationServices -framework AVFoundation -framework Speech \
  -framework Security -framework UserNotifications \
  "$PROJECT_DIR/macos/Jarvis.swift" "$PROJECT_DIR/macos/RealtimeVoice.swift" \
  "$PROJECT_DIR/macos/Setup.swift" -o "$CONTENTS_DIR/MacOS/Jarvis"
cp "$PROJECT_DIR/macos/Info.plist" "$CONTENTS_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :JarvisProjectPath $PROJECT_DIR" "$CONTENTS_DIR/Info.plist"
xattr -cr "$APP_DIR"
codesign --force --deep --sign - "$APP_DIR"
xattr -cr "$APP_DIR"
echo "$APP_DIR"
