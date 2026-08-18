#!/usr/bin/env bash
# start.sh
#
# Runs inside the Docker container as the single startup command.
# 1. Starts the real canvas-mcp-server on an internal-only port (8000),
#    listening only on 127.0.0.1 so it's not reachable from outside the
#    container directly.
# 2. Waits until it's ACTUALLY accepting connections (not just a fixed
#    guess) - important on Render's free tier, where the whole container
#    can cold-start after being asleep and take much longer than usual.
# 3. Starts the auth_gate proxy on Render's public $PORT, which is the
#    only thing actually exposed to the internet.
set -euo pipefail

INTERNAL_PORT=8000

echo "Starting canvas-mcp-server internally on 127.0.0.1:${INTERNAL_PORT}..."
canvas-mcp-server --transport streamable-http --host 127.0.0.1 --port "${INTERNAL_PORT}" &
MCP_PID=$!

echo "Waiting for canvas-mcp-server to actually accept connections..."
WAITED=0
MAX_WAIT=60
until (exec 3<>"/dev/tcp/127.0.0.1/${INTERNAL_PORT}") 2>/dev/null; do
  if ! kill -0 "${MCP_PID}" 2>/dev/null; then
    echo "canvas-mcp-server exited before it ever started listening - aborting."
    exit 1
  fi
  if [ "${WAITED}" -ge "${MAX_WAIT}" ]; then
    echo "Gave up waiting for canvas-mcp-server after ${MAX_WAIT}s."
    exit 1
  fi
  sleep 1
  WAITED=$((WAITED + 1))
done
exec 3<&- 2>/dev/null || true
echo "canvas-mcp-server is up after ${WAITED}s."

echo "Starting auth gate on 0.0.0.0:${PORT}..."
UPSTREAM_HOST=127.0.0.1 UPSTREAM_PORT="${INTERNAL_PORT}" \
  uvicorn auth_gate:app --host 0.0.0.0 --port "${PORT}" &
GATE_PID=$!

# If either process dies, stop the container so Render restarts it cleanly
# instead of silently running half-broken.
wait -n "${MCP_PID}" "${GATE_PID}"
