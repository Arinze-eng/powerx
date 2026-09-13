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

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from loguru import logger

from nanobot.config.paths import get_persistent_data_dir

ONLYFILES_UPLOAD_URL = "https://onlyfiles.com/api/v1/upload"
ONLYFILES_HOST = "onlyfiles.com"
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # onlyfiles hard limit (~100 MiB)
_DEFAULT_TIMEOUT_SECONDS = 90
#: Uploads are permanent: expiry 0 means the file NEVER expires, so the
#: returned URL stays valid forever and can be stored and re-referenced by
#: the AI at any time without re-uploading the same bytes.
_ONLYFILES_EXPIRY = 0


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
    # expiry=0: no expiry — the upload is permanent so the URL can be stored
    # in persistent memory and referenced by the AI indefinitely.
    form.add_field("expiry", str(_ONLYFILES_EXPIRY))
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


class UploadedUrlMemory:
    """Persistent content-hash -> uploaded-URL store under the data dir.

    Because uploads have NO expiry, the URL of an already-uploaded file is a
    permanent fact: a re-request for the same bytes can return the stored
    URL instead of paying for a second upload (and onlyfiles keeps one
    canonical copy instead of accumulating duplicates).
    """

    def __init__(self, root: Path | None = None) -> None:
        self._path = (root or get_persistent_data_dir("onlyfiles")) / "uploads.json"
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.is_file():
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self._data = payload
        except (OSError, ValueError, TypeError):
            self._data = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("uploaded-url memory save failed: {}", exc)

    @staticmethod
    def content_fingerprint(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def lookup(self, data: bytes) -> dict[str, str] | None:
        """Return the remembered URLs for this exact content, if any."""
        self._load()
        entry = self._data.get(self.content_fingerprint(data))
        if not entry:
            return None
        urls = entry.get("urls")
        if isinstance(urls, dict) and urls.get("url"):
            return {"url": urls["url"], "download_url": urls.get("download_url", urls["url"])}
        return None

    def remember(self, data: bytes, filename: str, urls: dict[str, str]) -> bool:
        """Persist the URL pair for uploaded content. Never raises."""
        self._load()
        self._data[self.content_fingerprint(data)] = {
            "filename": filename,
            "urls": urls,
            "ts": time.time(),
        }
        self._save()
        return True


async def upload_and_remember(
    path: str | Path,
    *,
    content_type: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    url_memory: UploadedUrlMemory | None = None,
) -> dict[str, str]:
    """Upload with expiry=0 and persist the URL for zero-reupload recall.

    Same content uploaded again (in this process or after a restart — the
    store lives on the Northflank persistent disk) returns the remembered
    URL without a network call, so the AI can always reference prior
    uploads. Raises OnlyFilesError exactly like ``upload_path`` on failure.
    """
    source = Path(path).expanduser()
    try:
        data = source.read_bytes()
        if not data:
            raise OnlyFilesError("cannot upload an empty file")
    except OSError as exc:
        raise OnlyFilesError(f"could not read upload file: {type(exc).__name__}") from None
    memory = url_memory or UploadedUrlMemory()
    remembered = memory.lookup(data)
    if remembered:
        logger.info("onlyfiles: reusing stored URL for {}", source.name)
        return remembered
    result = await upload_path(
        path,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )
    memory.remember(data, source.name, result)
    return result
