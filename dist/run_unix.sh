#!/usr/bin/env bash
# LazeR — laptop server one-shot setup + run (macOS / Linux).
#
#   1. Finds python3.
#   2. Installs the Python dependency (pynput).
#   3. Starts the server and prints the IP + token.
#
# Usage:  bash run_unix.sh            (or ./run_unix.sh after chmod +x)
#         bash run_unix.sh --install-apk   # push APK if adb + phone present
set -e
cd "$(dirname "$0")"

echo "=== LazeR server setup ==="

# 1. Python
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "python3 not found. Install it:"
  echo "  macOS:  brew install python   (or https://www.python.org/downloads/)"
  echo "  Linux:  sudo apt install python3 python3-pip   (or your distro's package)"
  exit 1
fi
echo "Python: $($PY --version)"

# 2. Dependencies (pynput only; volume uses built-in osascript/amixer/pactl)
echo "Installing dependencies..."
"$PY" -m pip install --user --quiet pynput cryptography zeroconf qrcode pillow pystray || \
  "$PY" -m pip install --user --quiet --break-system-packages pynput cryptography zeroconf qrcode pillow pystray
echo "Dependencies ready."

# macOS needs Accessibility permission for pynput to move the mouse.
if [ "$(uname)" = "Darwin" ]; then
  echo "macOS: if the cursor doesn't move, grant your terminal Accessibility:"
  echo "  System Settings > Privacy & Security > Accessibility"
fi
# Linux volume backends
if [ "$(uname)" = "Linux" ]; then
  command -v amixer >/dev/null 2>&1 || command -v pactl >/dev/null 2>&1 || \
    echo "Linux: install alsa-utils (amixer) or pulseaudio-utils (pactl) for volume control."
fi

# 3. Optional APK push
if [ "$1" = "--install-apk" ]; then
  if command -v adb >/dev/null 2>&1 && [ -f LazeR.apk ]; then
    echo "Installing LazeR.apk on the connected phone..."
    adb install -r LazeR.apk
  else
    echo "adb or LazeR.apk missing — copy LazeR.apk to your phone and tap to install."
  fi
else
  echo "Phone app: copy LazeR.apk to your phone and tap to install (allow 'unknown apps')."
fi

# 4. Run
echo
echo "Starting server (Ctrl+C to stop)..."
echo
PYTHONUNBUFFERED=1 "$PY" server/remote_server.py
