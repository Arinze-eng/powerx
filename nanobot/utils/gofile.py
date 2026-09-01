"""GoFile share resolution and download helpers.

GoFile share pages (``https://gofile.io/d/<code>``) return an HTML landing
page rather than the actual file, so a naive HTTP GET cannot be used to fetch
an upload. To download a file from a GoFile link you must:

1. create a temporary guest account token,
2. resolve the share via the contents API (which requires a rolling
   ``X-Website-Token`` header derived from the account token),
3. read the real direct-download ``link`` from the resolved children, and
4. fetch / redirect to that direct link.

These helpers wrap that flow with ``aiohttp`` so both the VPS execution backend
and the Novita sandbox backend can download GoFile uploads reliably. No user
API token is required.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from loguru import logger

GOFILE_API = "https://api.gofile.io"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
LANGUAGE = "en-US"
# Current GoFile website-token salt. GoFile may rotate this in the future.
WEBSITE_SALT = "12af056dacea0b"

_DEFAULT_TIMEOUT_SECONDS = 120
_CODE_RE = re.compile(r"[A-Za-z0-9_-]{6,32}")


class GoFileError(RuntimeError):
    """Raised when GoFile rejects a request or a link cannot be resolved."""


def is_gofile_url(url: str) -> bool:
    """Return True when *url* points at a gofile.io share/download resource."""
    parsed = urlparse(str(url or "").strip())
    host = (parsed.netloc or "").strip().lower()
    return host == "gofile.io" or host.endswith(".gofile.io")


def extract_gofile_code(url: str) -> str:
    """Extract the share ``/d/<code>`` identifier from a GoFile link."""
    value = str(url or "").strip()
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    lower = [part.lower() for part in parts]
    if "d" not in lower:
        raise GoFileError("GoFile link must look like https://gofile.io/d/<code>")
    code = parts[lower.index("d") + 1] if lower.index("d") + 1 < len(parts) else ""
    if not _CODE_RE.fullmatch(code):
        raise GoFileError("The GoFile share code is invalid.")
    return code


def _website_token(account_token: str, offset: int = 0) -> str:
    window = int(time.time() // 14400) + offset
    raw = f"{USER_AGENT}::{LANGUAGE}::{account_token}::{window}::{WEBSITE_SALT}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def _create_guest_token(session: aiohttp.ClientSession) -> str:
    try:
        async with session.post(
            f"{GOFILE_API}/accounts",
            headers={
                "User-Agent": USER_AGENT,
                "Origin": "https://gofile.io",
                "Accept": "application/json",
            },
        ) as response:
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as exc:
        raise GoFileError(f"could not reach GoFile API: {type(exc).__name__}") from None
    if payload.get("status") != "ok":
        raise GoFileError("could not create a temporary GoFile guest session")
    token = (payload.get("data") or {}).get("token")
    if not token:
        raise GoFileError("GoFile returned no guest token")
    return str(token)


async def _resolve_share(session: aiohttp.ClientSession, code: str, token: str) -> dict[str, Any]:
    query = "contentFilter=&page=1&pageSize=1000&sortField=createTime&sortDirection=-1"
    url = f"{GOFILE_API}/contents/{code}?{query}"
    last_status = "unknown error"
    for offset in (0, -1):
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Website-Token": _website_token(token, offset),
            "X-BL": LANGUAGE,
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Origin": "https://gofile.io",
            "Referer": "https://gofile.io/",
        }
        try:
            async with session.get(url, headers=headers) as response:
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as exc:
            raise GoFileError(f"could not reach GoFile API: {type(exc).__name__}") from None
        status = payload.get("status")
        if status == "ok":
            return payload.get("data") or {}
        last_status = status or last_status
        if status == "error-rateLimit":
            await asyncio.sleep(3)
    raise GoFileError(f"GoFile could not resolve this link: {last_status}")


async def resolve_gofile_download(
    url: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, str]]:
    """Resolve a GoFile share into a list of direct-download descriptors.

    Each descriptor contains ``name`` (the real file name), ``link`` (a direct
    download URL), and ``size`` (the file size in bytes, when reported).
    """
    code = extract_gofile_code(url)
    timeout = aiohttp.ClientTimeout(total=max(30, min(int(timeout_seconds), 300)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = await _create_guest_token(session)
        data = await _resolve_share(session, code, token)
    children = data.get("children") or {}
    files: list[dict[str, Any]] = []
    if children:
        files = list(children.values())
    else:
        files = [data]
    resolved: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "file":
            continue
        link = str(item.get("link") or "").strip()
        if not link:
            continue
        name = str(item.get("name") or "gofile_file").strip() or "gofile_file"
        size = item.get("size")
        resolved.append(
            {
                "name": name,
                "link": link,
                "size": str(int(size)) if isinstance(size, (int, float)) else str(size or ""),
            }
        )
    if not resolved:
        raise GoFileError(
            "no downloadable files found; the link may be expired, private, or unavailable"
        )
    logger.debug("Resolved GoFile share to {} downloadable file(s)", len(resolved))
    return resolved


async def download_gofile(
    url: str,
    destination: str | Path,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Resolve a single-file GoFile share and download it to *destination*.

    Returns the absolute destination path. Only the first downloadable file is
    fetched; folders/multi-file shares should use :func:`resolve_gofile_download`.
    """
    items = await resolve_gofile_download(url, timeout_seconds=timeout_seconds)
    if not items:
        raise GoFileError("GoFile share contained no downloadable files")
    item = items[0]
    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=max(30, min(int(timeout_seconds), 300)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                item["link"],
                headers={"User-Agent": USER_AGENT, "Referer": "https://gofile.io/"},
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise GoFileError(f"GoFile download failed with HTTP {response.status}")
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type and int(item["size"] or 0) == 0:
                    raise GoFileError("GoFile download resolved to an HTML page")
                target.write_bytes(await response.read())
    except aiohttp.ClientError as exc:
        raise GoFileError(f"GoFile download failed: {type(exc).__name__}") from None
    return str(target)
