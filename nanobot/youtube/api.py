"""Small YouTube Data API v3 client plus channel resolution helpers.

The agent tool and the settings connector both go through here so channel
resolution (handle / URL / id / plain name) and error handling stay consistent.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

_CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")


class YouTubeAPIError(RuntimeError):
    """A YouTube Data API call failed in a way we can report to the user."""

    def __init__(self, message: str, *, status: int = 400, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason


def _reason_from_error(payload: Any) -> tuple[str, str]:
    """Extract (user_message, reason) from a Google API error body."""
    if not isinstance(payload, dict):
        return ("", "")
    error = payload.get("error")
    if not isinstance(error, dict):
        return ("", "")
    message = error.get("message")
    reason = ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = str(errors[0].get("reason") or "")
    return (str(message) if message else "", reason)


def _friendly_error(status: int, reason: str, message: str) -> str:
    if reason == "quotaExceeded" or status == 403 and "quota" in message.lower():
        return (
            "YouTube API quota exceeded for this project. Try again later or "
            "use a different Google project."
        )
    if reason in {"authError", "unauthorized"} or status == 401:
        return "Your YouTube connection has expired. Reconnect YouTube in Settings."
    if status == 403:
        return (
            "YouTube refused this request (permission or quota). "
            f"{message}".strip()
        )
    if status == 404:
        return "Not found on YouTube."
    return message or f"YouTube API request failed ({status})."


def parse_api_error(response: httpx.Response) -> YouTubeAPIError:
    try:
        payload = response.json()
    except Exception:
        payload = None
    message, reason = _reason_from_error(payload)
    return YouTubeAPIError(
        _friendly_error(response.status_code, reason, message),
        status=response.status_code,
        reason=reason,
    )


async def api_get(
    path: str,
    *,
    access_token: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET a YouTube Data API v3 resource. Raises YouTubeAPIError on failure."""
    query = dict(params or {})
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{YOUTUBE_API_BASE}{path}", params=query, headers=headers)
    if response.status_code >= 400:
        raise parse_api_error(response)
    try:
        data = response.json()
    except Exception as exc:  # pragma: no cover - defensive
        raise YouTubeAPIError("YouTube returned an unreadable response.") from exc
    return data if isinstance(data, dict) else {}


async def api_post(
    path: str,
    *,
    access_token: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = dict(params or {})
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{YOUTUBE_API_BASE}{path}", params=query, headers=headers, json=body or {}
        )
    if response.status_code >= 400:
        raise parse_api_error(response)
    if not response.content:
        return {}
    try:
        data = response.json()
    except Exception:  # pragma: no cover - defensive
        return {}
    return data if isinstance(data, dict) else {}


def normalize_channel_reference(raw: str) -> dict[str, str]:
    """Classify a user-supplied channel reference.

    Returns a dict with a ``kind`` of ``id`` (UC... id), ``handle`` (@handle),
    ``username`` (legacy /user/ name) or ``query`` (free text / plain name),
    plus the extracted ``value``.
    """
    text = (raw or "").strip()
    if not text:
        return {"kind": "query", "value": ""}

    parsed = urlsplit(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        segments = [seg for seg in parsed.path.split("/") if seg]
        if segments:
            if segments[0] == "channel" and len(segments) > 1:
                return {"kind": "id", "value": segments[1]}
            if segments[0] == "c" and len(segments) > 1:
                return {"kind": "query", "value": segments[1]}
            if segments[0] == "user" and len(segments) > 1:
                return {"kind": "username", "value": segments[1]}
            if segments[0].startswith("@"):
                return {"kind": "handle", "value": segments[0]}
        params = parse_qs(parsed.query)
        if params.get("channel_id"):
            return {"kind": "id", "value": params["channel_id"][0]}

    if _CHANNEL_ID_RE.match(text):
        return {"kind": "id", "value": text}
    if text.startswith("@"):
        return {"kind": "handle", "value": text}
    return {"kind": "query", "value": text}


async def resolve_channel(access_token: str, raw: str) -> dict[str, Any]:
    """Resolve any user-supplied channel reference to a channel resource.

    Raises YouTubeAPIError if nothing matches.
    """
    ref = normalize_channel_reference(raw)
    snippet = "snippet,statistics,contentDetails"

    if ref["kind"] == "id" and ref["value"]:
        data = await api_get(
            "/channels", access_token=access_token, params={"part": snippet, "id": ref["value"]}
        )
        items = data.get("items") or []
        if items:
            return items[0]

    if ref["kind"] == "handle" and ref["value"]:
        data = await api_get(
            "/channels",
            access_token=access_token,
            params={"part": snippet, "forHandle": ref["value"]},
        )
        items = data.get("items") or []
        if items:
            return items[0]

    if ref["kind"] == "username" and ref["value"]:
        data = await api_get(
            "/channels",
            access_token=access_token,
            params={"part": snippet, "forUsername": ref["value"]},
        )
        items = data.get("items") or []
        if items:
            return items[0]

    # Free-text / plain-name / fallback: search for a channel.
    query = ref["value"]
    if query:
        data = await api_get(
            "/search",
            access_token=access_token,
            params={
                "part": "snippet",
                "type": "channel",
                "maxResults": 1,
                "q": query,
            },
        )
        items = data.get("items") or []
        if items:
            first = items[0]
            channel_id = (first.get("id") or {}).get("channelId") or (
                first.get("snippet") or {}
            ).get("channelId")
            if channel_id:
                detail = await api_get(
                    "/channels",
                    access_token=access_token,
                    params={"part": snippet, "id": channel_id},
                )
                detail_items = detail.get("items") or []
                if detail_items:
                    return detail_items[0]

    raise YouTubeAPIError(f"Could not find a YouTube channel matching {raw!r}.", status=404)


async def fetch_my_channel(access_token: str) -> dict[str, Any] | None:
    """Return the connected user's own channel, or None if the account has none."""
    data = await api_get(
        "/channels",
        access_token=access_token,
        params={"part": "snippet,statistics,contentDetails", "mine": "true"},
    )
    items = data.get("items") or []
    return items[0] if items else None
