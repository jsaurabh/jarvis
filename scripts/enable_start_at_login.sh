#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
APP_EXECUTABLE="$PROJECT_DIR/dist/Jarvis.app/Contents/MacOS/Jarvis"
PLIST="$HOME/Library/LaunchAgents/com.cray.jarvis.plist"

if [[ ! -x "$APP_EXECUTABLE" ]]; then
  echo "Build Jarvis first with ./scripts/build_macos.sh" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
defaults write "$PLIST" Label -string com.cray.jarvis
defaults write "$PLIST" ProgramArguments -array "$APP_EXECUTABLE"
defaults write "$PLIST" RunAtLoad -bool true
defaults write "$PLIST" ProcessType -string Interactive
defaults write "$PLIST" LimitLoadToSessionType -string Aqua
defaults write com.cray.jarvis didInitializeLogin -bool true
plutil -lint "$PLIST"
echo "Jarvis will start at login. Use the menu-bar toggle to turn this off."
