"""Minimal catbox.to transfer helpers for Mini App uploads and agent fetches."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

# catbox.to is the upload/download host used by the Telegram Mini App for files
# that are too large to transfer through the bot (100 MB-plus range).
CATBOX_DOMAIN = "catbox.to"
CATBOX_DOMAINS = {CATBOX_DOMAIN, "www.catbox.to"}
CATBOX_API_BASE = f"https://{CATBOX_DOMAIN}"
# ``*.catbox.to`` serves file pages and CDN content as well.
_CATBOX_SUFFIX = ".catbox.to"

UPLOAD_URL = f"{CATBOX_API_BASE}/upload"

_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # Mini App advertises "up to 2 GB"
_DEFAULT_TIMEOUT_SECONDS = 600
_CSRF_META_RE = re.compile(r'csrf-token"\s+content="([^"]+)"', re.IGNORECASE)

# Accepted retention values for a public share: 0 keeps the file indefinitely,
# otherwise the value is interpreted as days (1, 2, 3, 4, 5, 7, 14) and the
# file is auto-deleted after that window.
_DEFAULT_AUTO_DELETE = 0


class CatboxError(RuntimeError):
    """Raised when catbox.to cannot accept, describe, or deliver a transfer."""


def _is_catbox_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in CATBOX_DOMAINS:
        return True
    return host.endswith(_CATBOX_SUFFIX)


def is_catbox_url(value: str) -> bool:
    """Return True when *value* is an HTTPS share/file URL on catbox.to."""
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and _is_catbox_host(parsed.netloc) and bool(parsed.path)


def is_catbox_download_url(value: str) -> bool:
    """Return True when *value* is an HTTPS raw ``/download/<id>/<hash>/<name>`` URL."""
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or not _is_catbox_host(parsed.netloc):
        return False
    segments = [segment for segment in parsed.path.split("/") if segment]
    # /download/<shared_id>/<userhash>/<filename>
    return segments[:1] == ["download"] and len(segments) >= 4


def shared_id_from_url(value: str) -> str | None:
    """Extract a catbox.to ``shared_id`` from a bare ``/<id>`` or ``/<id>/file`` URL."""
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or not _is_catbox_host(parsed.netloc):
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return None
    if segments[0] == "download":
        # download/<shared_id>/<userhash>/<filename>
        return segments[1] if len(segments) >= 2 else None
    return segments[0]


async def _fetch_csrf(session: aiohttp.ClientSession) -> str:
    """Open the catbox.to homepage so the session cookie is set, then return CSRF.

    aiohttp stores the ``XSRF-TOKEN``/``session`` cookies from the response in
    the session's cookie jar and re-sends them on the next request to catbox.to,
    so the returned CSRF token pairs with the session established here.
    """
    async with session.get(CATBOX_API_BASE) as response:
        if response.status < 200 or response.status >= 300:
            raise CatboxError(f"catbox homepage request failed with HTTP {response.status}")
        html = await response.text()
    match = _CSRF_META_RE.search(html)
    if not match:
        raise CatboxError("catbox.to did not issue a CSRF token")
    return match.group(1)


async def upload_bytes(
    data: bytes,
    *,
    filename: str,
    content_type: str | None = None,
    keep_for_days: int = _DEFAULT_AUTO_DELETE,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Upload one bounded file and return its catbox share URL and metadata.

    catbox.to/CORS and the per-session CSRF token prevent a browser from
    uploading directly, so the caller (usually the API server) performs the
    upload on behalf of the client and exposes the resulting share URL.
    """
    if not data:
        raise CatboxError("cannot upload an empty file")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise CatboxError("file exceeds the catbox upload limit")
    safe_filename = Path(filename).name or "upload.bin"
    timeout = aiohttp.ClientTimeout(total=max(30, min(int(timeout_seconds), 1800)))
    form = aiohttp.FormData()
    form.add_field(
        "file",
        data,
        filename=safe_filename,
        content_type=content_type or "application/octet-stream",
    )
    form.add_field("upload_auto_delete", str(int(keep_for_days)))
    form.add_field("size", str(len(data)))

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            csrf = await _fetch_csrf(session)
            headers = {"X-CSRF-TOKEN": csrf}
            async with session.post(UPLOAD_URL, data=form, headers=headers) as response:
                raw = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise CatboxError(f"catbox upload failed with HTTP {response.status}")
    except aiohttp.ClientError as exc:
        raise CatboxError(f"catbox upload request failed: {type(exc).__name__}") from None

    payload = _json_payload(raw)
    if payload.get("type") != "success" or not payload.get("download_link"):
        raise CatboxError("catbox did not accept the upload")
    shared_id = str(payload.get("shared_id") or "").strip()
    download_link = str(payload.get("download_link") or "").strip()
    if not is_catbox_url(download_link) or not shared_id:
        raise CatboxError("catbox returned an invalid share URL")
    return {
        "url": download_link,
        "shared_id": shared_id,
        "file_name": str(payload.get("file_name") or safe_filename),
    }


async def upload_path(
    path: str | Path,
    *,
    content_type: str | None = None,
    keep_for_days: int = _DEFAULT_AUTO_DELETE,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Upload a local file without exposing its path to the remote service."""
    source = Path(path).expanduser()
    try:
        size = source.stat().st_size
        if size <= 0:
            raise CatboxError("cannot upload an empty file")
        if size > _MAX_UPLOAD_BYTES:
            raise CatboxError("file exceeds the catbox upload limit")
        data = source.read_bytes()
    except OSError as exc:
        raise CatboxError(f"could not read upload file: {type(exc).__name__}") from None
    return await upload_bytes(
        data,
        filename=source.name,
        content_type=content_type,
        keep_for_days=keep_for_days,
        timeout_seconds=timeout_seconds,
    )


async def resolve_raw_url(
    share_url: str,
    *,
    timeout_seconds: int = 60,
) -> str:
    """Turn a catbox ``/<id>/file`` share URL into a direct raw download URL.

    catbox serves an HTML download page (with a per-session CSRF token) at the
    share URL; the raw bytes are only reachable through the ``/download/create``
    endpoint, which returns a ``/download/<id>/<userhash>/<filename>`` link that
    streams the file directly. Follow that two-step flow here.
    """
    shared_id = shared_id_from_url(share_url)
    if not shared_id:
        raise CatboxError("not a valid catbox.to share URL")
    timeout = aiohttp.ClientTimeout(total=max(20, min(int(timeout_seconds), 180)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{CATBOX_API_BASE}/{shared_id}/file") as response:
                if response.status < 200 or response.status >= 300:
                    raise CatboxError(
                        f"catbox share page request failed with HTTP {response.status}"
                    )
                html = await response.text()
            csrf = _CSRF_META_RE.search(html)
            if not csrf:
                raise CatboxError("catbox share page did not issue a CSRF token")
            async with session.post(
                f"{CATBOX_API_BASE}/{shared_id}/download/create",
                headers={"X-CSRF-TOKEN": csrf.group(1)},
            ) as response:
                raw = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise CatboxError(
                        f"catbox download link request failed with HTTP {response.status}"
                    )
    except aiohttp.ClientError as exc:
        raise CatboxError(f"catbox download link request failed: {type(exc).__name__}") from None

    payload = _json_payload(raw)
    download_link = str((payload.get("download_link") or "").strip())
    if payload.get("type") != "success" or not is_catbox_download_url(download_link):
        raise CatboxError("catbox did not issue a raw download link")
    return download_link


async def download_bytes(
    share_url: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Fetch the raw bytes of a catbox share, following the /download/create step."""
    raw_url = await resolve_raw_url(share_url, timeout_seconds=timeout_seconds)
    timeout = aiohttp.ClientTimeout(total=max(30, min(int(timeout_seconds), 1800)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(raw_url, allow_redirects=True) as response:
                if response.status < 200 or response.status >= 300:
                    raise CatboxError(f"catbox download failed with HTTP {response.status}")
                content_type = response.headers.get("Content-Type", "")
                if "text/html" in content_type.lower():
                    raise CatboxError("catbox download resolved to an HTML page")
                return await response.read()
    except aiohttp.ClientError as exc:
        raise CatboxError(f"catbox download failed: {type(exc).__name__}") from None


def _json_payload(raw: str) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise CatboxError("catbox returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise CatboxError("catbox returned an unexpected response shape")
    return payload
