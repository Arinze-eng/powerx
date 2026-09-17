"""Tests for the YouTube agent tool.

Verify auto-discovery, the action surface, credential resolution (per-user
isolation and the not-connected path), and each action with the YouTube API
mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.youtube import YouTubeTool


def test_youtube_tool_auto_discovered() -> None:
    loader = ToolLoader()
    names = {c.__name__ for c in loader.discover()}
    assert "YouTubeTool" in names


def test_tool_has_expected_actions() -> None:
    tool = YouTubeTool()
    actions = set(tool.parameters["properties"]["action"]["enum"])
    assert actions == {
        "my_channel",
        "channel",
        "search",
        "videos",
        "video",
        "comments",
        "playlists",
        "like",
        "subscribe",
    }
    for prop in ("query", "channel", "video_id", "max_results", "channel_id"):
        assert prop in tool.parameters["properties"]


def test_enabled_always_true() -> None:
    assert YouTubeTool.enabled(MagicMock()) is True


def _tool_with_token(token: str | None) -> YouTubeTool:
    tool = YouTubeTool()
    tool._access_token = AsyncMock(return_value=token)  # type: ignore[method-assign]
    return tool


def test_not_connected_returns_error() -> None:
    tool = _tool_with_token(None)
    result = asyncio.run(tool.execute(action="my_channel"))
    assert result.is_error is True
    assert "not connected" in str(result).lower()


def test_my_channel_formats_stats() -> None:
    tool = _tool_with_token("tok")
    channel = {
        "id": "UC1",
        "snippet": {"title": "My Channel", "customUrl": "@mine"},
        "statistics": {"subscriberCount": "1234", "videoCount": "10", "viewCount": "999"},
    }
    with patch(
        "nanobot.agent.tools.youtube.fetch_my_channel",
        new=AsyncMock(return_value=channel),
    ):
        result = asyncio.run(tool.execute(action="my_channel"))
    assert "My Channel" in str(result)
    assert "1234" in str(result)


def test_channel_resolves_handle() -> None:
    tool = _tool_with_token("tok")
    channel = {
        "id": "UC2",
        "snippet": {"title": "MrBeast", "customUrl": "@MrBeast"},
        "statistics": {"subscriberCount": "999"},
    }
    with patch(
        "nanobot.agent.tools.youtube.resolve_channel",
        new=AsyncMock(return_value=channel),
    ) as resolve:
        result = asyncio.run(tool.execute(action="channel", channel="@MrBeast"))
    resolve.assert_awaited_once()
    assert "MrBeast" in str(result)


def test_search_lists_results() -> None:
    tool = _tool_with_token("tok")
    payload = {
        "items": [
            {
                "id": {"kind": "youtube#video", "videoId": "vid1"},
                "snippet": {"title": "A video"},
            }
        ]
    }
    with patch(
        "nanobot.agent.tools.youtube.api_get",
        new=AsyncMock(return_value=payload),
    ):
        result = asyncio.run(tool.execute(action="search", query="cats"))
    assert "A video" in str(result)
    assert "vid1" in str(result)


def test_video_details() -> None:
    tool = _tool_with_token("tok")
    payload = {
        "items": [
            {
                "id": "vid1",
                "snippet": {"title": "T", "channelTitle": "C", "publishedAt": "2024"},
                "statistics": {"viewCount": "42", "likeCount": "1"},
            }
        ]
    }
    with patch(
        "nanobot.agent.tools.youtube.api_get",
        new=AsyncMock(return_value=payload),
    ):
        result = asyncio.run(tool.execute(action="video", video_id="vid1"))
    assert "Views: 42" in str(result)


def test_like_posts_rating() -> None:
    tool = _tool_with_token("tok")
    post = AsyncMock(return_value={})
    with patch("nanobot.agent.tools.youtube.api_post", new=post):
        result = asyncio.run(tool.execute(action="like", video_id="vid1"))
    assert post.await_args.kwargs["params"] == {"id": "vid1", "rating": "like"}
    assert "Liked" in str(result)


def test_api_error_is_returned_not_raised() -> None:
    from nanobot.youtube.api import YouTubeAPIError

    tool = _tool_with_token("tok")
    with patch(
        "nanobot.agent.tools.youtube.api_get",
        new=AsyncMock(side_effect=YouTubeAPIError("quota exceeded", status=403)),
    ):
        result = asyncio.run(tool.execute(action="search", query="x"))
    assert result.is_error is True
    assert "quota exceeded" in str(result)


def test_unknown_action_returns_error() -> None:
    tool = _tool_with_token("tok")
    result = asyncio.run(tool.execute(action="not-real"))
    assert result.is_error is True


def test_access_token_prefers_per_user() -> None:
    """A resolved user id uses the per-user manager, never the env fallback."""

    fake_manager = MagicMock()
    fake_manager.access_token_for_user = AsyncMock(return_value="user-token")
    with (
        patch(
            "nanobot.agent.tools.youtube.get_youtube_oauth_manager",
            return_value=fake_manager,
        ),
        patch(
            "nanobot.agent.tools.youtube.YouTubeTool._current_user_id",
            return_value="user-1",
        ),
    ):
        tool = YouTubeTool()
        token = asyncio.run(tool._access_token())
    assert token == "user-token"
    fake_manager.access_token_for_user.assert_awaited_once_with("user-1")


def test_access_token_none_for_disconnected_user_even_with_env() -> None:
    import os

    fake_manager = MagicMock()
    fake_manager.access_token_for_user = AsyncMock(return_value=None)
    with (
        patch(
            "nanobot.agent.tools.youtube.get_youtube_oauth_manager",
            return_value=fake_manager,
        ),
        patch(
            "nanobot.agent.tools.youtube.YouTubeTool._current_user_id",
            return_value="user-1",
        ),
        patch.dict(os.environ, {"YOUTUBE_REFRESH_TOKEN": "env-refresh"}, clear=False),
    ):
        tool = YouTubeTool()
        token = asyncio.run(tool._access_token())
    assert token is None


def test_tool_registers_via_real_loader() -> None:
    from nanobot.agent.tools.context import ToolContext
    from nanobot.agent.tools.registry import ToolRegistry

    mock_config = MagicMock()
    mock_config.exec.enable = True
    mock_config.web.enable = True
    mock_config.image_generation.enabled = False
    mock_config.my.enable = False
    ctx = ToolContext(
        config=mock_config,
        workspace="/tmp",
        bus=MagicMock(),
        subagent_manager=MagicMock(),
        cron_service=MagicMock(),
        timezone="UTC",
    )
    registry = ToolRegistry()
    ToolLoader().load(ctx, registry)
    assert registry.has("youtube")
