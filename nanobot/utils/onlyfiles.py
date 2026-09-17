"""Minimal onlyfiles.com transfer helpers for VPS input and artifact delivery.

Implements the documented onlyfiles API (https://onlyfiles.com/api):

* ``POST https://api.onlyfiles.com/v1/upload`` — multipart upload with a
  single ``file`` field and an ``expire`` field (seconds 60-172800, or
  ``0`` to keep the file forever; default 86400 = 24h).
* ``GET https://api.onlyfiles.com/v1/file/{id}/info`` — liveness/metadata
  probe (HTTP 404 + ``status: false`` for a missing file).

This module parses the success payload and hands callers the exact same
return contract the rest of the codebase already expects:
``{"url": <page url>, "download_url": <direct link>}``.

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

The page URL (``url.full``) is the canonical, PERMANENT public link (uploads use
``expire=0``), but it serves an HTML viewer — tapping it opens a web page, not a
download. The raw bytes live under ``/dl/<ts.nonce>/<id>/<file>``, with the
nonce embedded in the viewer HTML, and that raw token EXPIRES after roughly two
hours (verified against the live service). So: ``download_url`` is a freshly
minted raw link that downloads on tap right now, ``url`` is the permanent page
link to store/share, and :func:`resolve_raw_url` re-mints a raw link at the
moment of delivery — never persist a raw link.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from loguru import logger

from nanobot.config.paths import get_persistent_data_dir

ONLYFILES_UPLOAD_URL = "https://api.onlyfiles.com/v1/upload"
# Raw bytes live under /dl/<ts.nonce>/<id>/<file>; the token is embedded in the
# viewer page HTML for the slug URL.
_ONLYFILES_DL_RE = re.compile(r"/dl/[^\s\"'>]+")
ONLYFILES_FILE_INFO_URL = "https://api.onlyfiles.com/v1/file/{id}/info"
ONLYFILES_HOST = "onlyfiles.com"
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # onlyfiles hard limit (~100 MiB)
_DEFAULT_TIMEOUT_SECONDS = 90
#: Uploads are permanent: expiry 0 means the file NEVER expires, so the
#: returned URL stays valid forever and can be stored and re-referenced by
#: the AI at any time without re-uploading the same bytes.
_ONLYFILES_EXPIRY = 0


class OnlyFilesError(RuntimeError):
    """Raised when onlyfiles.com cannot accept or describe a transfer."""


#: onlyfiles rejects uploads whose filename stem is shorter than this with
#: HTTP 422 ``ERROR_FILE_INVALID`` ("Invalid file name."). Verified live: a
#: 1-char stem (``a.txt``, ``t.txt``) is rejected, a 2-char stem is accepted.
_MIN_FILENAME_STEM = 2


def safe_upload_filename(filename: str) -> str:
    """Return a filename onlyfiles will accept.

    The service rejects short stems with HTTP 422 (verified live: ``t.txt`` and
    ``x.bin`` fail, ``ab.txt`` succeeds) and requires an extension. Artifacts
    produced by the agent are frequently named by a one-letter variable, so
    without this the upload fails and the user never receives a link. Never
    raises; always returns a usable name.
    """
    raw = Path(str(filename or "")).name.strip().replace("\x00", "")
    if not raw:
        return "powerx-file.bin"
    stem, dot, suffix = raw.rpartition(".")
    if not dot:
        # No extension at all: onlyfiles rejects it, so add a neutral one.
        stem, suffix = raw, "bin"
    if not suffix:
        suffix = "bin"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
    if len(cleaned) < _MIN_FILENAME_STEM:
        cleaned = f"file-{cleaned}" if cleaned else "powerx-file"
    return f"{cleaned}.{suffix}"


def onlyfiles_file_id(url: str) -> str:
    """Return the slug id for a page or ``/dl/`` onlyfiles URL (``''`` if none)."""
    parsed = urlparse(str(url or "").strip())
    if parsed.netloc != ONLYFILES_HOST:
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return ""
    # /dl/<ts.nonce>/<id>/<file> -> the id is the third segment.
    if parts[0] == "dl":
        return parts[2] if len(parts) >= 3 else ""
    return parts[0]


def gateway_base_url() -> str:
    """Public base URL of this gateway, used to build permanent links."""
    for var in ("POWERX_PUBLIC_URL", "NANOBOT_API_PUBLIC_URL", "API_SERVER_URL"):
        value = (os.environ.get(var) or "").strip().rstrip("/")
        if value:
            return value
    return ""


def permanent_download_url(page_url: str) -> str:
    """Return a stable link that downloads the bytes instead of opening a page.

    The policy is configured at the gateway (`/f/<id>` redirects to a
    freshly-minted raw ``/dl/`` token), so this link never expires. When no
    gateway is configured the page URL is returned, since it is still the
    permanent, shareable form.

    This is the fix for "pasting the link opens an HTML viewer": the page URL is
    permanent but serves a viewer, and the raw ``/dl/`` URL downloads but expires
    in ~2h, so neither can be stored. The gateway is both.
    """
    base = gateway_base_url()
    file_id = onlyfiles_file_id(page_url)
    if base and file_id:
        return f"{base}/f/{file_id}"
    return page_url


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


async def resolve_raw_url(page_url: str, *, timeout_seconds: int = 15) -> str:
    """Return a raw ``/dl/`` link that serves the file bytes for ``page_url``.

    The ``/dl/`` token is minted per page view and EXPIRES (verified: links minted
    two hours earlier stop serving bytes), so a raw link must never be stored and
    handed out later — resolve fresh at the moment of delivery/tap instead.

    A ``/dl/`` URL is re-resolved through its permanent page form, since an
    expired raw token would otherwise be handed straight back. Returns the input
    unchanged when it is not a resolvable onlyfiles URL or the fetch fails, so
    callers always have a safe fallback.
    """
    raw = str(page_url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.netloc != ONLYFILES_HOST or not parsed.path:
        return page_url
    parts = parsed.path.split("/")
    if parts[:2] == ["", "dl"]:
        # /dl/<ts.nonce>/<id>/<file> -> the permanent page form /<id>/<file>.
        if len(parts) < 4:
            return page_url
        raw = f"https://{ONLYFILES_HOST}/" + "/".join(parts[2:])
    try:
        timeout = aiohttp.ClientTimeout(total=max(5, min(int(timeout_seconds), 30)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(raw, headers={"User-Agent": "Mozilla/5.0"}) as page:
                if page.status != 200:
                    return raw
                match = _ONLYFILES_DL_RE.search(await page.text())
    except Exception:  # noqa: BLE001 - best-effort; the page URL is the fallback
        return raw
    return f"https://{ONLYFILES_HOST}{match.group(0)}" if match is not None else raw


def _extract_error(payload: dict[str, Any]) -> str | None:
    """Human-readable message from the documented error envelope, if any."""
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        etype = str(error.get("type") or "").strip()
        code = error.get("code")
        bits = [b for b in (message, etype) if b]
        if code is not None:
            bits.append(f"code={code}")
        if bits:
            return ": ".join(bits)
    return None


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
    safe_filename = safe_upload_filename(filename)
    timeout = aiohttp.ClientTimeout(total=max(10, min(int(timeout_seconds), 180)))
    form = aiohttp.FormData()
    form.add_field(
        "file",
        data,
        filename=safe_filename,
        content_type=content_type or "application/octet-stream",
    )
    # expire=0 per the docs: the file is kept FOREVER, so the returned URL is
    # permanent and can be stored in memory and re-referenced indefinitely.
    # (Field name is `expire`, not `expiry` — the wrong name is silently
    # ignored and the 24h default applies.)
    form.add_field("expire", str(_ONLYFILES_EXPIRY))
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
        detail = _extract_error(payload) if isinstance(payload, dict) else None
        raise OnlyFilesError(detail or "onlyfiles did not accept the upload")
    page_url = _extract_page_url(payload)
    # Mint a raw /dl/ link now so an immediate hand-off downloads on tap. The
    # token expires, so a link delivered later must be re-resolved with
    # ``resolve_raw_url``. ``url`` is the permanent link; when a gateway is
    # configured it is the always-downloads form rather than the HTML viewer.
    return {
        "url": permanent_download_url(page_url),
        "page_url": page_url,
        "download_url": await resolve_raw_url(page_url),
    }


async def file_info(file_id: str, *, timeout_seconds: int = 20) -> dict[str, Any] | None:
    """Probe the documented info endpoint; None when the file is gone.

    ``file_id`` is the slug segment of a stored URL (``https://onlyfiles.com
    /<id>/<name>`` -> ``<id>``). A missing file responds with HTTP 404 and
    ``status: false``; any other failure raises OnlyFilesError.
    """
    clean = str(file_id or "").strip().strip("/")
    if not clean:
        raise OnlyFilesError("file_info requires a file id")
    url = ONLYFILES_FILE_INFO_URL.format(id=clean)
    timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_seconds)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                raw = await response.text()
                status = response.status
    except aiohttp.ClientError as exc:
        raise OnlyFilesError(f"onlyfiles info request failed: {type(exc).__name__}") from None
    try:
        payload = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        payload = {}
    if status == 404:
        return None
    if not isinstance(payload, dict) or payload.get("status") is not True:
        detail = _extract_error(payload) if isinstance(payload, dict) else None
        raise OnlyFilesError(detail or f"onlyfiles info failed with HTTP {status}")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


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
        base = Path(root) if root is not None else get_persistent_data_dir("onlyfiles")
        self._path = base / "uploads.json"
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
    URL without a network call. Because expire=0 makes uploads permanent,
    the remembered URL is trusted WITHOUT a liveness probe (a probe would
    cost an API call per reuse); use ``file_info`` explicitly when a stored
    link must be verified. Raises OnlyFilesError like ``upload_path``.
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
        # The page URL is permanent but its raw /dl/ token expires, so mint a
        # fresh download link rather than handing back a stale one.
        stored_page = remembered.get("page_url") or remembered.get("url") or ""
        return {
            "url": permanent_download_url(stored_page) or stored_page,
            "page_url": stored_page,
            "download_url": await resolve_raw_url(stored_page),
        }
    result = await upload_path(
        path,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )
    memory.remember(data, source.name, result)
    return result


class ArtifactLinkMemory:
    """Persistent, named index of delivered artifact links.

    This is the durable "where did that file go?" memory. Sandboxes are torn
    down and the agent's context is small, but the *link* to an artifact is a
    tiny, permanent fact — uploads use ``expire=0`` and the gateway link stays
    valid, so it can be recalled forever.

    Only a short record per artifact is stored (name, description, URLs, size,
    timestamp) — never file bytes — so the persistent disk does not fill up.
    """

    #: Records kept before the oldest are pruned; each record is ~300 bytes, so
    #: this caps the store at roughly 1.5 MB.
    MAX_RECORDS = 5000

    def __init__(self, root: Path | None = None) -> None:
        base = Path(root) if root is not None else get_persistent_data_dir("artifacts")
        self._path = base / "links.json"
        self._loaded = False
        self._records: list[dict[str, Any]] = []

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.is_file():
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    self._records = [r for r in payload if isinstance(r, dict)]
                elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
                    self._records = [r for r in payload["records"] if isinstance(r, dict)]
        except (OSError, ValueError, TypeError):
            self._records = []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._records) > self.MAX_RECORDS:
                self._records = self._records[-self.MAX_RECORDS :]
            self._path.write_text(json.dumps(self._records), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("artifact link memory save failed: {}", exc)

    def remember(
        self,
        *,
        name: str,
        url: str,
        description: str = "",
        page_url: str = "",
        size: int | None = None,
        kind: str = "",
    ) -> dict[str, Any]:
        """Record (or refresh) a delivered artifact link. Never raises."""
        self._load()
        entry: dict[str, Any] = {
            "name": str(name or "").strip(),
            "description": str(description or "").strip()[:280],
            "url": str(url or "").strip(),
            "page_url": str(page_url or "").strip(),
            "kind": str(kind or "").strip(),
            "ts": time.time(),
        }
        if size is not None:
            entry["size"] = int(size)
        # Same name + same link replaces the previous record rather than
        # appending a duplicate every time the artifact is re-delivered.
        for index, existing in enumerate(self._records):
            if existing.get("name") == entry["name"] and existing.get("url") == entry["url"]:
                self._records[index] = entry
                self._save()
                return entry
        self._records.append(entry)
        self._save()
        return entry

    def search(self, query: str = "", *, limit: int = 10) -> list[dict[str, Any]]:
        """Return records matching ``query`` (name/description), newest first."""
        self._load()
        needle = str(query or "").strip().lower()
        records = list(reversed(self._records))
        if not needle:
            return records[:limit]
        terms = [t for t in re.split(r"\s+", needle) if t]
        hits = [
            r
            for r in records
            if all(
                term in f"{r.get('name','')} {r.get('description','')} {r.get('kind','')}".lower()
                for term in terms
            )
        ]
        return hits[:limit]


def artifact_memory() -> ArtifactLinkMemory:
    """Return the shared artifact-link memory (persistent disk backed)."""
    return ArtifactLinkMemory()
