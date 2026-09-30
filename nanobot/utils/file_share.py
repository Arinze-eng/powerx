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

from pathlib import Path
from typing import Any

import aiohttp

from nanobot.utils.onlyfiles import OnlyFilesError
from nanobot.utils.onlyfiles import upload_bytes as _onlyfiles_upload_bytes

# Routing thresholds (bytes).
_ONLYFILES_MAX_BYTES = 100 * 1024 * 1024        # onlyfiles hard limit (~100 MiB)
_CATBOX_THRESHOLD_BYTES = 100 * 1024 * 1024     # >100 MiB → always catbox
_CATBOX_MAX_BYTES = 200 * 1024 * 1024          # catbox practical ceiling (~200 MiB)

CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
_DEFAULT_TIMEOUT_SECONDS = 180


class FileShareError(RuntimeError):
    """Raised when no configured host can accept/describe the upload."""


def _normalize_onlyfiles(result: dict[str, str]) -> dict[str, Any]:
    """Map the onlyfiles result onto the uniform share contract.

    ``url`` must be the link a user can **paste and download**, so it prefers the
    permanent gateway form. The raw ``/dl/`` token expires in ~2h, so it is
    exposed separately as ``download_url`` for immediate one-off use only. Before
    this, ``url`` was the expiring raw token and ``page_url`` the HTML viewer —
    which is why a copied link either opened a web page or stopped working.

    ``gateway_url`` is the ``<origin>/f/<id>`` form and is EMPTY when no gateway
    is configured, which is how a delivery can tell a link that downloads and
    never expires (``url`` here) from a permanent-but-viewer page URL (``url``
    when there is no gateway). Collapsing the two is what made the old
    ``download_url`` result hand out an HTML viewer as its "permanent" fallback.
    """
    permanent = result.get("url") or ""
    return {
        "url": permanent or result.get("download_url") or "",
        "page_url": result.get("page_url") or permanent,
        "download_url": result.get("download_url", ""),
        "gateway_url": result.get("gateway_url", ""),
        "host": "onlyfiles",
    }


def _normalize_catbox(url: str) -> dict[str, Any]:
    # catbox returns one permanent direct-file URL for every field, so there is
    # nothing to separate: it always downloads and never expires.
    return {
        "url": url,
        "page_url": url,
        "download_url": url,
        "gateway_url": "",
        "host": "catbox",
    }


def artifact_delivery_text(shared: dict[str, Any], downloaded: Any) -> str:
    """The tool message that hands a published artifact to the user.

    Exactly one link is presented as the link to give the user, and it is always
    one that stays valid AND downloads on tap:

    * catbox returns a permanent direct file URL;
    * onlyfiles with a gateway configured returns ``<origin>/f/<id>``, which
      re-mints a raw token per request and forces ``Content-Disposition:
      attachment``;
    * onlyfiles with NO gateway has no such link — the page URL is an HTML viewer
      and the raw token expires in ~2h — so the fresh token is handed out for
      immediate use and the page URL is named as the permanent fallback.

    Rationale (the bug this replaces): every sandbox backend used to offer the
    ~2h raw ``/dl/`` token as the *primary* "Direct-download link" and the HTML
    viewer page as the "Permanent page link (fallback...)". The durable
    ``/f/<id>`` link was never shown to the model at all, so a user who pasted
    either one got a web page or a dead link — reported as "the link is not
    working, use onlyfiles".
    """
    host_label = str(shared.get("host") or "onlyfiles")
    gateway = str(shared.get("gateway_url") or "").strip()
    raw = str(shared.get("download_url") or "").strip()
    page = str(shared.get("page_url") or "").strip()
    url = str(shared.get("url") or "").strip()

    if host_label == "catbox":
        # One permanent, direct file URL: it downloads and it never expires.
        primary, fallback, expires = url or raw or page, "", False
    elif gateway:
        primary, fallback, expires = gateway, "", False
    else:
        # No gateway: the page URL is permanent but renders a viewer, so the
        # fresh raw token is the only link that actually downloads right now.
        primary = raw or page
        fallback = page if page and page != primary else ""
        expires = bool(raw)

    lines = [f"Downloaded remote artifact to local path: {downloaded}"]
    if primary and expires:
        lines.append(
            f"Download link ({host_label}) - tap to download now "
            "(this token expires in about two hours):"
        )
        lines.append(primary)
        if fallback:
            lines.append(
                f"Permanent link ({host_label}) - opens the file's page, valid forever:"
            )
            lines.append(fallback)
    elif primary:
        lines.append(f"Download link ({host_label}) - permanent, tap to download:")
        lines.append(primary)
    lines.append(
        "Give the user THIS link and do NOT paste the file contents into your reply. "
        "Do not substitute another link - not a sandbox preview or signed URL, not a "
        "cloud-drive share, not a raw transfer token: the link above is the one that "
        "works for the user. The file may also be attached directly via the message "
        "tool's media parameter when direct attachment delivery is available. Prefer "
        "a single clear download link over dumping raw text."
    )
    return "\n".join(lines)


def remember_artifact(result: dict[str, Any], *, filename: str, description: str = "") -> None:
    """Persist a delivered artifact link so it can be recalled after sandbox loss.

    Only the short metadata record is written (never the bytes), so this survives
    sandbox restarts and context loss without filling the persistent disk.
    Never raises — memory is best-effort and must not break a delivery.
    """
    url = str(result.get("url") or "").strip()
    if not url:
        return
    try:
        from nanobot.utils.onlyfiles import artifact_memory

        artifact_memory().remember(
            name=str(filename or "artifact"),
            url=url,
            page_url=str(result.get("page_url") or ""),
            description=description,
            kind=Path(str(filename or "")).suffix.lstrip(".").lower(),
        )
    except Exception:  # noqa: BLE001 - memory failure must never break delivery
        pass


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
        result = await _upload_catbox(
            data, filename=name, content_type=content_type, timeout_seconds=timeout_seconds
        )
        remember_artifact(result, filename=name)
        return result

    if size <= _ONLYFILES_MAX_BYTES:
        try:
            result = await _upload_onlyfiles(
                data, filename=name, content_type=content_type, timeout_seconds=timeout_seconds
            )
            remember_artifact(result, filename=name)
            return result
        except OnlyFilesError:
            # Fall through to catbox for any onlyfiles rejection.
            pass

    # Mid-size (>100 MiB or onlyfiles rejected): use catbox.
    result = await _upload_catbox(
        data, filename=name, content_type=content_type, timeout_seconds=timeout_seconds
    )
    remember_artifact(result, filename=name)
    return result


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
