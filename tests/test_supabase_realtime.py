"""Tests for the Supabase Realtime publisher and ChannelManager wiring."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import (
    ProgressEvent,
    RetryWaitEvent,
    StreamDeltaEvent,
    StreamEndEvent,
    StreamedResponseEvent,
    TurnEndEvent,
)
from nanobot.supabase_realtime import (
    SupabaseRealtimePublisher,
    _event_type,
)


class _FakeResponse:
    def __init__(self, status: int = 201, text: str = "") -> None:
        self.status_code = status
        self.text = text
        self.is_success = 200 <= status < 300


class _FakePublisher(SupabaseRealtimePublisher):
    """Publisher subclass that captures HTTP calls instead of making real ones."""

    def __init__(
        self,
        *,
        url: str = "https://example.supabase.co",
        service_key: str = "service-key",
        enabled_flag: str = "true",
    ) -> None:
        # Set the env vars that SupabaseRealtimePublisher reads.
        import os

        old_url = os.environ.get("SUPABASE_URL")
        old_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        old_flag = os.environ.get("SUPABASE_REALTIME_ENABLED")
        os.environ["SUPABASE_URL"] = url
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = service_key
        os.environ["SUPABASE_REALTIME_ENABLED"] = enabled_flag
        super().__init__()
        # Restore env vars for test isolation.
        for name, old in (
            ("SUPABASE_URL", old_url),
            ("SUPABASE_SERVICE_ROLE_KEY", old_key),
            ("SUPABASE_REALTIME_ENABLED", old_flag),
        ):
            if old is not None:
                os.environ[name] = old
            else:
                os.environ.pop(name, None)
        self.captured: list[dict[str, Any]] = []
        self._response = _FakeResponse(201, "")

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Capture the call instead of making a real HTTP request."""
        if not self.enabled:
            return
        event_type = _event_type(msg)
        if event_type in {"retry_wait"}:
            return
        chat_id = str(msg.chat_id or "").strip()
        if not chat_id:
            return
        # Apply the same content truncation as the real publisher.
        from nanobot.supabase_realtime import _MAX_CONTENT_CHARS, _bounded

        self.captured.append(
            {
                "chat_id": chat_id,
                "channel": msg.channel,
                "content": _bounded(str(msg.content or ""), _MAX_CONTENT_CHARS),
                "event_type": event_type,
            }
        )


def _make_msg(
    content: str = "Hello!",
    *,
    channel: str = "telegram",
    chat_id: str = "123",
    event: Any = None,
) -> OutboundMessage:
    return OutboundMessage(
        channel=channel,
        chat_id=chat_id,
        content=content,
        event=event,
    )


# ---------------------------------------------------------------------------
# _event_type tests
# ---------------------------------------------------------------------------

def test_event_type_none_event_returns_message() -> None:
    msg = _make_msg(event=None)
    assert _event_type(msg) == "message"

def test_event_type_stream_delta() -> None:
    msg = _make_msg(event=StreamDeltaEvent(content="hi", stream_id="s1"))
    assert _event_type(msg) == "delta"

def test_event_type_stream_end() -> None:
    msg = _make_msg(event=StreamEndEvent(stream_id="s1"))
    assert _event_type(msg) == "stream_end"

def test_event_type_streamed_response() -> None:
    msg = _make_msg(event=StreamedResponseEvent())
    assert _event_type(msg) == "streamed_response"

def test_event_type_progress() -> None:
    msg = _make_msg(event=ProgressEvent(content="working..."))
    assert _event_type(msg) == "progress"

def test_event_type_reasoning_delta() -> None:
    msg = _make_msg(event=ProgressEvent(content="thinking", reasoning_delta=True))
    assert _event_type(msg) == "reasoning_delta"

def test_event_type_reasoning_end() -> None:
    msg = _make_msg(event=ProgressEvent(reasoning_end=True))
    assert _event_type(msg) == "reasoning_end"

def test_event_type_turn_end() -> None:
    msg = _make_msg(event=TurnEndEvent(latency_ms=500))
    assert _event_type(msg) == "turn_end"

def test_event_type_retry_wait() -> None:
    msg = _make_msg(event=RetryWaitEvent(content="waiting..."))
    assert _event_type(msg) == "retry_wait"


# ---------------------------------------------------------------------------
# Publisher configuration tests
# ---------------------------------------------------------------------------

def test_publisher_not_configured_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    pub = SupabaseRealtimePublisher()
    assert not pub.configured
    assert not pub.enabled

def test_publisher_disabled_via_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_REALTIME_ENABLED", "false")
    pub = SupabaseRealtimePublisher()
    assert pub.configured
    assert not pub.enabled

def test_publisher_enabled_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_REALTIME_ENABLED", "true")
    pub = SupabaseRealtimePublisher()
    assert pub.configured
    assert pub.enabled


# ---------------------------------------------------------------------------
# publish_outbound tests
# ---------------------------------------------------------------------------


async def test_publish_outbound_skips_when_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    pub = SupabaseRealtimePublisher()
    msg = _make_msg()
    # Should be a no-op, not raise.
    await pub.publish_outbound(msg)


async def test_publish_outbound_skips_retry_wait() -> None:
    pub = _FakePublisher()
    msg = _make_msg(event=RetryWaitEvent(content="waiting"))
    await pub.publish_outbound(msg)
    assert pub.captured == []


async def test_publish_outbound_skips_empty_chat_id() -> None:
    pub = _FakePublisher()
    msg = OutboundMessage(channel="telegram", chat_id="", content="hi")
    await pub.publish_outbound(msg)
    assert pub.captured == []


async def test_publish_outbound_captures_message() -> None:
    pub = _FakePublisher()
    msg = _make_msg(content="Task done!", channel="telegram", chat_id="42")
    await pub.publish_outbound(msg)
    assert len(pub.captured) == 1
    entry = pub.captured[0]
    assert entry["chat_id"] == "42"
    assert entry["channel"] == "telegram"
    assert entry["content"] == "Task done!"
    assert entry["event_type"] == "message"


async def test_publish_outbound_captures_delta() -> None:
    pub = _FakePublisher()
    msg = _make_msg(event=StreamDeltaEvent(content="chunk", stream_id="s1"))
    await pub.publish_outbound(msg)
    assert len(pub.captured) == 1
    assert pub.captured[0]["event_type"] == "delta"


async def test_publish_outbound_captures_turn_end() -> None:
    pub = _FakePublisher()
    msg = _make_msg(event=TurnEndEvent(latency_ms=200))
    await pub.publish_outbound(msg)
    assert len(pub.captured) == 1
    assert pub.captured[0]["event_type"] == "turn_end"


# ---------------------------------------------------------------------------
# HTTP integration tests (mocked httpx)
# ---------------------------------------------------------------------------


async def test_publish_outbound_makes_http_post(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_REALTIME_ENABLED", "true")

    captured_request: dict[str, Any] = {}

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured_request["url"] = str(request.url)
            captured_request["headers"] = dict(request.headers)
            captured_request["json"] = json.loads(request.content)
            return httpx.Response(201, text="")

    original_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = _MockTransport()
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)

    pub = SupabaseRealtimePublisher()
    msg = _make_msg(content="Done", chat_id="99")
    await pub.publish_outbound(msg)

    assert "agent_feedback" in captured_request["url"]
    body = captured_request["json"]
    assert body["chat_id"] == "99"
    assert body["content"] == "Done"
    assert body["event_type"] == "message"
    assert body["channel"] == "telegram"


async def test_publish_outbound_logs_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_REALTIME_ENABLED", "true")

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal error")

    original_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = _MockTransport()
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)

    pub = SupabaseRealtimePublisher()
    msg = _make_msg()
    # Should not raise even on HTTP 500.
    await pub.publish_outbound(msg)


async def test_publish_outbound_logs_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("SUPABASE_REALTIME_ENABLED", "true")

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

    original_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = _MockTransport()
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)

    pub = SupabaseRealtimePublisher()
    msg = _make_msg()
    # Should not raise even on network error.
    await pub.publish_outbound(msg)


# ---------------------------------------------------------------------------
# Content / metadata bounding tests
# ---------------------------------------------------------------------------


async def test_publish_outbound_truncates_long_content() -> None:
    pub = _FakePublisher()
    long_content = "x" * 20_000
    msg = _make_msg(content=long_content)
    await pub.publish_outbound(msg)
    assert len(pub.captured[0]["content"]) == 12_000


# ---------------------------------------------------------------------------
# ChannelManager wiring tests
# ---------------------------------------------------------------------------


async def test_channel_manager_publishes_to_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify ChannelManager._publish_realtime calls the publisher."""
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.schema import Config

    # Build a minimal config + bus.
    config = Config()
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()

    # Patch _init_channels to avoid loading real channel plugins.
    with patch.object(ChannelManager, "_init_channels", lambda self: None):
        manager = ChannelManager(config, bus)

    # Inject a fake publisher.
    fake_pub = MagicMock()
    fake_pub.publish_outbound = AsyncMock()
    manager._realtime_publisher = fake_pub

    msg = _make_msg(content="Test feedback", chat_id="chat-1")
    await manager._publish_realtime(msg)

    fake_pub.publish_outbound.assert_called_once_with(msg)


async def test_channel_manager_realtime_noop_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Supabase is not configured, _publish_realtime is a silent no-op."""
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.schema import Config

    config = Config()
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()

    # Ensure Supabase env vars are unset.
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    with patch.object(ChannelManager, "_init_channels", lambda self: None):
        manager = ChannelManager(config, bus)

    msg = _make_msg()
    # Should be a no-op, not raise.
    await manager._publish_realtime(msg)
    assert manager._realtime_publisher is None

