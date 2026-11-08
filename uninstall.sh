#!/usr/bin/env bash
# Remove Chatbox, its menu entry and icon. Your Cursor / Claude / Codex sign-ins
# and the tools themselves are left alone.
set -eu
D="${CHATBOX_DATA:-$HOME/.local/share/chatbox}"
rm -rf "$D"
rm -f "$HOME/.local/share/applications/chatbox.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/chatbox.svg"
rm -rf "$HOME/Applications/Chatbox.app" /Applications/Chatbox.app 2>/dev/null || true   # macOS
echo "Chatbox removed."
