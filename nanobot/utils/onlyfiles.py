"""Minimal onlyfiles.com transfer helpers for VPS input and artifact delivery.

Replaces the previous tmpfiles.org integration. onlyfiles exposes the *same*
endpoint shape — ``POST /api/v1/upload`` with a single ``file`` multipart field —
but its success payload differs slightly, so this module parses it here and hands
callers the exact same return contract the rest of the codebase already expects:
``{"url": <page url>, "download_url": <direct link>}``.

Response shape observed from ``https://onlyfiles.com/api/v1/upload``::

    {
      "status": true,
      "data": {
        "file": {
          "url": {"full": "https://onlyfiles.com/<id>/<name>",
                   "short": "https://onlyfiles.com/<id>"},
          "metadata": {"id": "<id>", "name": "<name>", "size": {...}}
        }
      }
    }

The page URL (``url.full``) is the canonical public link. onlyfiles has no clean
raw-bytes download route for slug ids (its ``/download`` endpoint redirects back
to the HTML page), so ``download_url`` equals the page URL — mirroring how the old
code treated opaque-slug tmpfiles links.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

ONLYFILES_UPLOAD_URL = "https://onlyfiles.com/api/v1/upload"
ONLYFILES_HOST = "onlyfiles.com"
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # onlyfiles hard limit (~100 MiB)
_DEFAULT_TIMEOUT_SECONDS = 90


class OnlyFilesError(RuntimeError):
    """Raised when onlyfiles.com cannot accept or describe a transfer."""


def _public_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != ONLYFILES_HOST or not parsed.path:
        raise OnlyFilesError("onlyfiles returned an invalid public URL")
    return url


def _extract_page_url(payload: dict[str, Any]) -> str:
    """Pull the file's public page URL out of the onlyfiles response body."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise OnlyFilesError("onlyfiles returned no upload metadata")
    file_obj = data.get("file")
    if not isinstance(file_obj, dict):
        raise OnlyFilesError("onlyfiles returned no file object")
    urls = file_obj.get("url")
    if not isinstance(urls, dict):
        raise OnlyFilesError("onlyfiles returned no url object")
    # Prefer the full (filename-bearing) link; fall back to the short id link.
    page_url = _public_url(urls.get("full") or urls.get("short"))
    return page_url


async def upload_bytes(
    data: bytes,
    *,
    filename: str,
    content_type: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Upload one bounded file and return its page and direct-download URLs."""
    if not data:
        raise OnlyFilesError("cannot upload an empty file")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise OnlyFilesError("file exceeds the onlyfiles transfer limit")
    safe_filename = Path(filename).name or "upload.bin"
    timeout = aiohttp.ClientTimeout(total=max(10, min(int(timeout_seconds), 180)))
    form = aiohttp.FormData()
    form.add_field(
        "file",
        data,
        filename=safe_filename,
        content_type=content_type or "application/octet-stream",
    )
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(ONLYFILES_UPLOAD_URL, data=form) as response:
                raw = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise OnlyFilesError(f"onlyfiles upload failed with HTTP {response.status}")
    except aiohttp.ClientError as exc:
        raise OnlyFilesError(f"onlyfiles upload request failed: {type(exc).__name__}") from None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise OnlyFilesError("onlyfiles returned invalid JSON") from None
    if not isinstance(payload, dict) or payload.get("status") is not True:
        raise OnlyFilesError("onlyfiles did not accept the upload")
    page_url = _extract_page_url(payload)
    # No separate raw-download route exists for slug links; the page URL is the link.
    return {"url": page_url, "download_url": page_url}


async def upload_path(
    path: str | Path,
    *,
    content_type: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Upload a local file without exposing its path to the remote service."""
    source = Path(path).expanduser()
    try:
        size = source.stat().st_size
        if size <= 0:
            raise OnlyFilesError("cannot upload an empty file")
        if size > _MAX_UPLOAD_BYTES:
            raise OnlyFilesError("file exceeds the onlyfiles transfer limit")
        data = source.read_bytes()
    except OSError as exc:
        raise OnlyFilesError(f"could not read upload file: {type(exc).__name__}") from None
    return await upload_bytes(
        data,
        filename=source.name,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )
