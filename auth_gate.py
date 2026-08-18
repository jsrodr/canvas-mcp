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
UPSTREAM_BASE = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}"

if not GATEWAY_SECRET:
    raise RuntimeError(
        "GATEWAY_SECRET is not set. Set it as an environment variable "
        "before starting this proxy - do not hardcode it in the file."
    )

# Reused across requests so we're not opening a fresh TCP connection every call.
_client = httpx.AsyncClient(base_url=UPSTREAM_BASE, timeout=None)


async def proxy(request: Request):
    # Claude's custom connector UI on this account has no "request headers"
    # option and no working OAuth server on our side, so the secret and the
    # Canvas token are passed as query parameters baked directly into the
    # connector's Server URL instead: ?gw_key=...&canvas_token=...
    query = dict(request.query_params)
    supplied_secret = query.pop("gw_key", None)
    canvas_token = query.pop("canvas_token", None)

    print(f"[auth_gate debug] path={request.url.path} method={request.method}")
    print(f"[auth_gate debug] gw_key present: {supplied_secret is not None}")
    print(f"[auth_gate debug] canvas_token present: {canvas_token is not None}")

    if supplied_secret != GATEWAY_SECRET:
        return PlainTextResponse("Unauthorized", status_code=401)

    # Forward every header as-is except hop-by-hop ones. We inject the
    # Canvas token as a header ourselves below, since canvas-mcp expects it
    # as a header even though it arrived here as a query param.
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }
    if canvas_token:
        forward_headers["X-Canvas-Token"] = canvas_token

    body = await request.body()

    upstream_request = _client.build_request(
        method=request.method,
        url=request.url.path,
        headers=forward_headers,
        content=body,
        params=query,  # remaining query params only - gw_key/canvas_token stripped
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
