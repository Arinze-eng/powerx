"""Repost a TikTok video to YouTube (Shorts), downloading inside the sandbox.

Why it is shaped like this
-------------------------
Two halves live in two very different places. The download must happen **inside
the execution sandbox** — a TikTok fetch is an unbounded network+disk job and the
application host serves every user's gateway, so running yt-dlp there would be
reckless (the same reasoning as the ``media`` tool). The upload must happen on
the **host**, because that is the only place the user's Google access token
exists: it never leaves the credential store.

So the flow is:

    1. yt-dlp, in the sandbox   -> the .mp4 plus its info JSON
    2. backend.download         -> the file onto the host, in a temp directory
    3. YouTube resumable upload -> videos().insert for the connected user

Nothing is re-uploaded twice: every successful repost is recorded in a ledger
keyed by TikTok video id (the reference implementation's ``youtube_uploads.csv``
idea, kept as JSON so it needs no parsing and survives restarts).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import posixpath
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.youtube.api import YOUTUBE_API_BASE, YouTubeAPIError, parse_api_error

UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"

#: YouTube's own limits, enforced here so a failure is a clear message instead
#: of a 400 from Google after the whole file has been sent.
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500

#: The reference project's defaults: Shorts-shaped, under "People & Blogs".
DEFAULT_TAGS = ("TikTok", "Shorts", "Reels")
DEFAULT_CATEGORY = "22"

#: yt-dlp is not always pre-installed in a fresh sandbox, and `curl_cffi` is what
#: lets it impersonate a browser — the one thing that reliably gets past
#: TikTok's bot wall from a datacenter IP. One quiet pip line is cheaper than
#: refusing the user's request, and both installs are best-effort: the download
#: still runs when there is no network for pip (the impersonation flag is only
#: added when curl_cffi actually imported).
_YTDLP_SETUP = (
    'if ! command -v yt-dlp >/dev/null 2>&1 || ! python3 -c "import curl_cffi" >/dev/null 2>&1; then '
    "python3 -m pip install -q --break-system-packages -U yt-dlp curl_cffi "
    ">/dev/null 2>&1 || pip3 install -q --break-system-packages -U yt-dlp curl_cffi "
    ">/dev/null 2>&1 || true; fi; "
    "if command -v yt-dlp >/dev/null 2>&1; then YT=yt-dlp; else YT='python3 -m yt_dlp'; fi; "
    'IMP=""; '
    'python3 -c "import curl_cffi" >/dev/null 2>&1 && IMP="--impersonate chrome"; '
    "export YT IMP"
)

#: TikTok answers a bare extractor request with "Unexpected response from webpage
#: request" when it feels like it; naming an API hostname is the documented escape
#: hatch, so a failed first pass is retried once against a real API endpoint.
_YTDLP_FALLBACK = "--extractor-args 'tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com'"

_YTDLP_OPTS = (
    "-f 'mp4/best[ext=mp4]/best' --no-playlist --write-info-json --no-warnings "
    "--socket-timeout 30 --retries 3 -o 'video.%(ext)s'"
)

_TIKTOK_ID_RE = re.compile(r"/video/(\d{6,})")
_TIKTOK_SHORT_RE = re.compile(r"tiktok\.com/(?:t/)?([A-Za-z0-9_-]+)/?$")


class TikTokRepostError(RuntimeError):
    """A repost failed in a way worth reporting to the user verbatim."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# TikTok URL handling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TikTokTarget:
    """A TikTok video worth fetching, and what it is called."""

    url: str
    video_id: str
    author: str = ""


def parse_tiktok_url(raw: str) -> TikTokTarget:
    """Parse a TikTok video URL into (url, video id, author).

    Accepts the canonical ``/@user/video/<id>`` form and the short ``/t/<code>``
    links; a short link keeps the code as its id until yt-dlp resolves it.
    """
    text = (raw or "").strip()
    if not text:
        raise TikTokRepostError("Provide a TikTok video URL to repost.")
    if not text.startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    if "tiktok.com" not in text:
        raise TikTokRepostError("That does not look like a TikTok link.")

    match = _TIKTOK_ID_RE.search(text)
    if match:
        author = ""
        author_match = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)/video/", text)
        if author_match:
            author = author_match.group(1)
        return TikTokTarget(url=text, video_id=match.group(1), author=author)

    short = _TIKTOK_SHORT_RE.search(text.split("?")[0])
    if short:
        return TikTokTarget(url=text, video_id=short.group(1))
    raise TikTokRepostError("Could not find a video id in that TikTok link.")


def compose_title(*, caption: str, author: str, video_id: str) -> str:
    """A YouTube-legal title: the caption when there is one, collapsed and capped."""
    text = " ".join((caption or "").split())
    if not text:
        text = f"TikTok by @{author}" if author else f"TikTok {video_id}"
    if len(text) > TITLE_MAX:
        text = text[:TITLE_MAX].rstrip()
    return text


def compose_description(*, caption: str, author: str, source_url: str) -> str:
    """The caption, the creator credit and the original link, within the limit."""
    parts = []
    if caption and caption.strip():
        parts.append(caption.strip())
    if author:
        parts.append(f"Creator: @{author} on TikTok")
    parts.append(f"Original: {source_url}")
    parts.append("#TikTok #Shorts")
    return "\n\n".join(parts)[:DESCRIPTION_MAX]


# ---------------------------------------------------------------------------
# Ledger — never upload the same TikTok twice
# ---------------------------------------------------------------------------


def ledger_path() -> Path:
    """Where the repost ledger lives (the durable volume, never the workspace)."""
    try:
        from nanobot.config.paths import get_persistent_data_dir

        base = get_persistent_data_dir("tiktok_reposts")
    except Exception:  # noqa: BLE001 - a missing config must not break the tool
        base = Path(tempfile.gettempdir()) / "powerx-tiktok-reposts"
    base.mkdir(parents=True, exist_ok=True)
    return base / "ledger.json"


@dataclass
class RepostLedger:
    """TikTok video id -> the YouTube video it became."""

    path: Path = field(default_factory=ledger_path)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        tmp.replace(self.path)

    def get(self, video_id: str) -> dict[str, Any] | None:
        entry = self._read().get(video_id)
        return entry if isinstance(entry, dict) else None

    def all(self) -> dict[str, Any]:
        return self._read()

    async def record(self, video_id: str, entry: dict[str, Any]) -> None:
        async with self._lock:
            data = self._read()
            data[video_id] = {
                **entry,
                "reposted_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write(data)


# ---------------------------------------------------------------------------
# 1. Download, inside the execution sandbox
# ---------------------------------------------------------------------------


def download_command(remote_dir: str) -> str:
    """The sandbox command that fetches one TikTok video and its metadata.

    ``--write-info-json`` is what lets the title/description come from the real
    caption rather than from the URL, and ``-f`` prefers an mp4 container so the
    upload needs no remux.

    Two passes: browser impersonation first (what the bot wall respects), then the
    same request against a named TikTok API hostname. Only the first is attempted
    when curl_cffi is unavailable, and only the failure of the second is reported.
    """
    directory = shlex.quote(remote_dir)
    first = f"$YT $IMP {_YTDLP_OPTS}"
    return (
        f"mkdir -p {directory} && cd {directory} && {_YTDLP_SETUP}; "
        f"({first} \"$URL\" || "
        f"$YT $IMP {_YTDLP_FALLBACK} {_YTDLP_OPTS} \"$URL\")"
    )


async def download_in_sandbox(target: TikTokTarget) -> tuple[Path, dict[str, Any]]:
    """Fetch *target* inside the sandbox and copy it onto the host.

    Returns ``(local_mp4_path, info)``. The caller owns the temp directory
    (``local_mp4_path.parent``) and must remove it.
    """
    from nanobot.agent.tools.workspace_bridge import (
        remote_workspace_root,
        resolve_remote_executor,
    )

    try:
        executor = await resolve_remote_executor()
    except Exception as exc:  # noqa: BLE001
        raise TikTokRepostError(
            "No execution sandbox is available to download from. Select one in "
            "Settings and try again.",
            status=503,
        ) from exc
    if not executor.available:
        raise TikTokRepostError(
            "No execution sandbox is configured, so there is nowhere to download "
            "the TikTok video. Select a sandbox in Settings first.",
            status=503,
        )
    backend = getattr(executor, "backend", None)
    # The Novita SDK shape exposes files/commands instead of a backend object, so
    # it cannot hand a multi-megabyte video back through this path. Say so rather
    # than blowing up with an AttributeError deep inside the tool wrapper.
    if not callable(getattr(backend, "run", None)) or not callable(
        getattr(backend, "download", None)
    ):
        raise TikTokRepostError(
            "The selected sandbox cannot copy the downloaded video back to the "
            "server. Pick a different sandbox in Settings and try again.",
            status=503,
        )
    root = (await remote_workspace_root() or "").rstrip("/")
    if not root:
        raise TikTokRepostError("Could not resolve the sandbox workspace.", status=503)

    remote_dir = posixpath.join(root, ".tiktok-reposts", target.video_id or "video")
    command = download_command(remote_dir)
    # URL is passed as an env-style prefix rather than interpolated into the
    # command, so a link with shell metacharacters cannot become a second command.
    full = f"URL={shlex.quote(target.url)}; export URL; {command}"
    output = await executor.backend.run(full, timeout=900)  # type: ignore[attr-defined]
    if "[exit_code=0]" not in output and "exit_code" in output:
        raise TikTokRepostError(
            "The sandbox could not download that TikTok video. "
            f"{_tail(output)}".strip(),
            status=502,
        )

    info = await _read_remote_info(executor, f"{remote_dir}/video.info.json")
    remote_video = await _first_remote_video(executor, remote_dir)
    if remote_video is None:
        raise TikTokRepostError(
            "The download finished but produced no video file (the post may be "
            "private, region-locked, or deleted).",
            status=502,
        )

    local_dir = Path(tempfile.mkdtemp(prefix="tiktok-repost-"))
    local_video = local_dir / "video.mp4"
    try:
        await executor.backend.download(remote_video, str(local_video))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise TikTokRepostError(
            f"Could not copy the downloaded video out of the sandbox: {exc}",
            status=502,
        ) from exc
    if not local_video.is_file() or local_video.stat().st_size == 0:
        raise TikTokRepostError(
            "The downloaded video is empty — the source may be unavailable.",
            status=502,
        )
    return local_video, info


#: The only fields worth bringing back. yt-dlp also embeds a signed CDN URL per
#: format, which is what makes the file tens of kilobytes.
_INFO_FIELDS = (
    "id",
    "title",
    "description",
    "uploader",
    "uploader_id",
    "creator",
    "webpage_url",
    "track",
    "duration",
    "timestamp",
    "view_count",
    "like_count",
)


def info_read_command(remote_path: str) -> str:
    """Read those fields back as one small base64 line.

    ``cat``-ing the whole info JSON does not survive a backend that caps captured
    stdout: the document gets cut mid-object, the last ``}`` is nested, and the
    parse silently yields nothing. The program is base64-wrapped as well, so no
    quoting layer between here and the sandbox ever sees its characters.
    """
    program = (
        "import base64,json,sys\n"
        f"keys={json.dumps(list(_INFO_FIELDS))}\n"
        "data=json.load(open(sys.argv[1]))\n"
        "keep={k: data.get(k) for k in keys if isinstance(data.get(k), (str, int, float))}\n"
        "print(base64.b64encode(json.dumps(keep).encode()).decode())\n"
    )
    encoded = base64.b64encode(program.encode()).decode()
    return f'python3 -c "$(echo {encoded} | base64 -d)" {shlex.quote(remote_path)} 2>/dev/null'


def decode_info(output: str) -> dict[str, Any]:
    """The metadata line, whichever line of the captured output it landed on."""
    for line in reversed(str(output or "").splitlines()):
        text = line.strip()
        if not text or "exit_code" in text or text.startswith("["):
            continue
        try:
            data = json.loads(base64.b64decode(text, validate=True).decode())
        except Exception:  # noqa: BLE001 - any non-payload line is simply skipped
            continue
        if isinstance(data, dict) and data:
            return data
    return {}


async def _read_remote_info(executor: Any, remote_path: str) -> dict[str, Any]:
    """Best-effort read of yt-dlp's info JSON; metadata is optional, not fatal."""
    run = executor.backend.run  # type: ignore[attr-defined]
    info = decode_info(await run(info_read_command(remote_path), timeout=120))
    if info:
        return info
    # Older sandboxes may have no python3 on PATH; a bounded raw read is the
    # fallback, and it only wins when the JSON was small enough to arrive whole.
    listing = await run(f"cat {shlex.quote(remote_path)} | head -c 200000", timeout=120)
    start = listing.find("{")
    end = listing.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(listing[start : end + 1])
    except ValueError:
        logger.debug("tiktok repost: info JSON did not arrive whole; using defaults")
        return {}
    return data if isinstance(data, dict) else {}


async def _first_remote_video(executor: Any, remote_dir: str) -> str | None:
    """The .mp4 yt-dlp produced, whatever extension it settled on."""
    listing = await executor.backend.run(  # type: ignore[attr-defined]
        f"ls -1 {shlex.quote(remote_dir)} 2>/dev/null | grep -E '\\.(mp4|mkv|webm|mov)$' | head -1",
        timeout=120,
    )
    for line in listing.splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("[") and "exit_code" not in candidate:
            return f"{remote_dir}/{candidate}"
    return None


def _tail(text: str, limit: int = 300) -> str:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    return " ".join(lines[-3:])[:limit]


def metadata_from_info(info: dict[str, Any], target: TikTokTarget) -> tuple[str, str, str]:
    """``(caption, author, canonical_url)`` from yt-dlp's info JSON."""
    caption = str(info.get("description") or info.get("title") or "").strip()
    author = str(
        info.get("uploader") or info.get("creator") or info.get("uploader_id") or target.author
    ).lstrip("@")
    url = str(info.get("webpage_url") or target.url)
    return caption, author, url


# ---------------------------------------------------------------------------
# 2. Upload, on the host, with the user's own token
# ---------------------------------------------------------------------------


def video_body(
    *,
    title: str,
    description: str,
    tags: tuple[str, ...] = DEFAULT_TAGS,
    privacy: str = "public",
    category_id: str = DEFAULT_CATEGORY,
    made_for_kids: bool = False,
) -> dict[str, Any]:
    """The ``videos().insert`` request body (snippet + status)."""
    return {
        "snippet": {
            "title": title[:TITLE_MAX],
            "description": description[:DESCRIPTION_MAX],
            "tags": list(tags)[:TAGS_TOTAL_MAX],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }


async def upload_video(
    access_token: str,
    video_path: Path,
    *,
    title: str,
    description: str,
    tags: tuple[str, ...] = DEFAULT_TAGS,
    privacy: str = "public",
    made_for_kids: bool = False,
    timeout: float = 900.0,
) -> str:
    """Upload one file as a YouTube video and return its video id.

    Uses the resumable protocol: one JSON request that yields an upload URL, then
    a single PUT of the bytes. That keeps the token in a header (never in a URL)
    and works for a file of any size without buffering it twice.
    """
    body = video_body(
        title=title,
        description=description,
        tags=tags,
        privacy=privacy,
        made_for_kids=made_for_kids,
    )
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    # Redirects are handled by hand. The resumable handshake answers with the
    # session URL in ``Location`` (200/201, sometimes 308); if httpx chased that
    # redirect it would re-send the JSON body to the upload endpoint, which Google
    # rejects. So: never auto-follow, read the header, and PUT there ourselves.
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
        start = await client.post(
            f"{UPLOAD_BASE}/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers=headers,
            json=body,
        )
        if start.status_code >= 400:
            raise _upload_error(parse_api_error(start))
        location = start.headers.get("Location") or start.headers.get("location")
        if not location:
            raise TikTokRepostError(
                "YouTube did not return an upload session for this video.", status=502
            )

        data = video_path.read_bytes()
        put_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "video/*",
            "Content-Length": str(len(data)),
        }
        target = location
        for _attempt in range(3):
            finish = await client.put(target, headers=put_headers, content=data, timeout=timeout)
            if finish.status_code not in {301, 302, 303, 307, 308}:
                break
            # An upload session can redirect once (Google hands out a
            # googleusercontent URL); follow it, but only with the same bytes and
            # the same token — never by re-sending the JSON body.
            redirected = finish.headers.get("Location") or finish.headers.get("location")
            if not redirected or redirected == target:
                break
            target = redirected

    if finish.status_code >= 400:
        raise _upload_error(parse_api_error(finish))
    if finish.status_code >= 300:
        raise TikTokRepostError(
            "YouTube kept redirecting the upload without accepting it. Try again.",
            status=502,
        )
    try:
        payload = finish.json()
    except Exception as exc:  # noqa: BLE001
        raise TikTokRepostError("YouTube returned an unreadable upload response.") from exc
    video_id = str((payload or {}).get("id") or "")
    if not video_id:
        raise TikTokRepostError("YouTube accepted the upload but returned no video id.")
    return video_id


def _upload_error(exc: YouTubeAPIError) -> TikTokRepostError:
    """Translate a Google upload failure into something worth saying out loud."""
    if exc.reason == "quotaExceeded" or "quota" in exc.message.lower():
        return TikTokRepostError(
            "YouTube's daily upload quota is used up for this project "
            "(the Data API allows about 6 uploads a day). Try again after the "
            "quota resets at midnight Pacific.",
            status=429,
        )
    return TikTokRepostError(exc.message, status=exc.status)


# ---------------------------------------------------------------------------
# 3. The whole job
# ---------------------------------------------------------------------------


async def repost_tiktok(
    *,
    access_token: str,
    url: str,
    title: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] = DEFAULT_TAGS,
    privacy: str = "public",
    made_for_kids: bool = False,
    force: bool = False,
    ledger: RepostLedger | None = None,
) -> dict[str, Any]:
    """Download a TikTok video in the sandbox and publish it to YouTube."""
    if privacy not in {"public", "unlisted", "private"}:
        raise TikTokRepostError("privacy must be public, unlisted or private.")
    target = parse_tiktok_url(url)
    ledger = ledger or RepostLedger()

    known = ledger.get(target.video_id)
    if known and not force:
        return {
            "status": "already_reposted",
            "tiktok_video_id": target.video_id,
            "youtube_video_id": known.get("youtube_video_id"),
            "youtube_url": f"https://youtu.be/{known.get('youtube_video_id')}",
            "reposted_at": known.get("reposted_at"),
            "note": (
                "This TikTok was already reposted, so nothing was uploaded again. "
                "Pass force=true to publish a second copy."
            ),
        }

    video_path, info = await download_in_sandbox(target)
    try:
        caption, author, canonical = metadata_from_info(info, target)
        final_title = (title or "").strip() or compose_title(
            caption=caption, author=author, video_id=target.video_id
        )
        final_description = (description or "").strip() or compose_description(
            caption=caption, author=author, source_url=canonical
        )
        video_id = await upload_video(
            access_token,
            video_path,
            title=final_title,
            description=final_description,
            tags=tags,
            privacy=privacy,
            made_for_kids=made_for_kids,
        )
    finally:
        _cleanup(video_path.parent)

    await ledger.record(
        target.video_id,
        {
            "youtube_video_id": video_id,
            "title": final_title,
            "source_url": canonical,
            "privacy": privacy,
        },
    )
    return {
        "status": "reposted",
        "tiktok_video_id": target.video_id,
        "youtube_video_id": video_id,
        "youtube_url": f"https://youtu.be/{video_id}",
        "title": final_title,
        "privacy": privacy,
    }


def _cleanup(directory: Path) -> None:
    """Remove the download directory — but only one we created.

    Deleting a directory we were merely handed would be a data-loss bug, so the
    name has to carry this module's own temp prefix (``mkdtemp`` in
    :func:`download_in_sandbox`); anything else is left alone.
    """
    import shutil

    if not directory.name.startswith("tiktok-repost-"):
        logger.warning(
            "tiktok repost: refusing to remove {} (not a repost temp directory)",
            directory,
        )
        return
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except Exception:  # noqa: BLE001 - temp cleanup is never fatal
        logger.debug("tiktok repost: could not remove {}", directory)


__all__ = [
    "DEFAULT_TAGS",
    "RepostLedger",
    "TikTokRepostError",
    "TikTokTarget",
    "compose_description",
    "compose_title",
    "download_command",
    "download_in_sandbox",
    "ledger_path",
    "metadata_from_info",
    "parse_tiktok_url",
    "repost_tiktok",
    "upload_video",
    "video_body",
]
