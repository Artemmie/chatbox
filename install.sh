#!/usr/bin/env bash
# Install Chatbox for the current user and add it to the app menu (Linux)
# or to /Applications as Chatbox.app (macOS; ~/Applications if /Applications isn't writable).
#
#   ./install.sh                     own window (installs PyQt6 into a private venv)
#   ./install.sh --browser           no extra downloads; opens in your web browser
#   ./install.sh --distrobox NAME    Linux: menu entry runs Chatbox inside that distrobox
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

OS="$(uname -s)"
command -v python3 >/dev/null || { echo "Python 3 is required." >&2; exit 1; }
if [ "$OS" = Darwin ] && [ "$(command -v python3)" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
  echo "Python 3 is missing. Run: xcode-select --install  (or install Python from python.org), then try again." >&2
  exit 1
fi
if [ -n "$BOX" ] && [ "$OS" = Darwin ]; then echo "--distrobox is for Linux only." >&2; exit 1; fi

echo "Installing Chatbox to $D"
mkdir -p "$D"
cp "$SRC"/*.py "$SRC"/chatbox.svg "$D"/
# VERSION is what the sidebar, Settings and the window title show. Without it the copy has no version.
if [ -f "$SRC/VERSION" ]; then cp "$SRC/VERSION" "$D"/; fi
rm -rf "$D/ui"
cp -R "$SRC/ui" "$D/ui"

has_qt() { "$D/venv/bin/python" -c "import PyQt6.QtWebEngineWidgets" 2>/dev/null; }

get_uv() {
  if command -v uv >/dev/null; then command -v uv; return; fi
  if [ ! -x "$D/bin/uv" ]; then
    echo "Downloading uv (Python package tool by Astral)…" >&2
    local arch; arch="$(uname -m)"; [ "$arch" = arm64 ] && arch=aarch64
    mkdir -p "$D/bin"
    local target=unknown-linux-gnu; [ "$OS" = Darwin ] && target=apple-darwin
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-$target.tar.gz" \
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
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH"
$RUN >>"\$D/chatbox.log" 2>&1
EOF
chmod +x "$D/run.sh"

# Remember this clone, the mode, and a distrobox name so a later update can
# fast-forward the same folder and run install.sh the same way. No tokens.
python3 - "$D" "$SRC" "$MODE" "$BOX" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import update
update.write_install(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
PY

if [ "$OS" = Darwin ]; then
  # macOS: a small Chatbox.app that starts run.sh. /Applications is what Finder and
  # Launchpad show; admin users can write there without sudo.
  APPDIR=/Applications
  [ -w "$APPDIR" ] || { APPDIR="$HOME/Applications"; mkdir -p "$APPDIR"; }
  APP="$APPDIR/Chatbox.app"
  rm -rf "$APP" "$HOME/Applications/Chatbox.app"   # also drop a copy from an older install
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  printf '#!/bin/sh\nexec "%s/run.sh"\n' "$D" > "$APP/Contents/MacOS/Chatbox"
  chmod +x "$APP/Contents/MacOS/Chatbox"
  cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Chatbox</string>
  <key>CFBundleDisplayName</key><string>Chatbox</string>
  <key>CFBundleIdentifier</key><string>local.chatbox</string>
  <key>CFBundleExecutable</key><string>Chatbox</string>
  <key>CFBundleIconFile</key><string>chatbox</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
  # Icon: render the SVG with Quick Look, then build an .icns (skipped if that fails).
  # qlmanage can hang; cap it so install still finishes without an icon.
  T="$(mktemp -d)"
  python3 - "$T" "$SRC/chatbox.svg" <<'PY' || true
import subprocess, sys
folder, svg = sys.argv[1], sys.argv[2]
try:
    subprocess.run(["qlmanage", "-t", "-s", "1024", "-o", folder, svg],
                   timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except (OSError, subprocess.TimeoutExpired):
    sys.exit(1)
PY
  if [ -f "$T/chatbox.svg.png" ]; then
    mkdir -p "$T/chatbox.iconset"
    for s in 16 32 128 256 512; do
      sips -z $s $s "$T/chatbox.svg.png" --out "$T/chatbox.iconset/icon_${s}x${s}.png" >/dev/null 2>&1 || true
      sips -z $((s*2)) $((s*2)) "$T/chatbox.svg.png" --out "$T/chatbox.iconset/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns "$T/chatbox.iconset" -o "$APP/Contents/Resources/chatbox.icns" 2>/dev/null || true
  fi
  rm -rf "$T"
  touch "$APP"
  # register it so Spotlight and Launchpad pick it up right away
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
  echo "Done ($MODE mode). Chatbox is in $APPDIR - open it from there, Launchpad or Spotlight (Cmd+Space, \"Chatbox\")."
  echo "Or run: $D/run.sh"
  echo "On first start, use Accounts (bottom left) to install the tools and sign in."
  exit 0
fi

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
echo "On first start, use Accounts (bottom left) to install the tools and sign in."
