#!/bin/bash
# Start a packaged Chatbox server and read the page. Does not pipe curl into grep.
set -euo pipefail
BIN="${1:?binary}"
shift
PORT="${CHATBOX_SMOKE_PORT:-8765}"
DATA="$(mktemp -d)"
CHATBOX_DATA="$DATA" "$BIN" "$@" --no-browser --port "$PORT" >"$DATA/server.log" 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true' EXIT
ready=0
i=0
while [ "$i" -lt 60 ]; do
  i=$((i + 1))
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "server exited before it was ready" >&2
    cat "$DATA/server.log" >&2 || true
    exit 1
  fi
  code="$(curl -sS --max-time 2 -o "$DATA/status.json" -w "%{http_code}" "http://127.0.0.1:${PORT}/api/status" || true)"
  if [ "$code" = "200" ] && grep -F -q '"home"' "$DATA/status.json"; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" != 1 ]; then
  echo "server did not answer /api/status" >&2
  cat "$DATA/server.log" >&2 || true
  exit 1
fi
curl -fsS --max-time 10 -o "$DATA/index.html" "http://127.0.0.1:${PORT}/"
grep -F -q "ui/js/app.js" "$DATA/index.html"
echo "smoke ok ($BIN)"
