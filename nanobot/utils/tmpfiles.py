"""Minimal tmpfiles.org transfer helpers for VPS input and artifact delivery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

TMPFILES_UPLOAD_URL = "https://tmpfiles.org/api/v1/upload"
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 90


class TmpfilesError(RuntimeError):
    """Raised when tmpfiles.org cannot accept or describe a transfer."""


def _public_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "tmpfiles.org" or not parsed.path:
        raise TmpfilesError("tmpfiles returned an invalid public URL")
    return url


def _download_url(page_url: str) -> str:
    """Return a direct link for legacy numeric IDs, otherwise the valid file page."""
    parsed = urlparse(page_url)
    path = parsed.path
    if not path.startswith("/"):
        path = "/" + path
    segments = [segment for segment in path.split("/") if segment]
    if segments and segments[0].isdigit():
        return f"https://tmpfiles.org/dl{path}"
    # Current tmpfiles responses may use opaque slugs whose /dl route redirects
    # back to the HTML file page. The page URL remains the valid public link.
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
        raise TmpfilesError("cannot upload an empty file")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise TmpfilesError("file exceeds the tmpfiles transfer limit")
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
            async with session.post(TMPFILES_UPLOAD_URL, data=form) as response:
                raw = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise TmpfilesError(f"tmpfiles upload failed with HTTP {response.status}")
    except aiohttp.ClientError as exc:
        raise TmpfilesError(f"tmpfiles upload request failed: {type(exc).__name__}") from None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise TmpfilesError("tmpfiles returned invalid JSON") from None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise TmpfilesError("tmpfiles did not accept the upload")
    data_payload = payload.get("data")
    if not isinstance(data_payload, dict):
        raise TmpfilesError("tmpfiles returned no upload metadata")
    page_url = _public_url(data_payload.get("url"))
    return {"url": page_url, "download_url": _download_url(page_url)}


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
            raise TmpfilesError("cannot upload an empty file")
        if size > _MAX_UPLOAD_BYTES:
            raise TmpfilesError("file exceeds the tmpfiles transfer limit")
        data = source.read_bytes()
    except OSError as exc:
        raise TmpfilesError(f"could not read upload file: {type(exc).__name__}") from None
    return await upload_bytes(
        data,
        filename=source.name,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )
