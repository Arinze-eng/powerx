"""Size-aware artifact sharing for finished agent outputs.

When the agent produces a file (PDF, image, archive, dataset…) the user needs a
link they can actually open — not a raw path inside an ephemeral sandbox. Different
free hosts fit different sizes:

* ``onlyfiles.com`` — fast, tiny API, hard-caps at ~100 MiB; the page URL is
  permanent (uploads use expire=0) while raw /dl/ tokens are re-minted at tap time.
* ``catbox.moe``    — accepts up to ~200 MiB and stores files permanently.

This module picks the right host from the file size, uploads once, and returns a
uniform result so callers don't have to think about which provider was used. The
routing is deliberately conservative: small files go to onlyfiles; anything over the
100 MiB threshold goes straight to catbox; mid-sized files try onlyfiles first and
transparently fall back to catbox if onlyfiles rejects them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp

from nanobot.utils.onlyfiles import OnlyFilesError, upload_bytes as _onlyfiles_upload_bytes

# Routing thresholds (bytes).
_ONLYFILES_MAX_BYTES = 100 * 1024 * 1024        # onlyfiles hard limit (~100 MiB)
_CATBOX_THRESHOLD_BYTES = 100 * 1024 * 1024     # >100 MiB → always catbox
_CATBOX_MAX_BYTES = 200 * 1024 * 1024          # catbox practical ceiling (~200 MiB)

CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
_DEFAULT_TIMEOUT_SECONDS = 180


class FileShareError(RuntimeError):
    """Raised when no configured host can accept/describe the upload."""


def _normalize_onlyfiles(result: dict[str, str]) -> dict[str, Any]:
    return {
        "url": result.get("download_url") or result.get("url"),
        "page_url": result.get("url"),
        "host": "onlyfiles",
    }


def _normalize_catbox(url: str) -> dict[str, Any]:
    return {"url": url, "page_url": url, "host": "catbox"}


async def _upload_onlyfiles(
    data: bytes, *, filename: str, content_type: str | None, timeout_seconds: int
) -> dict[str, Any]:
    result = await _onlyfiles_upload_bytes(
        data, filename=filename, content_type=content_type, timeout_seconds=timeout_seconds
    )
    return _normalize_onlyfiles(result)


async def _upload_catbox(
    data: bytes,
    *,
    filename: str,
    content_type: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    if len(data) > _CATBOX_MAX_BYTES:
        raise FileShareError("file exceeds the catbox transfer limit (~200 MiB)")
    safe_filename = Path(filename).name or "upload.bin"
    timeout = aiohttp.ClientTimeout(total=max(30, min(int(timeout_seconds), 600)))
    form = aiohttp.FormData()
    form.add_field(
        "reqtype",
        "fileupload",
    )
    form.add_field(
        "fileToUpload",
        data,
        filename=safe_filename,
        content_type=content_type or "application/octet-stream",
    )
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(CATBOX_UPLOAD_URL, data=form) as response:
                text = (await response.text()).strip()
                if response.status < 200 or response.status >= 300:
                    raise FileShareError(f"catbox upload failed with HTTP {response.status}")
    except aiohttp.ClientError as exc:
        raise FileShareError(f"catbox upload request failed: {type(exc).__name__}") from None
    # catbox returns plain-text on success ("https://files.catbox.moe/xxxx.ext")
    # and either empty or an error string otherwise.
    if not text.startswith("https://"):
        raise FileShareError("catbox did not return a valid URL")
    return _normalize_catbox(text)


async def upload_artifact_bytes(
    data: bytes,
    *,
    filename: str,
    content_type: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Upload one artifact and return ``{url, page_url, host}``.

    Chooses the host by size: <100 MiB → onlyfiles; >100 MiB → catbox; if
    onlyfiles rejects a mid-size file it falls back to catbox. Raises
    :class:`FileShareError` if neither host accepts it.
    """
    if not data:
        raise FileShareError("cannot upload an empty file")
    size = len(data)
    name = Path(filename).name or "upload.bin"

    if size > _CATBOX_THRESHOLD_BYTES:
        return await _upload_catbox(
            data, filename=name, content_type=content_type, timeout_seconds=timeout_seconds
        )

    if size <= _ONLYFILES_MAX_BYTES:
        try:
            return await _upload_onlyfiles(
                data, filename=name, content_type=content_type, timeout_seconds=timeout_seconds
            )
        except OnlyFilesError:
            # Fall through to catbox for any onlyfiles rejection.
            pass

    # Mid-size (>100 MiB or onlyfiles rejected): use catbox.
    return await _upload_catbox(
        data, filename=name, content_type=content_type, timeout_seconds=timeout_seconds
    )


async def upload_artifact_path(
    path: str | Path,
    *,
    content_type: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Upload a local file by path without exposing its location to the host."""
    source = Path(path).expanduser()
    try:
        size = source.stat().st_size
        if size <= 0:
            raise FileShareError("cannot upload an empty file")
        data = source.read_bytes()
    except OSError as exc:
        raise FileShareError(f"could not read upload file: {type(exc).__name__}") from None
    return await upload_artifact_bytes(
        data,
        filename=source.name,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )
