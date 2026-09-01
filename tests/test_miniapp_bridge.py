"""Tests for the gateway chat bridge (miniapp_bridge) and gateway /app routes."""

from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.api.miniapp_bridge import MiniAppBridge
from nanobot.api.telegram_auth import miniapp_tokens


class FakeBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish_inbound(self, msg) -> None:
        self.published.append(msg)


class FakeChannel:
    """Mimics the WebSocketChannel seams used by the bridge."""

    def __init__(self) -> None:
        self._subs: dict[str, set] = {}
        self._conn_chats: dict[object, set] = {}
        self.bus = FakeBus()

    def _attach(self, conn, chat_id: str) -> None:
        self._subs.setdefault(chat_id, set()).add(conn)
        self._conn_chats.setdefault(conn, set()).add(chat_id)

    async def deliver(self, chat_id: str, frame: dict) -> None:
        raw = json.dumps(frame)
        for conn in list(self._subs.get(chat_id, ())):
            await conn.send(raw)


@pytest.fixture()
def token() -> str:
    return miniapp_tokens.mint("42")


@pytest.mark.asyncio
async def test_bridge_streams_message_and_turn_end(token) -> None:
    bridge = MiniAppBridge()
    channel = FakeChannel()
    bridge.wire(channel)

    received: list[bytes] = []

    async def consume() -> None:
        async for chunk in bridge.run_turn(
            token=token, content="hi there", media_paths=[]
        ):
            received.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)

    assert len(channel.bus.published) == 1
    msg = channel.bus.published[0]
    assert msg.channel == "telegram"
    assert msg.chat_id == "42"
    assert msg.session_key_override == "telegram:42"

    await channel.deliver("telegram:42", {"event": "message", "text": "Hello!"})
    await channel.deliver("telegram:42", {"event": "turn_end"})
    await asyncio.wait_for(task, 5)

    out = b"".join(received).decode()
    assert '"delta": "Hello!"' in out
    assert "[DONE]" in out
    # Sink detached after the turn.
    assert not channel._subs.get("telegram:42")


@pytest.mark.asyncio
async def test_bridge_surfaces_error_frames(token) -> None:
    bridge = MiniAppBridge()
    channel = FakeChannel()
    bridge.wire(channel)
    got: list[bytes] = []

    async def consume() -> None:
        async for chunk in bridge.run_turn(token=token, content="x", media_paths=[]):
            got.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await channel.deliver("telegram:42", {"event": "error", "detail": "boom"})
    await asyncio.wait_for(task, 5)
    assert b'"error": "boom"' in b"".join(got)


@pytest.mark.asyncio
async def test_bridge_rejects_unknown_token() -> None:
    bridge = MiniAppBridge()
    bridge.wire(FakeChannel())
    with pytest.raises(LookupError):
        async for _ in bridge.run_turn(token="nope", content="x", media_paths=[]):
            pass


@pytest.mark.asyncio
async def test_bridge_requires_wired_channel(token) -> None:
    bridge = MiniAppBridge()
    with pytest.raises(RuntimeError):
        async for _ in bridge.run_turn(token=token, content="x", media_paths=[]):
            pass


@pytest.mark.asyncio
async def test_bridge_cleans_sink_on_timeout(monkeypatch, token) -> None:
    monkeypatch.setattr("nanobot.api.miniapp_bridge.TURN_TIMEOUT_SECONDS", 0.2)
    bridge = MiniAppBridge()
    channel = FakeChannel()
    bridge.wire(channel)
    chunks = []
    async for chunk in bridge.run_turn(token=token, content="x", media_paths=[]):
        chunks.append(chunk)
    assert b"timed out" in b"".join(chunks)
    assert not channel._subs.get("telegram:42")
