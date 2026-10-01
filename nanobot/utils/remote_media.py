"""Turn a remote media reference into a local file.

A WebUI (or Telegram) upload of a *file* — anything that is not a small image
or video — is handed to the agent as an onlyfiles.com URL instead of bytes: the
browser uploads straight to onlyfiles and the gateway only ever sees a link, so
``store_inbound_attachments`` stores the URL string and nothing is written to
disk. That is the right call for a 100 MiB APK, and it is why the agent is told
"the links below download the raw file bytes directly — fetch each with a single
curl/wget".

It is the wrong shape for any tool that takes a **local path**. Handing such a
tool the URL the agent saw (``https://onlyfiles.com/dl/<token>/<id>/photo.png``)
fails every path guard in the codebase:

* ``ImageGenerationTool._resolve_reference_image`` answers
  ``reference_images must be inside the workspace or nanobot media directory``
  (or ``reference image is not a file``), and
* ``CloudinaryVideoEditTool._resolve_media_path`` answers ``file not found``.

The agent then reports, accurately but uselessly, that the attachment "isn't
available in the workspace or sandbox" and asks the user to upload it again.

:func:`materialize_reference` closes that gap in one place: it recognises an
``http(s)`` reference, rewrites an onlyfiles *page* URL to the form that serves
raw bytes, downloads the bytes with the same pinned-DNS transport the rest of
the codebase uses for untrusted URLs, and writes the file under the nanobot
media directory — which is already an allowed root for those guards. Callers get
back a local :class:`~pathlib.Path` and every existing code path keeps working
unchanged.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from nanobot.config.paths import get_media_dir
from nanobot.security.network import PinnedDNSAsyncTransport
from nanobot.utils.helpers import detect_image_mime

#: Schemes that mean "this is a URL, not a path on this machine".
REMOTE_REFERENCE_SCHEMES = ("http://", "https://")

#: Hosts whose *page* URL serves an HTML viewer rather than the file bytes.
_ONLYFILES_HOSTS = frozenset({"onlyfiles.com", "www.onlyfiles.com"})

_DOWNLOAD_TIMEOUT_S = 60.0
_DOWNLOAD_MAX_BYTES = 32 * 1024 * 1024
_DOWNLOAD_MAX_REDIRECTS = 5
_USER_AGENT = "Mozilla/5.0 (compatible; nanobot/1.0)"

#: Reference files land here, under the media root: an allowed root for every
#: tool that guards media paths, so a materialized reference needs no special
#: casing downstream.
REFERENCE_SUBDIR = "references"

#: Files copied *out* of the execution sandbox land here, for the same reason.
SANDBOX_SUBDIR = "sandbox"

#: Ceiling for one pull out of the sandbox. The bytes are held in memory and
#: Novita moves them as base64 (a third more), so this stays well under the
#: gateway's memory limit rather than tracking the 200 MiB upload cap.
SANDBOX_MAX_BYTES = 64 * 1024 * 1024

_SANDBOX_TIMEOUT_S = 180.0

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


class RemoteMediaError(RuntimeError):
    """Raised when a remote media reference cannot be turned into a local file."""


def is_remote_reference(value: object) -> bool:
    """True when ``value`` is an ``http(s)`` URL rather than a local path."""
    return isinstance(value, str) and value.strip().lower().startswith(
        REMOTE_REFERENCE_SCHEMES
    )


def reference_filename(url: str, suffix: str) -> str:
    """A safe, collision-free name for the local copy of ``url``.

    The digest of the URL (not of the bytes — those are unknown until the
    download finishes) is the stem so two references with the same basename
    never overwrite each other, and the basename is kept for a readable log.
    """
    parsed = urlparse(url)
    stem = Path(parsed.path).name or "reference"
    stem = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in stem).strip(".-")
    if not stem:
        stem = "reference"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    # The basename may already end in the extension; do not double it up.
    if suffix and stem.lower().endswith(suffix.lower()):
        stem = stem[: -len(suffix)]
    return f"{digest}-{stem}{suffix}"


def _suffix_for(url: str, content_type: str, raw: bytes) -> str:
    """Pick the extension for downloaded bytes: sniffed, then declared, then named."""
    mime = detect_image_mime(raw)
    if mime in _SUFFIX_BY_MIME:
        return _SUFFIX_BY_MIME[mime]
    declared = content_type.split(";", 1)[0].strip().lower()
    if declared in _SUFFIX_BY_MIME:
        return _SUFFIX_BY_MIME[declared]
    named = Path(urlparse(url).path).suffix.lower()
    return named if named and len(named) <= 6 else ".bin"


def _is_raw_onlyfiles_url(url: str) -> bool:
    """True for an onlyfiles ``/dl/`` URL: it already names the file bytes."""
    return urlparse(url).path.split("/")[:2] == ["", "dl"]


async def reference_candidates(url: str) -> list[str]:
    """URLs to try, in order, to obtain the bytes behind a reference.

    A ``/dl/`` link carries a token minted per page view that dies after 300 s,
    so it is tried first (no extra request when it is still fresh) and the
    freshly minted link is kept as a fallback. A viewer *page* URL is the other
    way round: it can only serve HTML, so the raw link must be minted up front.
    A non-onlyfiles URL is returned as-is.
    """
    if urlparse(url).netloc.lower() not in _ONLYFILES_HOSTS:
        return [url]
    from nanobot.utils.onlyfiles import resolve_raw_url

    if _is_raw_onlyfiles_url(url):
        candidates = [url]
    else:
        candidates = []
    try:
        minted = await resolve_raw_url(url)
    except Exception as exc:  # noqa: BLE001 - best-effort; the input is the fallback
        logger.debug("could not mint a fresh onlyfiles link for {}: {}", url, exc)
        minted = ""
    if minted and minted not in candidates:
        candidates.append(minted)
    return candidates or [url]


async def fetch_remote_bytes(
    url: str,
    *,
    max_bytes: int = _DOWNLOAD_MAX_BYTES,
    timeout: float = _DOWNLOAD_TIMEOUT_S,
) -> tuple[bytes, str]:
    """Download ``url`` into memory, bounded and DNS-pinned. Raises RemoteMediaError."""
    if not is_remote_reference(url):
        raise RemoteMediaError(f"not a remote reference: {url}")
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
            transport=PinnedDNSAsyncTransport(),
        ) as client:
            current = url
            for _ in range(_DOWNLOAD_MAX_REDIRECTS + 1):
                async with client.stream(
                    "GET", current, headers={"User-Agent": _USER_AGENT}
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise RemoteMediaError("redirected without a location")
                        current = urljoin(str(response.url), location)
                        continue
                    if response.status_code >= 400:
                        raise RemoteMediaError(
                            f"download failed with HTTP {response.status_code}"
                        )
                    content_type = response.headers.get("content-type", "")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise RemoteMediaError(
                                "remote file is larger than the "
                                f"{max_bytes // (1024 * 1024)} MiB limit"
                            )
                    if not body:
                        raise RemoteMediaError("remote file is empty")
                    return bytes(body), content_type
            raise RemoteMediaError("too many redirects")
    except httpx.HTTPError as exc:
        raise RemoteMediaError(f"download failed: {type(exc).__name__}") from exc


async def materialize_reference(
    value: str,
    *,
    dest_dir: Path | None = None,
    max_bytes: int = _DOWNLOAD_MAX_BYTES,
) -> Path:
    """Return a local path holding the bytes of the remote reference ``value``.

    Downloads once per URL: an existing copy under the destination directory is
    reused, so a file carried through the transcript across turns is not fetched
    again. Raises :class:`RemoteMediaError` with a message a model can act on.
    """
    url = str(value).strip()
    root = dest_dir if dest_dir is not None else get_media_dir() / REFERENCE_SUBDIR
    # Cache by URL digest alone: the extension is only known after the bytes
    # are sniffed, so look for any file already carrying this digest.
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    for existing in root.glob(f"{digest}-*"):
        if existing.is_file() and existing.stat().st_size > 0:
            logger.debug("remote reference already materialized: {}", existing)
            return existing
    candidates = await reference_candidates(url)
    raw: bytes | None = None
    content_type = ""
    fetched_from = url
    failures: list[str] = []
    for candidate in candidates:
        try:
            body, ctype = await fetch_remote_bytes(candidate, max_bytes=max_bytes)
        except RemoteMediaError as exc:
            failures.append(str(exc))
            continue
        if looks_like_html(body):
            # A viewer page, not the file: the link had no working raw token.
            failures.append("the link served a web page, not the file")
            continue
        raw, content_type, fetched_from = body, ctype, candidate
        break
    if raw is None:
        raise RemoteMediaError("; ".join(failures) or "no downloadable link")
    suffix = _suffix_for(fetched_from, content_type, raw)
    root.mkdir(parents=True, exist_ok=True)
    path = root / reference_filename(url, suffix)
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(raw)
    tmp.replace(path)
    logger.info(
        "materialized remote reference {} -> {} ({} bytes)",
        url,
        path,
        len(raw),
    )
    return path


def looks_like_html(raw: bytes) -> bool:
    """True when bytes are an HTML page — i.e. a share link, not a file.

    onlyfiles and friends serve a viewer page when the raw ``/dl/`` token is
    missing or expired, and downloading that page silently would hand the
    provider HTML instead of an image. Callers use this to say so plainly.
    """
    head = raw.lstrip()[:256].lower()
    return head.startswith((b"<!doctype html", b"<html", b"<?xml")) or b"<html" in head


def is_sandbox_reference(value: object) -> bool:
    """True when ``value`` could name a file inside the execution sandbox.

    The Cloudinary tools and ``generate_image`` run on the gateway host, while
    the agent writes its working files inside the execution sandbox (Novita
    ``/workspace``, Freestyle ``/home/ubuntu/workspace``, …). A path the sandbox
    tool handed back therefore means nothing to a host-side resolver, which
    answered ``file not found`` — and the turn then reported "Cloudinary failed
    to locate the staged file" for a file that plainly existed one filesystem
    away.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or is_remote_reference(text):
        return False
    return text.startswith("/") or "/" not in text


def sandbox_filename(remote_path: str) -> str:
    """A safe local name for the copy of ``remote_path`` pulled onto this host."""
    base = Path(str(remote_path).rstrip("/")).name or "sandbox-file"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in base).strip("._")
    if not safe:
        safe = "sandbox-file"
    digest = hashlib.sha256(str(remote_path).encode("utf-8")).hexdigest()[:12]
    return f"{digest}-{safe}"


async def materialize_sandbox_file(
    value: str,
    *,
    dest_dir: Path | None = None,
    max_bytes: int = SANDBOX_MAX_BYTES,
) -> Path:
    """Copy a file that exists only inside the execution sandbox onto this host.

    ``value`` is either an absolute in-sandbox path (``/workspace/out.png``) or
    a bare name relative to the active sandbox workspace root (``out.png``).
    Returns a local :class:`~pathlib.Path` under the media directory, which is
    an allowed root for every media path guard, so the caller needs no special
    casing. Raises :class:`RemoteMediaError` with a message a model can act on.
    """
    raw = str(value).strip()
    if not raw:
        raise RemoteMediaError("no sandbox path was given")

    from nanobot.agent.tools.workspace_bridge import (  # noqa: PLC2701
        fetch_remote_file,
        sandbox_workspace_root,
    )

    if raw.startswith("/"):
        candidates = [raw]
    else:
        root = (await sandbox_workspace_root()) or ""
        if not root:
            raise RemoteMediaError(
                "no execution backend is configured, so there is no sandbox to read "
                f"{raw} from"
            )
        candidates = [f"{root.rstrip('/')}/{raw}"]

    payload: bytes | None = None
    remote_used = ""
    for remote in candidates:
        # An absolute path the backend refuses (outside its own workspace, or a
        # level it keeps private) must not end the search: the same name is
        # often present under the workspace root.
        payload = await fetch_remote_file(remote, max_bytes=max_bytes)
        if payload:
            remote_used = remote
            break

    if not payload:
        raise RemoteMediaError(
            f"{raw} was not found in the execution sandbox"
            # Name the path actually tried whenever it is not the value the
            # caller handed in — that is what turns "not found" into a location
            # the model can check with one sandbox call.
            + (f" (tried {', '.join(candidates)})" if candidates != [raw] else "")
            + ". Pass the sandbox path the sandbox tool returned, a local path on "
            "this host, or fetch the file with the sandbox tool's download_url "
            "action and pass the https link instead."
        )

    root_dir = dest_dir if dest_dir is not None else get_media_dir() / SANDBOX_SUBDIR
    root_dir.mkdir(parents=True, exist_ok=True)
    path = root_dir / sandbox_filename(remote_used)
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(payload)
    tmp.replace(path)
    logger.info(
        "materialized sandbox file {} -> {} ({} bytes)",
        remote_used,
        path,
        len(payload),
    )
    return path


async def try_materialize_sandbox_file(
    value: str, *, dest_dir: Path | None = None
) -> Path | None:
    """Best-effort :func:`materialize_sandbox_file`: ``None`` on any miss.

    Callers use this after their own lookup already failed, so a value that is
    simply not a sandbox path — or a sandbox that is unreachable — must fall
    through to their original error rather than replacing it with this one.
    """
    if not is_sandbox_reference(value):
        return None
    try:
        return await materialize_sandbox_file(value, dest_dir=dest_dir)
    except RemoteMediaError as exc:
        logger.debug("sandbox file {} not materialized: {}", value, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a miss must not mask the real error
        logger.debug("sandbox file {} errored: {}", value, exc)
        return None
