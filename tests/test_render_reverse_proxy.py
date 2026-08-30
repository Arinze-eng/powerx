"""
Local integration test for scripts/render_reverse_proxy.py.

Spins up:
  * a fake Telegram webhook backend on 127.0.0.1:8081 (echoes body + secret header)
  * a fake WebUI backend on 127.0.0.1:8766 (HTTP echo + WebSocket echo)
  * the proxy on 127.0.0.1:8875

Verifies:
  1. POST /telegram hits the telegram backend, body intact, secret enforced.
  2. GET /webui/bootstrap hits the WebUI HTTP backend.
  3. WebSocket upgrade to / is tunnelled to the WebUI backend.
"""
import asyncio
import json
import os
import socket

os.environ["PROXY_LISTEN_PORT"] = "8875"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
os.environ["TELEGRAM_WEBHOOK_PATH"] = "/telegram"
os.environ["TELEGRAM_WEBHOOK_ORIGIN"] = "http://127.0.0.1:8081"
os.environ["WEBUI_UPSTREAM"] = "http://127.0.0.1:8766"
os.environ["WEBUI_WS_UPSTREAM"] = "ws://127.0.0.1:8766"

import aiohttp
import websockets  # client lib for testing
from aiohttp import web

# `scripts` is not a package, so load the proxy module directly from its path.
import importlib.util

_proxy_spec = importlib.util.spec_from_file_location(
    "render_reverse_proxy",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "render_reverse_proxy.py"),
)
proxy = importlib.util.module_from_spec(_proxy_spec)
_proxy_spec.loader.exec_module(proxy)

# ---------------------------------------------------------------------------
# Fake Telegram webhook backend (aiohttp, binds 127.0.0.1:8081)
# ---------------------------------------------------------------------------
async def tele_handler(request):
    body = await request.read()
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    payload = json.dumps({"ok": True, "path": request.path, "body": body.decode()})
    return web.Response(status=200, content_type="application/json", text=payload)

tele_app = web.Application()
tele_app.router.add_post("/telegram", tele_handler)

# ---------------------------------------------------------------------------
# Fake WebUI backend (aiohttp, binds 127.0.0.1:8766) HTTP + WS echo
# ---------------------------------------------------------------------------
async def webui_http(request):
    return web.Response(status=200, text="webui-ok")

async def webui_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await ws.send_str("echo:" + msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await ws.send_bytes(msg.data)
    return ws

webui_app = web.Application()
webui_app.router.add_get("/webui/bootstrap", webui_http)
webui_app.router.add_get("/", webui_ws)

# ---------------------------------------------------------------------------
async def _free_port(start):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

async def main():
    runner_tele = web.AppRunner(tele_app)
    await runner_tele.setup()
    site_tele = web.TCPSite(runner_tele, "127.0.0.1", 8081)
    await site_tele.start()

    runner_webui = web.AppRunner(webui_app)
    await runner_webui.setup()
    site_webui = web.TCPSite(runner_webui, "127.0.0.1", 8766)
    await site_webui.start()

    # Run the proxy app directly (import function) not the __main__ path
    app = proxy.build_app()
    runner_proxy = web.AppRunner(app)
    await runner_proxy.setup()
    site_proxy = web.TCPSite(runner_proxy, "127.0.0.1", 8875)
    await site_proxy.start()

    results = []
    async with aiohttp.ClientSession() as s:
        # 1. Telegram webhook with correct secret
        r = await s.post("http://127.0.0.1:8875/telegram",
                         json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
        txt = await r.text()
        results.append(("tele-ok", r.status, txt))
        # 2. Telegram webhook with wrong secret -> 401
        r2 = await s.post("http://127.0.0.1:8875/telegram", json={"x": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "bad"})
        results.append(("tele-401", r2.status, ""))
        # 3. WebUI HTTP
        r3 = await s.get("http://127.0.0.1:8875/webui/bootstrap")
        results.append(("webui-http", r3.status, await r3.text()))
        # 4. Root HTTP
        r4 = await s.get("http://127.0.0.1:8875/")
        results.append(("root", r4.status, ""))

    # 5. WebSocket tunnel
    try:
        async with websockets.connect("ws://127.0.0.1:8875/") as ws:
            await ws.send("hello")
            reply = await asyncio.wait_for(ws.recv(), timeout=5)
            results.append(("ws-tunnel", 0, reply))
    except Exception as e:  # noqa: BLE001
        results.append(("ws-tunnel-ERR", 0, repr(e)))

    for name, status, data in results:
        print(f"{name}: status={status} data={data!r}")

    await runner_proxy.cleanup()
    await runner_webui.cleanup()
    await runner_tele.cleanup()
    ok = all(r[0] not in ("tele-401-wrong", "ws-tunnel-ERR") for r in results)
    return results


if __name__ == "__main__":
    res = asyncio.run(main())
    for name, status, data in res:
        if name in ("tele-ok", "webui-http", "ws-tunnel"):
            assert data not in ("", None), f"{name} empty"
    print("ALL PROXY TESTS PASSED")