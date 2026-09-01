"""Telegram Mini App for large file uploads (gofile.io).

Telegram's inline file upload is limited (documents and photos are transferred
through the bot, which is impractical for files above the 100 MB-plus range and
eats Render bandwidth). This module serves a Mini App that lets a user pick a
large file in the Telegram client, upload it directly to gofile.io from the
browser (bypassing the Render-hosted bot entirely), and then report the public
gofile.io download URL back to the API server.

The recorded upload is then available to the agent, which uses the
``novita_sandbox`` ``fetch_url`` action to pull the file into the Novita
Sandbox or the configured VPS for analysis without ever transferring the bytes
through the Render-hosted process.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from loguru import logger

GOFILE_DOMAINS = {
    "gofile.io",
    "api.gofile.io",
}
# gofile.io exposes upload servers under ``*.gofile.io`` as well.
def _is_gofile_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in GOFILE_DOMAINS:
        return True
    return host.endswith(".gofile.io")


def is_gofile_url(value: str) -> bool:
    """Return True when *value* is an HTTPS share/page URL on gofile.io."""
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and _is_gofile_host(parsed.netloc) and bool(parsed.path)


# ---------------------------------------------------------------------------
# Pending upload store
# ---------------------------------------------------------------------------


class _PendingUpload:
    """Metadata for one file handed off by the Mini App."""

    __slots__ = ("url", "file_id", "filename", "size", "created_at")

    def __init__(
        self,
        url: str,
        file_id: str,
        filename: str,
        size: int,
        created_at: float,
    ) -> None:
        self.url = url
        self.file_id = file_id
        self.filename = filename
        self.size = size
        self.created_at = created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "file_id": self.file_id,
            "filename": self.filename,
            "size": self.size,
            "created_at": self.created_at,
        }


class MiniAppUploadStore:
    """In-memory, thread-safe store of files uploaded through the Mini App.

    Entries are keyed by the agent session key so the agent can resolve which
    file (and its public URL) was handed off for the current session. Older
    entries are evicted after a TTL to bound memory growth.
    """

    _TTL_SECONDS = 60 * 60 * 24  # keep uploads for one day

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[str, list[_PendingUpload]] = {}

    def record(
        self,
        session_key: str,
        *,
        url: str,
        file_id: str,
        filename: str,
        size: int,
    ) -> _PendingUpload:
        upload = _PendingUpload(
            url=url,
            file_id=file_id,
            filename=filename or "upload.bin",
            size=int(size or 0),
            created_at=time.time(),
        )
        with self._lock:
            now = time.time()
            entries = self._pending.setdefault(session_key or "unknown", [])
            # Drop stale entries while appending the new upload.
            self._pending[session_key or "unknown"] = [
                entry for entry in entries if now - entry.created_at < self._TTL_SECONDS
            ]
            self._pending[session_key or "unknown"].append(upload)
        return upload

    def take_all(self, session_key: str) -> list[_PendingUpload]:
        """Return and clear the persisted uploads for *session_key*."""
        key = session_key or "unknown"
        with self._lock:
            entries = self._pending.pop(key, [])
            now = time.time()
            return [entry for entry in entries if now - entry.created_at < self._TTL_SECONDS]


# Module-level singleton shared across the API process and the tool layer.
upload_store = MiniAppUploadStore()


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


async def handle_miniapp_page(request: web.Request) -> web.Response:
    """GET /upload — serve the Telegram Mini App."""

    def _foundation() -> web.Response:
        return web.Response(
            text=MINIAPP_HTML,
            content_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    return _foundation()


def _format_upload_link_message(filename: str, url: str) -> str:
    """Build the Telegram message that surfaces a freshly-uploaded gofile link."""
    name = (filename or "upload").strip()[:120] or "upload"
    return (
        f"✅ File *{name}* is on GoFile and ready.\n\n"
        f"Your link:\n`{url}`\n\n"
        f"Now type what you want me to do with it (e.g. *analyze this file*, "
        f"*extract the contents*, *summarize it*). I'll pull it from the link."
    )


async def _push_link_to_telegram(
    url: str,
    filename: str,
    chat_id: str,
) -> None:
    """Best-effort: send the gofile link into the user's Telegram private chat.

    ``chat_id`` is the Telegram user id (the Mini App's ``session_key``), which
    matches the private bot chat id. Failures are logged, never raised, so the
    upload-completion response is not blocked if Telegram is unreachable.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id or chat_id == "unknown":
        logger.debug("Skipping Telegram link push (no token/chat) for {}", url)
        return
    text = _format_upload_link_message(filename, url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.warning(
                        "Telegram link push failed: HTTP {} {}", response.status, body[:200]
                    )
    except Exception as exc:  # noqa: BLE001 - best-effort outbound
        logger.warning("Telegram link push skipped: {}", exc)


async def handle_upload_complete(request: web.Request) -> web.Response:
    """Accept a gofile.io URL reported by the Mini App.

    Supports both POST with a JSON body (API server / aiohttp) and GET with
    query-string fields (also supported by the light-weight gateway that only
    exposes query params): url, file_id, filename, size, session_key.
    """
    if request.method == "GET":
        params = request.query
        url = str(params.get("url") or "").strip()
        filename = str(params.get("filename") or "upload.bin").strip()[:255]
        file_id = str(params.get("file_id") or "").strip()[:128]
        try:
            size = int(params.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        session_key = str(params.get("session_key") or "").strip()
    else:
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Request body must be valid JSON")
        if not isinstance(body, dict):
            return _json_error(400, "Request body must be a JSON object")
        url = str(body.get("url") or "").strip()
        filename = str(body.get("filename") or "upload.bin").strip()[:255]
        file_id = str(body.get("file_id") or "").strip()[:128]
        try:
            size = int(body.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        session_key = str(body.get("session_key") or "").strip()

    if not is_gofile_url(url):
        return _json_error(400, "url must be an HTTPS gofile.io share/page URL")
    if size < 0 or size > (200 * 1024 * 1024 * 1024):
        return _json_error(400, "size is outside the supported upload range")
    session_key = session_key or "unknown"

    record = upload_store.record(
        session_key,
        url=url,
        file_id=file_id,
        filename=filename,
        size=size,
    )
    logger.info(
        "Mini App upload received session={} file={} size={} url={}",
        session_key, filename, size, url,
    )
    # Fire-and-forget push of the gofile link into the user's private chat so
    # they immediately see a ready-to-use link and can type their request.
    if session_key not in ("", "unknown"):
        try:
            asyncio.ensure_future(
                _push_link_to_telegram(url, filename, session_key)
            )
        except RuntimeError:  # no event loop running
            logger.debug("No event loop to push gofile link for {}", url)
    return web.json_response({
        "ok": True,
        "stored": True,
        "url": record.url,
        "filename": record.filename,
    })


# ---------------------------------------------------------------------------
# Mini App HTML (single self-contained file)
# ---------------------------------------------------------------------------

MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>Upload Large File</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --accent:#f59e0b; --text:#e2e8f0; --muted:#94a3b8; }
  * { box-sizing:border-box; }
  html,body { margin:0; padding:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { max-width:440px; margin:0 auto; padding:24px 16px 40px; }
  h1 { font-size:20px; margin:0 0 6px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 20px; line-height:1.5; }
  .drop { border:2px dashed #475569; border-radius:14px; padding:32px 16px; text-align:center;
    cursor:pointer; transition:border-color .15s, background .15s; }
  .drop:hover, .drop.drag { border-color:var(--accent); background:#1e293b; }
  .drop .big { font-size:15px; font-weight:600; }
  .drop .small { color:var(--muted); font-size:12px; margin-top:6px; }
  input[type=file] { display:none; }
  .status { margin-top:18px; border-radius:12px; padding:14px; background:#1e293b; font-size:13px; line-height:1.5; }
  .status .row { display:flex; justify-content:space-between; gap:10px; margin-bottom:6px; }
  .status .k { color:var(--muted); }
  .bar { height:8px; border-radius:99px; background:#334155; overflow:hidden; margin-top:10px; }
  .bar > i { display:block; height:100%; width:0; background:var(--accent); transition:width .15s; }
  .urlbox { margin-top:14px; }
  .urlbox label { font-size:12px; color:var(--muted); display:block; margin-bottom:6px; }
  .urlbox input { width:100%; border:1px solid #334155; background:#0f172a; color:var(--text);
    border-radius:8px; padding:10px; font-size:13px; }
  .btn { margin-top:14px; width:100%; border:0; border-radius:10px; padding:13px;
    background:var(--accent); color:#0f172a; font-weight:700; font-size:14px; cursor:pointer; }
  .btn:disabled { opacity:.5; cursor:not-allowed; }
  .hidden { display:none; }
  a.copy { color:var(--accent); cursor:pointer; text-decoration:underline; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📤 Upload Large File</h1>
  <p class="sub">Telegram buttons cap out around 50&nbsp;MB. Use this Mini App to upload files up to
    2&nbsp;GB directly to gofile.io, then ask the bot to analyze it.</p>

  <div class="drop" id="drop">
    <div class="big">Tap to choose a file</div>
    <div class="small">or drag &amp; drop it here · up to 2&nbsp;GB</div>
  </div>
  <input type="file" id="file" />
  <button class="btn hidden" id="pick">Choose File</button>

  <div class="status hidden" id="status">
    <div class="row"><span class="k">File</span><span id="fname">—</span></div>
    <div class="row"><span class="k">Size</span><span id="fsize">—</span></div>
    <div class="row"><span class="k">Progress</span><span id="fprog">0%</span></div>
    <div class="bar"><i id="fbar"></i></div>
  </div>

  <div class="urlbox hidden" id="urlbox">
    <label>Public download URL (share this with the bot)</label>
    <input id="url" readonly />
    <button class="btn" id="copylink">Copy Link</button>
  </div>

  <div class="urlbox hidden" id="next">
    <label>Next step</label>
    <p class="sub" style="margin:4px 0 0;">Go back to the chat and send a message like
      <b>analyze the file I just uploaded</b>. The agent will pull it from gofile.io.</p>
  </div>
</div>

<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
(function () {
  'use strict';
  var app = window.Telegram && window.Telegram.WebApp;
  if (app) {
    app.ready();
    app.expand();
    app.setHeaderColor('#0f172a');
    app.setBackgroundColor('#0f172a');
  }

  var drop = document.getElementById('drop');
  var fileInput = document.getElementById('file');
  var status = document.getElementById('status');
  var fname = document.getElementById('fname');
  var fsize = document.getElementById('fsize');
  var fprog = document.getElementById('fprog');
  var fbar = document.getElementById('fbar');
  var urlbox = document.getElementById('urlbox');
  var urlInput = document.getElementById('url');
  var nextBox = document.getElementById('next');

  var sessionKey = 'unknown';
  if (app && app.initDataUnsafe && app.initDataUnsafe.user) {
    sessionKey = String(app.initDataUnsafe.user.id);
  }
  var query = new URLSearchParams(window.location.search);
  if (query.get('session')) sessionKey = query.get('session');

  function fmtSize(n) {
    if (!n || n <= 0) return '?';
    if (n >= 1073741824) return (n/1073741824).toFixed(2) + ' GB';
    if (n >= 1048576) return (n/1048576).toFixed(1) + ' MB';
    return Math.max(1, Math.round(n/1024)) + ' KB';
  }

  function setProgress(pct) {
    fprog.textContent = pct + '%';
    fbar.style.width = pct + '%';
  }

  drop.addEventListener('click', function () { fileInput.click(); });
  drop.addEventListener('dragover', function (e) { e.preventDefault(); drop.classList.add('drag'); });
  drop.addEventListener('dragleave', function () { drop.classList.remove('drag'); });
  drop.addEventListener('drop', function (e) {
    e.preventDefault();
    drop.classList.remove('drag');
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      fileInput.files = e.dataTransfer.files;
      handleFile(fileInput.files[0]);
    }
  });
  fileInput.addEventListener('change', function () {
    if (fileInput.files && fileInput.files.length) handleFile(fileInput.files[0]);
  });

  function handleFile(file) {
    if (!file) return;
    status.classList.remove('hidden');
    urlbox.classList.add('hidden');
    nextBox.classList.add('hidden');
    fname.textContent = file.name;
    fsize.textContent = fmtSize(file.size);
    setProgress(0);
    if (file.size > 2 * 1024 * 1024 * 1024) {
      setProgress(0);
      fprog.textContent = 'too large';
      fprog.style.color = '#f87171';
      return;
    }
    uploadToGofile(file);
  }

  function uploadToGofile(file) {
    fetch('https://api.gofile.io/servers', { method: 'GET' })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        var server = (res && res.data && res.data.servers && res.data.servers[0])
          ? res.data.servers[0].name : null;
        if (!server) throw new Error('No gofile server available');
        return server;
      })
      .then(function (server) {
        return new Promise(function (resolve, reject) {
          var xhr = new XMLHttpRequest();
          xhr.open('POST', 'https://' + server + '.gofile.io/contents/uploadfile', true);
          xhr.upload.onprogress = function (e) {
            if (e.lengthComputable) setProgress(Math.round(e.loaded / e.total * 100));
          };
          xhr.onload = function () { resolve(xhr); };
          xhr.onerror = function () { reject(new Error('Upload request failed')); };
          xhr.onabort = function () { reject(new Error('Upload aborted')); };
          var form = new FormData();
          form.append('file', file);
          xhr.send(form);
        });
      })
      .then(function (xhr) {
        var res = JSON.parse(xhr.responseText || '{}');
        if (xhr.status < 200 || xhr.status >= 300 || !res || res.status !== 'ok') {
          var msg = res && res.data && res.data ? (res.data.error || JSON.stringify(res.data)) : 'Upload failed';
          throw new Error(msg);
        }
        var data = res.data || {};
        var pageUrl = data.downloadPage || ('https://gofile.io/d/' + data.fileId);
        complete(pageUrl, data.fileId, file);
      })
      .catch(function (err) {
        fprog.textContent = 'error';
        fprog.style.color = '#f87171';
      });
  }

  function complete(pageUrl, fileId, file) {
    setProgress(100);
    urlInput.value = pageUrl;
    urlbox.classList.remove('hidden');
    nextBox.classList.remove('hidden');
    var params = new URLSearchParams({
      url: pageUrl,
      file_id: fileId || '',
      filename: file.name,
      size: String(file.size || 0),
      session_key: sessionKey
    });
    // Best-effort beacon. GET is used so the callback works on both the API
    // server (aiohttp) and the gateway (which only exposes query parameters).
    fetch('/upload/complete?' + params.toString(), { method: 'GET', mode: 'no-cors' }).catch(function () {});
  }

  document.getElementById('copylink').addEventListener('click', function () {
    urlInput.select();
    try { document.execCommand('copy'); } catch (e) {}
    if (navigator.clipboard) navigator.clipboard.writeText(urlInput.value);
  });
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "invalid_request_error", "code": status}},
        status=status,
    )


def register_miniapp_routes(app: web.Application) -> None:
    """Add the Mini App GET and completion routes to *app*."""
    app.router.add_get("/upload", handle_miniapp_page)
    app.router.add_route("*", "/upload/complete", handle_upload_complete)


# Keep the uuid import used by callers that construct mini-app session id values.
def build_session_id() -> str:
    return f"mapp-{uuid.uuid4().hex[:12]}"
