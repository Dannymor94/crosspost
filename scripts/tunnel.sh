#!/usr/bin/env bash
# Start a localhost.run SSH tunnel on port 8000 and update INSTAGRAM_PUBLIC_BASE_URL in runtime/.env.
# Usage: ./scripts/tunnel.sh
# Stop: kill $(cat /tmp/crosspost-tunnel.pid)
set -euo pipefail

PORT=8000
ENV_FILE="runtime/.env"
TUNNEL_LOG="/tmp/crosspost-tunnel.log"
PID_FILE="/tmp/crosspost-tunnel.pid"

# Kill any existing tunnel
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Stopping existing tunnel (PID $(cat "$PID_FILE"))..."
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  sleep 1
fi

rm -f "$TUNNEL_LOG"

ssh -o StrictHostKeyChecking=accept-new \
    -o ServerAliveInterval=30 \
    -R "80:localhost:$PORT" \
    nokey@localhost.run > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
echo "$TUNNEL_PID" > "$PID_FILE"

echo "Waiting for tunnel URL..."
for i in $(seq 1 20); do
  TUNNEL_URL=$(grep -o 'https://[a-z0-9]*.lhr.life' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)
  if [ -n "$TUNNEL_URL" ]; then
    break
  fi
  sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
  echo "ERROR: tunnel did not start. Log:" >&2
  cat "$TUNNEL_LOG" >&2
  exit 1
fi

# Update or append INSTAGRAM_PUBLIC_BASE_URL in runtime/.env
if grep -q '^INSTAGRAM_PUBLIC_BASE_URL=' "$ENV_FILE" 2>/dev/null; then
  # macOS-compatible sed in-place
  sed -i '' "s|^INSTAGRAM_PUBLIC_BASE_URL=.*|INSTAGRAM_PUBLIC_BASE_URL=${TUNNEL_URL}|" "$ENV_FILE"
else
  printf '\nINSTAGRAM_PUBLIC_BASE_URL=%s\n' "$TUNNEL_URL" >> "$ENV_FILE"
fi

echo ""
echo "Tunnel running:"
echo "  URL : $TUNNEL_URL"
echo "  PID : $TUNNEL_PID  (saved to $PID_FILE)"
echo "  Log : $TUNNEL_LOG"
echo ""
echo "To stop: kill \$(cat $PID_FILE)"
echo ""
echo "INSTAGRAM_PUBLIC_BASE_URL updated in $ENV_FILE"
echo "Restart the app server for the new URL to take effect: make run"
