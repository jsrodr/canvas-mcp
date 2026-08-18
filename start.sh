#!/usr/bin/env bash
# start.sh
#
# Runs inside the Docker container as the single startup command.
# 1. Starts the real canvas-mcp-server on an internal-only port (8000),
#    listening only on 127.0.0.1 so it's not reachable from outside the
#    container directly.
# 2. Starts the auth_gate proxy on Render's public $PORT, which is the
#    only thing actually exposed to the internet.
set -euo pipefail

INTERNAL_PORT=8000

echo "Starting canvas-mcp-server internally on 127.0.0.1:${INTERNAL_PORT}..."
canvas-mcp-server --transport streamable-http --host 127.0.0.1 --port "${INTERNAL_PORT}" &
MCP_PID=$!

# Give it a moment to come up before the proxy starts forwarding to it.
sleep 2

echo "Starting auth gate on 0.0.0.0:${PORT}..."
UPSTREAM_HOST=127.0.0.1 UPSTREAM_PORT="${INTERNAL_PORT}" \
  uvicorn auth_gate:app --host 0.0.0.0 --port "${PORT}" &
GATE_PID=$!

# If either process dies, stop the container so Render restarts it cleanly
# instead of silently running half-broken.
wait -n "${MCP_PID}" "${GATE_PID}"
