"""YouTube tool for the nanobot agent.

Auto-discovered by ToolLoader. Gives the LLM a single ``youtube`` action that
covers reading a channel, searching, listing videos, inspecting a video, reading
comments and playlists, plus write actions (like, subscribe) via the YouTube
Data API v3.

Per-user tokens are resolved from Supabase (set via the YouTube connector in
Settings). When no user identity is present at all, the server-level
``YOUTUBE_REFRESH_TOKEN`` env fallback is used.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.youtube.api import (
    YouTubeAPIError,
    api_get,
    api_post,
    fetch_my_channel,
    resolve_channel,
)
from nanobot.youtube.oauth import get_youtube_oauth_manager


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "my_channel",
                    "channel",
                    "search",
                    "videos",
                    "video",
                    "comments",
                    "playlists",
                    "like",
                    "subscribe",
                ],
                "description": (
                    "The YouTube action to perform. 'my_channel' shows the connected "
                    "user's own channel (use for 'show my YouTube channel', 'my "
                    "subscribers'); 'channel' looks up ANY channel by handle, URL, id or "
                    "plain name (use for 'how many subscribers does @handle have', "
                    "'info about the MrBeast channel'); 'search' searches videos and "
                    "channels (use for 'find videos about X'); 'videos' lists a channel's "
                    "recent videos; 'video' gives details/statistics for one video; "
                    "'comments' reads comments on a video; 'playlists' lists a channel's "
                    "playlists; 'like' likes a video; 'subscribe' subscribes to a channel."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Free-text search query for the 'search' action, or a plain channel "
                    "name for the 'channel' action."
                ),
            },
            "channel": {
                "type": "string",
                "description": (
                    "A channel reference for 'channel', 'videos' or 'playlists': an "
                    "@handle, a youtube.com URL, a UC... channel id, or a plain name. "
                    "The tool resolves it to the right channel."
                ),
            },
            "video_id": {
                "type": "string",
                "description": (
                    "A YouTube video id (the v= value or the part after youtu.be/) for "
                    "'video', 'comments' or 'like'."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (1-25). Defaults to 5.",
            },
            "channel_id": {
                "type": "string",
                "description": "A UC... channel id, used by 'subscribe' (and as an alternative to 'channel').",
            },
        },
        "required": ["action"],
    }
)
class YouTubeTool(Tool):
    """Look up and act on YouTube channels, videos, comments and playlists."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "youtube"

    @property
    def description(self) -> str:
        return (
            "Interact with YouTube on behalf of the connected user. Use this tool for "
            "ANY YouTube request: showing the user's own channel, looking up a channel "
            "by handle/URL/id/name and its subscriber or video counts, searching videos "
            "and channels, listing a channel's recent videos or playlists, getting a "
            "video's details and statistics, reading a video's comments, and write "
            "actions like liking a video or subscribing to a channel. Actions: "
            "'my_channel', 'channel', 'search', 'videos', 'video', 'comments', "
            "'playlists', 'like', 'subscribe'. The user's Google account must be "
            "connected in Settings first."
        )

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    # -- credential resolution ---------------------------------------------

    @staticmethod
    def _current_user_id() -> str | None:
        """Return the authenticated user identity from the request context, if any."""
        try:
            from nanobot.agent.tools.context import current_request_context

            ctx = current_request_context()
            if ctx is None:
                return None
            metadata = getattr(ctx, "metadata", None) or {}
            for key in ("user_id", "supabase_user_id", "telegram_user_id", "sender_id"):
                value = metadata.get(key)
                if value:
                    return str(value)
            attributes = getattr(ctx, "attributes", None) or {}
            for key in ("user_id", "supabase_user_id", "telegram_user_id"):
                value = attributes.get(key)
                if value:
                    return str(value)
            if getattr(ctx, "sender_id", None):
                return str(ctx.sender_id)
            return None
        except Exception:
            return None

    async def _access_token(self) -> str | None:
        """Resolve a live access token for the current user, env fallback only
        when there is no user identity at all."""
        user_id = self._current_user_id()
        manager = get_youtube_oauth_manager()
        if user_id:
            return await manager.access_token_for_user(user_id)

        # No identity: server-level env fallback.
        refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN", "").strip()
        if refresh_token:
            from nanobot.youtube.oauth import refresh_access_token

            try:
                tokens = await refresh_access_token(refresh_token)
                return tokens.get("access_token")
            except Exception as exc:
                logger.debug(f"YouTube env refresh token failed: {exc}")
                return None
        return None

    # -- entry point --------------------------------------------------------

    async def execute(self, **kwargs) -> Any:
        action = str(kwargs.get("action", "") or "").strip()
        try:
            access_token = await self._access_token()
        except Exception as exc:
            logger.exception("YouTube tool could not resolve credentials")
            return ToolResult.error(f"Could not access YouTube: {exc}")

        if not access_token:
            return ToolResult.error(
                "YouTube is not connected for this user. Open Settings, find the "
                "YouTube connector and click Connect to link your Google account."
            )

        try:
            handler = {
                "my_channel": self._my_channel,
                "channel": self._channel,
                "search": self._search,
                "videos": self._videos,
                "video": self._video,
                "comments": self._comments,
                "playlists": self._playlists,
                "like": self._like,
                "subscribe": self._subscribe,
            }.get(action)
            if handler is None:
                return ToolResult.error(f"Unknown YouTube action: {action}")
            return await handler(access_token, kwargs)
        except YouTubeAPIError as exc:
            return ToolResult.error(exc.message)
        except Exception as exc:
            logger.exception("YouTube tool error")
            return ToolResult.error(f"YouTube request failed: {exc}")

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _max_results(kwargs: dict[str, Any], default: int = 5) -> int:
        try:
            value = int(kwargs.get("max_results") or default)
        except (TypeError, ValueError):
            value = default
        return max(1, min(25, value))

    # -- actions ------------------------------------------------------------

    async def _my_channel(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        channel = await fetch_my_channel(access_token)
        if not channel:
            return ToolResult(
                "This Google account has no YouTube channel. Create one on YouTube "
                "and try again."
            )
        return ToolResult(self._format_channel(channel, heading="Your channel"))

    async def _channel(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        reference = (kwargs.get("channel") or kwargs.get("query") or "").strip()
        if not reference:
            return ToolResult.error("Provide a channel handle, URL, id or name.")
        channel = await resolve_channel(access_token, reference)
        return ToolResult(self._format_channel(channel))

    async def _search(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        query = (kwargs.get("query") or kwargs.get("channel") or "").strip()
        if not query:
            return ToolResult.error("Provide a query to search for.")
        max_results = self._max_results(kwargs)
        data = await api_get(
            "/search",
            access_token=access_token,
            params={
                "part": "snippet",
                "maxResults": max_results,
                "q": query,
                "type": "video,channel",
            },
        )
        items = data.get("items") or []
        if not items:
            return ToolResult(f"No YouTube results for {query!r}.")
        lines = [f"YouTube results for {query!r}:"]
        for item in items:
            lines.append(self._format_search_item(item))
        return ToolResult("\n".join(lines))

    async def _videos(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        reference = (kwargs.get("channel") or kwargs.get("query") or "").strip()
        if not reference:
            return ToolResult.error("Provide a channel to list videos for.")
        channel = await resolve_channel(access_token, reference)
        channel_id = channel.get("id")
        max_results = self._max_results(kwargs)
        data = await api_get(
            "/search",
            access_token=access_token,
            params={
                "part": "snippet",
                "channelId": channel_id,
                "maxResults": max_results,
                "order": "date",
                "type": "video",
            },
        )
        items = data.get("items") or []
        title = (channel.get("snippet") or {}).get("title", channel_id)
        if not items:
            return ToolResult(f"No recent videos found for {title}.")
        lines = [f"Recent videos for {title}:"]
        for item in items:
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            lines.append(f"  - {snippet.get('title')} ({video_id})")
        return ToolResult("\n".join(lines))

    async def _video(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        video_id = (kwargs.get("video_id") or "").strip()
        if not video_id:
            return ToolResult.error("Provide a video_id.")
        data = await api_get(
            "/videos",
            access_token=access_token,
            params={"part": "snippet,statistics,contentDetails", "id": video_id},
        )
        items = data.get("items") or []
        if not items:
            return ToolResult.error(f"No video found with id {video_id}.")
        video = items[0]
        snippet = video.get("snippet") or {}
        stats = video.get("statistics") or {}
        return ToolResult(
            f"Video: {snippet.get('title')}\n"
            f"  Channel: {snippet.get('channelTitle')}\n"
            f"  Published: {snippet.get('publishedAt')}\n"
            f"  Views: {stats.get('viewCount', '0')}\n"
            f"  Likes: {stats.get('likeCount', '0')}\n"
            f"  Comments: {stats.get('commentCount', '0')}\n"
            f"  URL: https://www.youtube.com/watch?v={video_id}"
        )

    async def _comments(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        video_id = (kwargs.get("video_id") or "").strip()
        if not video_id:
            return ToolResult.error("Provide a video_id to read comments for.")
        max_results = self._max_results(kwargs, default=10)
        data = await api_get(
            "/commentThreads",
            access_token=access_token,
            params={
                "part": "snippet",
                "videoId": video_id,
                "maxResults": max_results,
                "textFormat": "plainText",
            },
        )
        items = data.get("items") or []
        if not items:
            return ToolResult(f"No comments found on video {video_id}.")
        lines = [f"Comments on {video_id}:"]
        for item in items:
            top = ((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
            lines.append(f"  - {top.get('authorDisplayName')}: {top.get('textDisplay')}")
        return ToolResult("\n".join(lines))

    async def _playlists(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        reference = (kwargs.get("channel") or kwargs.get("query") or "").strip()
        max_results = self._max_results(kwargs)
        params: dict[str, Any] = {"part": "snippet", "maxResults": max_results}
        heading = "Your playlists"
        if reference:
            channel = await resolve_channel(access_token, reference)
            params["channelId"] = channel.get("id")
            heading = f"Playlists for {(channel.get('snippet') or {}).get('title', reference)}"
        else:
            params["mine"] = "true"
        data = await api_get("/playlists", access_token=access_token, params=params)
        items = data.get("items") or []
        if not items:
            return ToolResult(f"{heading}: none found.")
        lines = [f"{heading}:"]
        for item in items:
            snippet = item.get("snippet") or {}
            lines.append(f"  - {snippet.get('title')} ({item.get('id')})")
        return ToolResult("\n".join(lines))

    async def _like(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        video_id = (kwargs.get("video_id") or "").strip()
        if not video_id:
            return ToolResult.error("Provide a video_id to like.")
        await api_post(
            "/videos/rate",
            access_token=access_token,
            params={"id": video_id, "rating": "like"},
        )
        return ToolResult(f"Liked video {video_id}.")

    async def _subscribe(self, access_token: str, kwargs: dict[str, Any]) -> ToolResult:
        channel_id = (kwargs.get("channel_id") or "").strip()
        reference = (kwargs.get("channel") or kwargs.get("query") or "").strip()
        if not channel_id and reference:
            channel = await resolve_channel(access_token, reference)
            channel_id = str(channel.get("id") or "")
        if not channel_id:
            return ToolResult.error("Provide a channel to subscribe to.")
        await api_post(
            "/subscriptions",
            access_token=access_token,
            params={"part": "snippet"},
            body={
                "snippet": {
                    "resourceId": {"kind": "youtube#channel", "channelId": channel_id}
                }
            },
        )
        return ToolResult(f"Subscribed to channel {channel_id}.")

    # -- formatting ---------------------------------------------------------

    @staticmethod
    def _format_channel(channel: dict[str, Any], *, heading: str | None = None) -> str:
        snippet = channel.get("snippet") or {}
        stats = channel.get("statistics") or {}
        title = snippet.get("title", "Unknown channel")
        lines = [f"{heading or title}"]
        if heading:
            lines.append(f"  Name: {title}")
        lines.extend(
            [
                f"  Handle: {snippet.get('customUrl', '(none)')}",
                f"  Channel ID: {channel.get('id')}",
                f"  Subscribers: {stats.get('subscriberCount', 'hidden')}",
                f"  Videos: {stats.get('videoCount', '0')}",
                f"  Views: {stats.get('viewCount', '0')}",
                f"  URL: https://www.youtube.com/channel/{channel.get('id')}",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _format_search_item(item: dict[str, Any]) -> str:
        ident = item.get("id") or {}
        snippet = item.get("snippet") or {}
        kind = ident.get("kind", "")
        if kind == "youtube#channel":
            ref = ident.get("channelId", "")
        else:
            ref = ident.get("videoId", "")
        return f"  - [{kind.replace('youtube#', '')}] {snippet.get('title')} ({ref})"
