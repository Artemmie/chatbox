#!/usr/bin/env bash
# Install Chatbox for the current user and add it to the app menu.
#
#   ./install.sh                     own window (installs PyQt6 into a private venv)
#   ./install.sh --browser           no extra downloads; opens in your web browser
#   ./install.sh --distrobox NAME    menu entry runs Chatbox inside that distrobox
#
# Everything goes into ~/.local/share/chatbox (or $CHATBOX_DATA). Nothing needs root.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
D="${CHATBOX_DATA:-$HOME/.local/share/chatbox}"
MODE=window
BOX=""

while [ $# -gt 0 ]; do
  case "$1" in
    --browser) MODE=browser ;;
    --distrobox) BOX="${2:?--distrobox needs a container name}"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

command -v python3 >/dev/null || { echo "Python 3 is required." >&2; exit 1; }

echo "Installing Chatbox to $D"
mkdir -p "$D"
cp "$SRC"/chatbox.py "$SRC"/accounts.py "$SRC"/memory.py "$SRC"/vault.py "$SRC"/app.py "$SRC"/chatbox.svg "$D"/

has_qt() { "$D/venv/bin/python" -c "import PyQt6.QtWebEngineWidgets" 2>/dev/null; }

get_uv() {
  if command -v uv >/dev/null; then command -v uv; return; fi
  if [ ! -x "$D/bin/uv" ]; then
    echo "Downloading uv (Python package tool by Astral)…" >&2
    local arch; arch="$(uname -m)"; [ "$arch" = arm64 ] && arch=aarch64
    mkdir -p "$D/bin"
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-unknown-linux-gnu.tar.gz" \
      | tar xz -C "$D/bin" --strip-components=1
  fi
  echo "$D/bin/uv"
}

if [ "$MODE" = window ] && ! has_qt; then
  echo "Setting up the window (PyQt6 + Qt WebEngine, about 150 MB download)…"
  if python3 -c "import ensurepip" 2>/dev/null; then
    python3 -m venv "$D/venv" && "$D/venv/bin/pip" install -q PyQt6 PyQt6-WebEngine || true
  else
    UV="$(get_uv)"
    UV_CACHE_DIR="$D/.uv-cache" "$UV" venv -q --python "$(command -v python3)" "$D/venv" \
      && UV_CACHE_DIR="$D/.uv-cache" "$UV" pip install -q --python "$D/venv/bin/python" PyQt6 PyQt6-WebEngine || true
    rm -rf "$D/.uv-cache"
  fi
  if ! has_qt; then
    echo "Could not set up the window. Falling back to browser mode." >&2
    MODE=browser
  fi
fi

if [ "$MODE" = window ]; then
  RUN='exec "$D/venv/bin/python" "$D/app.py" "$@"'
else
  RUN='exec python3 "$D/chatbox.py" --port 8766 "$@"'
fi
cat > "$D/run.sh" <<EOF
#!/bin/sh
# Starts Chatbox ($MODE mode). Created by install.sh.
D="$D"
export PATH="\$HOME/.local/bin:\$PATH"
$RUN >>"\$D/chatbox.log" 2>&1
EOF
chmod +x "$D/run.sh"

EXEC="$D/run.sh"
[ -n "$BOX" ] && EXEC="distrobox enter $BOX -- $D/run.sh"

ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
APPS="$HOME/.local/share/applications"
mkdir -p "$ICONS" "$APPS"
cp "$SRC/chatbox.svg" "$ICONS/chatbox.svg"
cat > "$APPS/chatbox.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Chatbox
GenericName=AI Chat
Comment=Chat with Cursor, Claude, Codex or ChatGPT
Exec=$EXEC
Icon=chatbox
Terminal=false
StartupNotify=true
StartupWMClass=chatbox
Categories=Utility;Development;
Keywords=ai;chat;cursor;claude;codex;chatgpt;
EOF

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

echo "Done ($MODE mode). Open \"Chatbox\" from your app menu, or run: $D/run.sh"
echo "On first start, use Accounts (top right) to install the tools and sign in."
