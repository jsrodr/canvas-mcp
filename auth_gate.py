"""
auth_gate.py

A minimal authenticating reverse proxy for canvas-mcp's streamable-http mode.

Why this exists:
The canvas-mcp maintainer explicitly retired their own public hosted server
because an MCP endpoint with no access gate isn't safe to run on the open
internet (it exposes a code-execution tool). This script sits in front of
the real canvas-mcp-server process and rejects any request that doesn't
carry a shared secret you choose. Only requests carrying the correct
secret get proxied through to the real server.

It does NOT touch your Canvas API token. Your Canvas token is a *separate*
header that the client (Claude) sends straight through to canvas-mcp on
every request, exactly as the project's README describes ("your Canvas
token is sent as an HTTP header per-request and never stored on the
server"). This proxy only checks the GATEWAY_SECRET header; every other
header, including your Canvas token header, passes through untouched.

Environment variables this script reads:
  GATEWAY_SECRET      Required. The shared secret you choose. Set this in
                       Render's dashboard as an environment variable -
                       never commit it to the repo.
  UPSTREAM_HOST        Optional. Where the real canvas-mcp-server is
                       listening internally. Default: 127.0.0.1
  UPSTREAM_PORT         Optional. Default: 8000
  GATEWAY_HEADER_NAME  Optional. The header clients must send the secret
                       in. Default: "x-gateway-secret"

How requests flow:
  Claude -> https://your-app.onrender.com/mcp   (public, $PORT, this script)
         -> checks x-gateway-secret header
         -> if valid, proxies to http://127.0.0.1:8000/mcp  (canvas-mcp-server)
         -> if invalid or missing, returns 401 immediately, never reaches
            canvas-mcp-server at all

Run canvas-mcp-server on the internal port first, then run this script
with uvicorn on $PORT. See start.sh for the exact commands.
"""

import os
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse, PlainTextResponse
from starlette.routing import Route

GATEWAY_SECRET = os.environ.get("GATEWAY_SECRET")
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = os.environ.get("UPSTREAM_PORT", "8000")
GATEWAY_HEADER_NAME = os.environ.get("GATEWAY_HEADER_NAME", "x-gateway-secret").lower()
UPSTREAM_BASE = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}"

if not GATEWAY_SECRET:
    raise RuntimeError(
        "GATEWAY_SECRET is not set. Set it as an environment variable "
        "before starting this proxy - do not hardcode it in the file."
    )

# Reused across requests so we're not opening a fresh TCP connection every call.
_client = httpx.AsyncClient(base_url=UPSTREAM_BASE, timeout=None)


async def proxy(request: Request):
    supplied = request.headers.get(GATEWAY_HEADER_NAME)

    # --- TEMPORARY DEBUG LOGGING ---
    # Prints to Render's log so we can compare what arrived vs. what's
    # expected, WITHOUT ever printing the full secret. Safe to remove
    # once things are working.
    incoming_header_names = sorted(request.headers.keys())
    print(f"[auth_gate debug] path={request.url.path} method={request.method}")
    print(f"[auth_gate debug] incoming header names: {incoming_header_names}")
    print(f"[auth_gate debug] looking for header named: {GATEWAY_HEADER_NAME!r}")
    if supplied is None:
        print("[auth_gate debug] that header was NOT present on this request at all")
    else:
        print(
            f"[auth_gate debug] supplied value length={len(supplied)} "
            f"starts_with={supplied[:4]!r} ends_with={supplied[-4:]!r}"
        )
        print(
            f"[auth_gate debug] expected value length={len(GATEWAY_SECRET)} "
            f"starts_with={GATEWAY_SECRET[:4]!r} ends_with={GATEWAY_SECRET[-4:]!r}"
        )
        print(f"[auth_gate debug] values match: {supplied == GATEWAY_SECRET}")
    # --- END TEMPORARY DEBUG LOGGING ---

    if supplied != GATEWAY_SECRET:
        return PlainTextResponse("Unauthorized", status_code=401)

    # Forward every header except the gateway secret itself and hop-by-hop
    # headers that shouldn't be forwarded as-is. Your Canvas token header
    # (whatever canvas-mcp expects it to be named) passes through here
    # untouched, straight to the real server.
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {GATEWAY_HEADER_NAME, "host", "content-length"}
    }

    body = await request.body()

    upstream_request = _client.build_request(
        method=request.method,
        url=request.url.path,
        headers=forward_headers,
        content=body,
        params=request.query_params,
    )

    upstream_response = await _client.send(upstream_request, stream=True)

    async def body_stream():
        async for chunk in upstream_response.aiter_raw():
            yield chunk
        await upstream_response.aclose()

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in {"content-length", "connection", "transfer-encoding"}
    }

    return StreamingResponse(
        body_stream(),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


async def health(request: Request):
    # Unauthenticated on purpose, so Render's health checks can hit it
    # without needing the secret. It reveals nothing about canvas-mcp.
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/healthz", health, methods=["GET"]),
        Route("/{path:path}", proxy, methods=["GET", "POST", "DELETE", "OPTIONS"]),
    ]
)
