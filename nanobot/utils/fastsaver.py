"""FastSaver: the first-choice YouTube downloader for the media tool.

FastSaver fetches the video from its own servers and hands back a short-lived
link to the finished file, so the download does not depend on YouTube accepting
the sandbox's IP address. That is the failure Invidious and yt-dlp hit most.

The API key lives on the host (``FASTSAVER_API_KEY`` in the application's
environment). It is never written into a sandbox command or a sandbox file: the
host asks for the link, and only the link (valid about five minutes, needs no
key) is handed to the sandbox CLI.

Costs: 15 credits per download, 25 for 1440p and 2160p. A failed request costs
almost nothing. ``GET /v1/balance`` is free.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

FASTSAVER_BASE = "https://api.fastsaver.io/v1"
REQUEST_TIMEOUT_SECONDS = 60
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_HEIGHT_FORMATS = {
    "best": "1080p",  # 2160p costs 25 credits; 1080p keeps the default cheap
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
}
_EXPENSIVE_FORMATS = {"1440p", "2160p"}


class FastSaverError(RuntimeError):
    """FastSaver could not provide a link. The caller falls back to yt-dlp."""


def api_key() -> str:
    """The key from the host environment, or ``""`` when FastSaver is not set up."""
    return os.environ.get("FASTSAVER_API_KEY", "").strip()


def is_youtube_url(url: str) -> bool:
    host = urllib.parse.urlparse(str(url or "").strip()).netloc.lower()
    return host == "youtu.be" or host.endswith("youtube.com") or host.endswith("youtu.be")


def format_for(quality: str, audio_only: bool) -> str:
    """Map the tool's quality value onto a FastSaver ``format``."""
    if audio_only:
        return "mp3"
    return _HEIGHT_FORMATS.get(str(quality or "1080").strip().lower(), "1080p")


def _reason(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("error", "message", "detail"):
            value = data.get(key)
            if value:
                return str(value)[:300]
    return "no reason given"


def request_download(
    url: str,
    quality: str = "1080",
    audio_only: bool = False,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Ask FastSaver for a download link for one YouTube video.

    Returns ``None`` when FastSaver is not usable for this request at all (no key,
    or not a YouTube link), so the caller can simply go on to the next path.
    Raises :class:`FastSaverError` when FastSaver was asked and did not deliver.

    Success is decided by ``ok`` in the JSON body, not by the HTTP status: the
    API documents its failures in the body.
    """
    key = api_key()
    if not key or not is_youtube_url(url):
        return None
    fmt = format_for(quality, audio_only)
    body = json.dumps({"url": str(url).strip(), "format": fmt}).encode("utf-8")
    request = urllib.request.Request(
        f"{FASTSAVER_BASE}/youtube/download",
        data=body,
        method="POST",
        headers={
            "X-Api-Key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    status = 0
    raw = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(1 << 16)
    except (urllib.error.URLError, OSError) as exc:
        raise FastSaverError(f"could not reach FastSaver: {exc}") from exc

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise FastSaverError(f"FastSaver answered HTTP {status} without JSON") from exc
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise FastSaverError(f"FastSaver refused the request (HTTP {status}): {_reason(data)}")

    link = str(data.get("download_url") or "").strip()
    if not link.startswith("https://"):
        raise FastSaverError("FastSaver returned no download link")
    return {
        "download_url": link,
        "filename": str(data.get("filename") or ""),
        "duration": float(data.get("duration") or 0.0),
        "video_id": str(data.get("video_id") or ""),
        "format": fmt,
        "credits": 25 if fmt in _EXPENSIVE_FORMATS else 15,
    }
