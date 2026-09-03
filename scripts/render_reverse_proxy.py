#!/usr/bin/env python3
"""
Render single-port reverse proxy.

Render web services expose exactly ONE public port. This proxy owns that public
listener and dispatches by path to two local backends that both live inside the
same container/process group:

  * /telegram  -> PTB Telegram webhook server  (aiohttp, reads POST bodies)
  * everything -> Embedded WebUI / WebSocket channel (websockets server)

This is what lets the Telegram webhook be publicly reachable on the same port
that already serves the admin WebUI, without forcing long-polling.

Routes / upstreams are configured through environment variables so the same
image works on Render and locally:

  PROXY_LISTEN_HOST   (default 0.0.0.0)
  PROXY_LISTEN_PORT   (default 8765, Render's $PORT)
  TELEGRAM_WEBHOOK_ORIGIN (default http://127.0.0.1:8081)
  TELEGRAM_WEBHOOK_PATH   (default /telegram)
  WEBUI_UPSTREAM          (default http://127.0.0.1:8766)
  WEBUI_WS_UPSTREAM       (default ws://127.0.0.1:8766)
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [proxy] %(message)s",
)
logger = logging.getLogger("render-proxy")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw.isdigit():
        return int(raw)
    return default


LISTEN_HOST = os.environ.get("PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = _env_int("PROXY_LISTEN_PORT", int(os.environ.get("PORT", "8765") or 8765))
TG_ORIGIN = os.environ.get("TELEGRAM_WEBHOOK_ORIGIN", "http://127.0.0.1:8081").rstrip("/")
TG_PATH = os.environ.get("TELEGRAM_WEBHOOK_PATH", "/telegram")
WEBUI_ORIGIN = os.environ.get("WEBUI_UPSTREAM", "http://127.0.0.1:8766").rstrip("/")
WEBUI_WS_ORIGIN = os.environ.get(
    "WEBUI_WS_UPSTREAM", "ws://127.0.0.1:8766"
).rstrip("/")

# Hop-by-hop headers we never forward.
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _HOP_HEADERS:
            continue
        # Normalize case so duplicate-ish keys don't accumulate.
        out[key] = value
    return out


def _is_websocket_upgrade(headers: dict[str, str]) -> bool:
    connection = headers.get("Connection", headers.get("connection", "")).lower()
    upgrade = headers.get("Upgrade", headers.get("upgrade", "")).lower()
    return "upgrade" in connection and upgrade == "websocket"


async def _proxy_webui_http_raw(request: web.Request) -> web.StreamResponse:
    """Forward ordinary HTTP to the websockets server without connection reuse."""
    parsed = urlsplit(WEBUI_ORIGIN)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = request.raw_path
    body = await request.read()
    headers = _clean_headers(dict(request.headers))
    headers["Host"] = parsed.netloc
    # HTTP/1.1 keep-alive (default when no Connection header is sent): the
    # upstream reads the next pipelined request from this same connection and
    # our raw writer never follows up, so the response times out -> 502.
    # Force "close" to match how aiohttp's connector behaves.
    headers["Connection"] = "close"
    if body:
        headers["Content-Length"] = str(len(body))
    else:
        headers.pop("Content-Length", None)
    request_head = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    raw_request = (
        f"{request.method} {target} HTTP/1.1\r\n"
        f"{request_head}\r\n"
    ).encode("latin-1") + body
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(raw_request)
        await writer.drain()
        response_head = await reader.readuntil(b"\r\n\r\n")
        response_lines = response_head[:-4].split(b"\r\n")
        status_line = response_lines[0].decode("latin-1")
        status = int(status_line.split(" ", 2)[1])
        response_headers: dict[str, str] = {}
        for line in response_lines[1:]:
            if b":" not in line:
                continue
            key, value = line.split(b":", 1)
            response_headers[key.decode("latin-1").strip()] = value.decode("latin-1").strip()
        content_length = int(response_headers.get("Content-Length", "0") or "0")
        response_body = await reader.readexactly(content_length) if content_length else b""
        writer.close()
        await writer.wait_closed()
        return web.Response(
            status=status,
            body=response_body,
            headers={
                key: value
                for key, value in response_headers.items()
                if key.lower() in {"content-type", "content-encoding", "location", "cache-control", "www-authenticate"}
            },
        )
    except (asyncio.IncompleteReadError, OSError, ValueError) as exc:
        logger.warning("raw WebUI proxy error (%s): %s", WEBUI_ORIGIN + target, exc)
        return web.Response(status=502, text="upstream unavailable")


async def _proxy_http(
    request: web.Request, upstream_base: str, *, append_path: bool = True
) -> web.StreamResponse:
    if append_path and upstream_base == WEBUI_ORIGIN:
        return await _proxy_webui_http_raw(request)
    # For the Telegram webhook, upstream_base already includes the exact path
    # (TG_ORIGIN + TG_PATH). For the WebUI we must preserve the incoming path
    # and querystring so sub-routes (bootstrap, static, api, ...) keep working.
    if append_path:
        target = upstream_base + request.raw_path
    else:
        target = upstream_base
    body = await request.read()
    headers = _clean_headers(dict(request.headers))
    headers.setdefault("Host", upstream_base.split("://", 1)[-1])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                request.method,
                target,
                data=body,
                headers=headers,
                allow_redirects=False,
            ) as upstream_resp:
                resp_body = await upstream_resp.read()
                response = web.Response(
                    status=upstream_resp.status,
                    body=resp_body,
                )
                content_type = upstream_resp.headers.get("Content-Type")
                if content_type:
                    response.content_type = content_type.split(";", 1)[0].strip()
                return response
    except (aiohttp.ClientError, OSError) as exc:
        logger.warning("upstream proxy error (%s): %s", target, exc)
        return web.Response(status=502, text="upstream unavailable")


async def _forward_telegram(request: web.Request) -> web.StreamResponse:
    # Telegram webhook: require the configured secret token header.
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if expected and supplied != expected:
        logger.warning("rejecting telegram webhook: bad secret token")
        return web.Response(status=401, text="unauthorized")
    return await _proxy_http(request, TG_ORIGIN + TG_PATH, append_path=False)


async def _proxy_websocket(request: web.Request) -> web.StreamResponse:
    """Tunnel a WebSocket upgrade to the WebUI upstream, relaying frames."""
    server_ws = web.WebSocketResponse(autoping=False, heartbeat=30.0)
    await server_ws.prepare(request)

    headers = dict(request.headers)
    target = WEBUI_WS_ORIGIN + request.raw_path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                target,
                headers=headers,
                autoping=False,
                heartbeat=30.0,
            ) as client_ws:
                async def client_to_server() -> None:
                    async for msg in client_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await server_ws.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await server_ws.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.PING:
                            await server_ws.ping(msg.data)
                        elif msg.type == aiohttp.WSMsgType.PONG:
                            await server_ws.pong(msg.data)
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            await server_ws.close(code=msg.data if isinstance(msg.data, int) else 1000)
                            break

                async def server_to_client() -> None:
                    async for msg in server_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await client_ws.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await client_ws.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.PING:
                            await client_ws.ping(msg.data)
                        elif msg.type == aiohttp.WSMsgType.PONG:
                            await client_ws.pong(msg.data)
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            await client_ws.close(code=msg.data if isinstance(msg.data, int) else 1000)
                            break

                # Bidirectional relay; cancel the pair when either side ends.
                await asyncio.wait(
                    {
                        asyncio.create_task(client_to_server()),
                        asyncio.create_task(server_to_client()),
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
    except (aiohttp.ClientError, OSError) as exc:
        logger.warning("websocket proxy error: %s", exc)

    return server_ws


async def _forward_api_with_body(request: web.Request) -> web.StreamResponse:
    """Proxy /v1/* to the WebUI, moving any POST body into ?payload=."""
    from urllib.parse import parse_qsl, quote, urlencode

    body = await request.read()
    parsed = urlsplit(request.raw_path)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if body:
        params["payload"] = body.decode("utf-8", "replace").strip()
    new_query = urlencode(params, quote_via=quote)
    target = WEBUI_ORIGIN + parsed.path + (("?" + new_query) if new_query else "")

    headers = _clean_headers(dict(request.headers))
    headers.setdefault("Host", WEBUI_ORIGIN.split("://", 1)[-1])
    # Content-Length must NOT be forwarded: we send no body upstream now, and
    # aiohttp would otherwise emit a stale length that breaks the request.
    headers.pop("Content-Length", None)

    timeout = aiohttp.ClientTimeout(total=None, sock_read=900, sock_connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                request.method, target, headers=headers, allow_redirects=False
            ) as upstream_resp:
                raw = await upstream_resp.read()
                resp_headers = {
                    k: v
                    for k, v in upstream_resp.headers.items()
                    if k.lower()
                    not in _HOP_HEADERS | {"content-length", "content-encoding", "transfer-encoding"}
                }
                return web.Response(status=upstream_resp.status, body=raw, headers=resp_headers)
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("api proxy error (%s): %s", target.split("?", 1)[0], exc)
        return web.Response(status=502, text="upstream unavailable")


async def _dispatch(request: web.Request) -> web.StreamResponse:
    headers = dict(request.headers)
    path = request.path
    # Telegram webhook
    if path == TG_PATH:
        return await _forward_telegram(request)
    # OpenAI-compatible API: the embedded WebUI server (websockets lib) cannot
    # read POST bodies, so buffer the JSON body here and re-inject it as the
    # ?payload= query param that nanobot.api.gateway_routes expects. This makes
    # standard `curl -d '{...}'` / OpenAI-SDK style requests work unchanged.
    if path.startswith("/v1/"):
        return await _forward_api_with_body(request)
    # Render health probes terminate at the public proxy. Only the explicit
    # /health path is a plain "ok"; the root "/" is forwarded to the WebUI so
    # the admin dashboard loads at the site root instead of returning "ok".
    if path == "/health" and not _is_websocket_upgrade(headers):
        return web.Response(text="ok", content_type="text/plain")
    # WebSocket upgrade to the WebUI
    if _is_websocket_upgrade(headers):
        return await _proxy_websocket(request)
    # Everything else (including "/"): forward HTTP to the WebUI
    return await _proxy_http(request, WEBUI_ORIGIN)


def build_app() -> web.Application:
    app = web.Application()
    # Telegram webhook path is matched verbatim and takes priority.
    app.router.add_route("*", TG_PATH, _dispatch, name="telegram-webhook")
    # All other paths (WebUI HTTP + WebSocket upgrades) go to the WebUI.
    # `/` is matched first so it is not shadowed by the catch-all.
    app.router.add_get("/", _dispatch, name="root")
    app.router.add_route("*", "/{tail:.*}", _dispatch, name="catch-all")
    return app


if __name__ == "__main__":
    port = _env_int("PROXY_LISTEN_PORT", int(os.environ.get("PORT", "8765") or 8765))
    logger.info(
        "starting reverse proxy on %s:%s -> telegram=%s%s, webui=%s (ws=%s)",
        LISTEN_HOST,
        port,
        TG_ORIGIN,
        TG_PATH,
        WEBUI_ORIGIN,
        WEBUI_WS_ORIGIN,
    )
    app = build_app()
    web.run_app(app, host=LISTEN_HOST, port=port, print=None)
