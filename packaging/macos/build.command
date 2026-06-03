#!/bin/bash
# ============================================================================
#  Cool MP3 Player — macOS build script
#  Double-click this file (or run it in Terminal) to build the app + .dmg.
#  It does everything in a self-contained folder and won't touch your system
#  Python. When it finishes you'll get:  dist/macos/Cool MP3 Player.dmg
# ============================================================================
set -e

# Always work from the project root (two levels up from this script), no matter
# where it's launched from (double-click sets CWD to $HOME otherwise).
cd "$(dirname "$0")/../.." || exit 1
ROOT="$(pwd)"
echo "Project root: $ROOT"
echo

# 1. Make sure Python 3 is available.
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌  Python 3 is not installed."
  echo "    Install it from https://www.python.org/downloads/macos/ (the big"
  echo "    yellow 'Download Python' button), then double-click this file again."
  echo
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi
echo "✅  Using $(python3 --version)"

# 2. Build everything inside an isolated virtual environment (clean + safe).
VENV="$ROOT/build/macos-venv"
echo "→  Setting up an isolated build environment (first run downloads packages)..."
python3 -m venv "$VENV"
# shellcheck disable=SC1090
source "$VENV/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyinstaller pygame Pillow mutagen numpy

# 3. Build the .app bundle from the spec.
echo "→  Building Cool MP3 Player.app ..."
python -m PyInstaller --noconfirm \
    --distpath "dist/macos" \
    --workpath "build/macos" \
    "packaging/macos/cool_mp3_player.spec"

APP="$ROOT/dist/macos/Cool MP3 Player.app"
if [ ! -d "$APP" ]; then
  echo "❌  Build finished but the .app was not found. Scroll up for the error."
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi

# 4. Wrap the .app into a compressed .dmg using macOS's built-in hdiutil.
DMG="$ROOT/dist/macos/Cool MP3 Player.dmg"
echo "→  Packaging into a .dmg ..."
rm -f "$DMG"
hdiutil create -volname "Cool MP3 Player" \
    -srcfolder "$APP" -ov -format UDZO "$DMG" >/dev/null

echo
echo "🎉  Done!"
echo "    App:  $APP"
echo "    DMG:  $DMG"
echo
echo "    Share the .dmg, or open it and drag the app to Applications."
read -n 1 -s -r -p "Press any key to close..."
