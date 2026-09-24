"""Tests for the live GUI screen stream and its WebSocket wiring.

The capture path itself is exercised against a fake source rather than a real
display: the point here is the pump's behaviour (dedup, hydration, subscriber
lifecycle, teardown), not that ImageMagick works. The real capture is verified
separately against a live terminal.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.tools.workspace_bridge import RemoteExecutor
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig, _clean_display
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.screen_stream import (
    LocalScreenSource,
    ScreenSource,
    ScreenFrame,
    ScreenStream,
    ScreenStreamManager,
    image_size,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PORT = 29901

#: The fastest interval the stream will actually honour. ``ScreenStream`` clamps
#: to ``MIN_INTERVAL_S`` because a capture costs ~0.2 s of the budget, so asking
#: for 10 ms would just queue captures back to back. Tests use the real minimum
#: rather than monkeypatching it, so the clamp stays covered.
_FAST = 0.25

#: Enough wall clock for a few polls at ``_FAST``, so assertions about "several
#: frames" are about behaviour rather than about scheduler luck.
_WINDOW = 1.0


def _png(width: int = 4, height: int = 3, *, fill: int = 0) -> bytes:
    """A minimal but structurally valid PNG header plus a distinguishing tail."""
    header = (
        _PNG_MAGIC
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, 6, 0, 0, 0])
    )
    return header + bytes([fill] * 8)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_image_size_reads_png_ihdr() -> None:
    assert image_size(_png(1920, 1080)) == (1920, 1080)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff\xd8\xff\xe0 not a png at all",
        _PNG_MAGIC,  # too short to carry an IHDR
        _PNG_MAGIC + b"\x00" * 40,  # right length, wrong chunk name
    ],
)
def test_image_size_rejects_non_png(data: bytes) -> None:
    assert image_size(data) is None


@pytest.mark.parametrize("value", [":99", ":0", ":127", ":99.0", ":99.12"])
def test_clean_display_accepts_real_display_specs(value: str) -> None:
    assert _clean_display(value) == value


@pytest.mark.parametrize(
    "value",
    [
        ":99; rm -rf /",
        ":99$(id)",
        "localhost:99",
        "99",
        ":",
        ":99.999",
        "",
        "   ",
        None,
        42,
    ],
)
def test_clean_display_refuses_anything_that_is_not_a_display(value: Any) -> None:
    assert _clean_display(value) is None


# ---------------------------------------------------------------------------
# Fake source + sinks
# ---------------------------------------------------------------------------


class FakeSource:
    """Yields a scripted sequence of frames, then repeats the last one."""

    def __init__(
        self, frames: list[bytes | None], display: str = ":99", location: str = "sandbox"
    ) -> None:
        self._frames = frames
        self.display = display
        self.location = location
        self.calls = 0
        self.resolved = 0

    async def resolve(self) -> None:
        self.resolved += 1

    async def capture(self) -> bytes | None:
        index = min(self.calls, len(self._frames) - 1)
        self.calls += 1
        return self._frames[index]

    async def diagnostic(self) -> str:
        return "fake source ran out of frames"


class Sink:
    """Records what the stream hands it."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.frames: list[tuple[ScreenFrame, bool]] = []
        self._fail_after = fail_after
        self.calls = 0

    async def __call__(self, frame: ScreenFrame, *, replay: bool = False) -> None:
        self.calls += 1
        if self._fail_after is not None and self.calls > self._fail_after:
            raise RuntimeError("transport closed")
        self.frames.append((frame, replay))


async def _settle(stream: ScreenStream, seconds: float = 0.25) -> None:
    """Give the pump time to run without making the suite slow."""
    await asyncio.sleep(seconds)
    task = stream._task  # noqa: SLF001 - tests inspect the pump directly
    if task is not None:
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# ScreenFrame payload
# ---------------------------------------------------------------------------


def test_payload_carries_base64_image_and_geometry() -> None:
    raw = _png(800, 600)
    frame = ScreenFrame(
        seq=3,
        data=raw,
        content_type="image/png",
        captured_at=123.0,
        width=800,
        height=600,
        changed=True,
        display=":99",
    )
    payload = frame.payload()

    assert payload["seq"] == 3
    assert payload["width"] == 800
    assert payload["height"] == 600
    assert payload["changed"] is True
    assert payload["bytes"] == len(raw)
    assert base64.b64decode(payload["image"]) == raw
    assert "replay" not in payload

    assert frame.payload(replay=True)["replay"] is True


# ---------------------------------------------------------------------------
# Pump behaviour
# ---------------------------------------------------------------------------


async def test_pump_pushes_frames_to_a_subscriber() -> None:
    source = FakeSource([_png(fill=1), _png(fill=2), _png(fill=3)])
    stream = ScreenStream("s1", source, interval_s=_FAST, keepalive_s=60.0)
    sink = Sink()

    await stream.subscribe(sink)
    await _settle(stream, _WINDOW)

    assert len(sink.frames) >= 2
    assert all(isinstance(f, ScreenFrame) for f, _ in sink.frames)
    assert stream.error is None
    await stream.stop()


async def test_identical_frames_are_suppressed() -> None:
    """The whole point of byte comparison: a static screen costs one frame."""
    same = _png(fill=7)
    source = FakeSource([same, same, same, same, same])
    stream = ScreenStream("s2", source, interval_s=_FAST, keepalive_s=60.0)
    sink = Sink()

    await stream.subscribe(sink)
    await _settle(stream, _WINDOW)

    assert source.calls >= 3, "the pump should keep polling"
    assert len(sink.frames) == 1, "only the first frame should reach the wire"
    assert sink.frames[0][0].changed is True
    await stream.stop()


async def test_keepalive_repushes_an_unchanged_frame() -> None:
    """A static screen must still refresh, or a late viewer sees a stale image."""
    same = _png(fill=9)
    source = FakeSource([same] * 20)
    stream = ScreenStream("s3", source, interval_s=_FAST, keepalive_s=0.5)
    sink = Sink()

    await stream.subscribe(sink)
    await _settle(stream, 1.4)

    # The re-pushes must be flagged as unchanged, so the UI can avoid repainting.
    assert len(sink.frames) >= 2, "a static screen must still refresh a viewer"
    assert sink.frames[0][0].changed is True
    assert any(not frame.changed for frame, _ in sink.frames[1:]), (
        "a keepalive re-push must be marked unchanged, not as new content"
    )
    await stream.stop()


async def test_late_subscriber_is_hydrated_immediately() -> None:
    source = FakeSource([_png(fill=1), _png(fill=2)])
    stream = ScreenStream("s4", source, interval_s=_FAST, keepalive_s=60.0)
    first = Sink()
    late = Sink()

    await stream.subscribe(first)
    await _settle(stream, 0.4)
    await stream.subscribe(late)

    assert late.frames, "a subscriber joining a live stream must not wait for a change"
    frame, replay = late.frames[0]
    assert replay is True
    assert frame.seq == stream.last_frame.seq  # type: ignore[union-attr]
    await stream.stop()


async def test_first_subscriber_has_nothing_to_replay() -> None:
    """Hydration only applies once a frame exists; the first viewer waits for it."""
    source = FakeSource([_png()])
    stream = ScreenStream("s5", source, interval_s=10.0, keepalive_s=60.0)
    sink = Sink()

    await stream.subscribe(sink)

    assert sink.calls == 0, "no frame exists yet, so nothing can be replayed"
    await stream.stop()


async def test_a_failing_sink_is_dropped_and_the_rest_keep_receiving() -> None:
    source = FakeSource([_png(fill=1), _png(fill=2), _png(fill=3), _png(fill=4)])
    stream = ScreenStream("s6", source, interval_s=_FAST, keepalive_s=60.0)
    dead = Sink(fail_after=1)
    alive = Sink()

    await stream.subscribe(dead)
    await stream.subscribe(alive)
    await _settle(stream, _WINDOW)

    assert stream.subscriber_count == 1
    assert alive.frames, "one closed transport must not silence the others"
    await stream.stop()


async def test_capture_failure_sets_error_and_does_not_kill_the_pump() -> None:
    source = FakeSource([None, None, _png(fill=5)])
    stream = ScreenStream("s7", source, interval_s=_FAST, keepalive_s=60.0)
    sink = Sink()

    await stream.subscribe(sink)
    await asyncio.sleep(0.05)
    assert stream.error == "fake source ran out of frames"

    # The backoff is seconds-long; assert the pump is still alive rather than
    # waiting it out.
    assert stream._task is not None and not stream._task.done()  # noqa: SLF001
    await stream.stop()


async def test_stop_cancels_the_pump_and_clears_subscribers() -> None:
    source = FakeSource([_png(fill=1)] * 50)
    stream = ScreenStream("s8", source, interval_s=_FAST, keepalive_s=60.0)
    sink = Sink()

    await stream.subscribe(sink)
    await _settle(stream, 0.4)
    await stream.stop()

    assert stream.subscriber_count == 0
    assert stream._task is None  # noqa: SLF001
    before = source.calls
    await asyncio.sleep(0.3)
    assert source.calls == before, "a stopped pump must not keep capturing"


async def test_last_unsubscribe_ends_the_pump_but_one_left_keeps_it() -> None:
    source = FakeSource([_png(fill=1)] * 50)
    stream = ScreenStream("s9", source, interval_s=_FAST, keepalive_s=60.0)
    a = Sink()
    b = Sink()

    await stream.subscribe(a)
    await stream.subscribe(b)
    await _settle(stream, 0.4)

    await stream.unsubscribe(a)
    assert stream.subscriber_count == 1
    assert stream._task is not None and not stream._task.done()  # noqa: SLF001

    await stream.unsubscribe(b)
    assert stream._task is None  # noqa: SLF001


async def test_interval_is_clamped_to_a_sane_range() -> None:
    assert ScreenStream("x", FakeSource([_png()]), interval_s=0.0).interval_s == 0.25
    assert ScreenStream("x", FakeSource([_png()]), interval_s=999.0).interval_s == 10.0


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


async def test_manager_reuses_one_stream_per_key() -> None:
    manager = ScreenStreamManager(lambda display, session_key: FakeSource([_png()], display=display or ":99"))
    a, b = Sink(), Sink()

    first = await manager.subscribe("chat-1", a, interval_s=_FAST)
    second = await manager.subscribe("chat-1", b, interval_s=_FAST)

    assert first is second
    assert first.subscriber_count == 2
    assert manager.active_keys == ["chat-1"]
    await manager.shutdown()


async def test_manager_drops_the_stream_when_the_last_viewer_leaves() -> None:
    manager = ScreenStreamManager(lambda display, session_key: FakeSource([_png()] * 50))
    sink = Sink()

    stream = await manager.subscribe("chat-2", sink, interval_s=_FAST)
    await manager.unsubscribe("chat-2", sink)

    assert manager.active_keys == []
    assert stream.subscriber_count == 0


async def test_manager_release_sink_detaches_across_every_stream() -> None:
    manager = ScreenStreamManager(lambda display, session_key: FakeSource([_png()] * 50))
    sink = Sink()

    await manager.subscribe("chat-a", sink, interval_s=_FAST)
    await manager.subscribe("chat-b", sink, interval_s=_FAST)
    assert len(manager.active_keys) == 2

    await manager.release_sink(sink)

    assert manager.active_keys == [], "a disconnected client must not leave pumps running"


async def test_shutdown_stops_every_stream() -> None:
    manager = ScreenStreamManager(lambda display, session_key: FakeSource([_png()] * 50))
    await manager.subscribe("chat-a", Sink(), interval_s=_FAST)
    await manager.subscribe("chat-b", Sink(), interval_s=_FAST)

    await manager.shutdown()

    assert manager.active_keys == []


async def test_manager_resolves_the_source_before_the_ack_can_name_it() -> None:
    """A lazily-chosen source would otherwise report the wrong machine."""
    sources: list[FakeSource] = []

    def factory(display: str | None, session_key: str | None) -> ScreenSource:
        source = FakeSource([_png()], location="host")
        sources.append(source)
        return source

    manager = ScreenStreamManager(factory)
    stream = await manager.subscribe("chat-1", Sink(), interval_s=_FAST)

    assert sources[0].resolved == 1, "the source must settle its choice before the ack"
    assert stream.source.location == "host"
    await manager.shutdown()


async def test_manager_hands_the_session_key_to_the_source_factory() -> None:
    """The pump has no request context, so the sandbox key must travel with it."""
    seen: list[tuple[str | None, str | None]] = []

    def factory(display: str | None, session_key: str | None) -> ScreenSource:
        seen.append((display, session_key))
        return FakeSource([_png()])

    manager = ScreenStreamManager(factory)
    await manager.subscribe(
        "chat-1", Sink(), display=":77", interval_s=_FAST, session_key="websocket:chat-1"
    )

    assert seen == [(":77", "websocket:chat-1")]
    await manager.shutdown()


# ---------------------------------------------------------------------------
# Session key plumbing
# ---------------------------------------------------------------------------


def test_sandbox_source_forwards_the_session_key_to_resolution(monkeypatch) -> None:
    """Without this the pump resolves ``"unknown"`` and shows nothing."""
    from nanobot.webui import screen_stream as module

    captured: list[str | None] = []

    async def fake_resolve(session_key: str | None = None):
        captured.append(session_key)
        return RemoteExecutor(name="unavailable")

    monkeypatch.setattr(module, "resolve_remote_executor", fake_resolve)
    source = module.SandboxScreenSource(session_key="websocket:abc")

    asyncio.run(source._resolve())  # noqa: SLF001 - the seam under test

    assert captured == ["websocket:abc"]


class _StubExecutor:
    def __init__(self, name: str, available: bool) -> None:
        self.name = name
        self._available = available

    @property
    def available(self) -> bool:
        return self._available


def test_host_display_reachability_is_a_socket_check() -> None:
    from nanobot.webui.screen_stream import host_display_is_reachable

    # A TCP display is somebody else's machine; it must never be mistaken for
    # the gateway's own desktop.
    assert host_display_is_reachable("example.com:0") is False
    assert host_display_is_reachable("nonsense") is False
    assert host_display_is_reachable(":notanumber") is False
    # Resolution is not a property of the spec, so :59999 must not be assumed up.
    assert host_display_is_reachable(":59999") is False


@pytest.mark.parametrize(
    ("name", "available", "reachable", "expect_host"),
    [
        # No sandbox handle and a live local display: this box is the GUI box.
        ("novita", False, True, True),
        # No sandbox handle and nothing local: nothing to show, so keep the
        # sandbox source whose diagnostic explains the absence.
        ("novita", False, False, False),
        # A configured sandbox is used as-is even when it is down: showing the
        # gateway host instead would be showing the wrong machine.
        ("novita", True, True, False),
        ("runloop", True, False, False),
    ],
)
def test_auto_source_picks_the_machine_that_actually_has_the_display(
    monkeypatch, name: str, available: bool, reachable: bool, expect_host: bool
) -> None:
    from nanobot.webui import screen_stream as module

    calls: list[str | None] = []

    async def _patched_resolve(session_key: str | None = None):
        calls.append(session_key)
        return _StubExecutor(name, available)

    monkeypatch.setattr(module, "resolve_remote_executor", _patched_resolve)
    monkeypatch.setattr(module, "host_display_is_reachable", lambda display: reachable)

    source = module.AutoScreenSource(display=":99", session_key="websocket:abc")
    asyncio.run(source._pick())  # noqa: SLF001

    assert isinstance(source._delegate, LocalScreenSource) is expect_host  # noqa: SLF001
    assert calls == ["websocket:abc"]
    assert source.location == ("host" if expect_host else "sandbox")


# ---------------------------------------------------------------------------
# WebSocket wiring
# ---------------------------------------------------------------------------


class FakeConnection:
    """Minimal stand-in for a ServerConnection that records outgoing events."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.sent if e.get("event") == name]


def _channel(bus: Any) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": _PORT,
        "path": "/ws",
        "websocketRequiresToken": False,
    }
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(cfg, bus, gateway=gateway)


def _scripted_channel(frame_bytes: list[bytes | None] | None = None) -> WebSocketChannel:
    """A channel whose screen manager captures from a scripted source."""
    channel = _channel(MessageBus())
    payload = frame_bytes or [_png(fill=1), _png(fill=2), _png(fill=3)]
    channel._screens = ScreenStreamManager(  # noqa: SLF001 - test seam
        lambda display, session_key: FakeSource(list(payload), display=display or ":99")
    )
    return channel


async def test_screen_subscribe_acks_and_streams_frames() -> None:
    channel = _scripted_channel()
    connection = FakeConnection()

    await channel._dispatch_envelope(  # noqa: SLF001
        connection, "client-1", {"type": "screen_subscribe", "chat_id": "chat-1", "interval_s": 0.01}
    )
    await asyncio.sleep(0.15)

    acks = connection.events("screen_subscribed")
    assert acks and acks[0]["chat_id"] == "chat-1"
    assert acks[0]["display"] == ":99"
    assert acks[0]["location"] == "sandbox"

    frames = connection.events("screen_frame")
    assert frames, "subscribing must start delivering frames"
    assert frames[0]["chat_id"] == "chat-1"
    assert frames[0]["content_type"] == "image/png"
    assert base64.b64decode(frames[0]["image"]).startswith(_PNG_MAGIC)

    await channel._screens.shutdown()  # noqa: SLF001


async def test_screen_subscribe_names_the_chats_own_sandbox() -> None:
    """The pump has no request context, so the sandbox key is reconstructed here."""
    seen: list[str | None] = []
    channel = _channel(MessageBus())
    channel._screens = ScreenStreamManager(  # noqa: SLF001 - test seam
        lambda display, session_key: (seen.append(session_key), FakeSource([_png()]))[1]
    )
    connection = FakeConnection()

    await channel._dispatch_envelope(  # noqa: SLF001
        connection, "c", {"type": "screen_subscribe", "chat_id": "chat-1"}
    )

    assert seen == ["websocket:chat-1"]
    await channel._screens.shutdown()  # noqa: SLF001


async def test_screen_subscribe_uses_the_connection_default_chat() -> None:
    channel = _scripted_channel()
    connection = FakeConnection()
    channel._conn_default[connection] = "chat-default"  # noqa: SLF001

    await channel._dispatch_envelope(connection, "c", {"type": "screen_subscribe"})  # noqa: SLF001
    await asyncio.sleep(0.05)

    assert connection.events("screen_subscribed")[0]["chat_id"] == "chat-default"
    await channel._screens.shutdown()  # noqa: SLF001


async def test_screen_subscribe_without_a_chat_reports_an_error() -> None:
    channel = _scripted_channel()
    connection = FakeConnection()

    await channel._dispatch_envelope(connection, "c", {"type": "screen_subscribe"})  # noqa: SLF001

    assert connection.events("screen_error")
    assert not connection.events("screen_frame")
    await channel._screens.shutdown()  # noqa: SLF001


async def test_ack_reports_a_host_capture_as_such() -> None:
    """A local/self-hosted box captures on the gateway, and must not claim otherwise."""
    channel = _channel(MessageBus())
    channel._screens = ScreenStreamManager(  # noqa: SLF001 - test seam
        lambda display, session_key: FakeSource([_png()], location="host")
    )
    connection = FakeConnection()

    await channel._dispatch_envelope(  # noqa: SLF001
        connection, "c", {"type": "screen_subscribe", "chat_id": "chat-1"}
    )

    assert connection.events("screen_subscribed")[0]["location"] == "host"
    await channel._screens.shutdown()  # noqa: SLF001


async def test_screen_unsubscribe_stops_the_stream() -> None:
    channel = _scripted_channel()
    connection = FakeConnection()

    await channel._dispatch_envelope(  # noqa: SLF001
        connection, "c", {"type": "screen_subscribe", "chat_id": "chat-1", "interval_s": 0.01}
    )
    await asyncio.sleep(0.05)
    await channel._dispatch_envelope(  # noqa: SLF001
        connection, "c", {"type": "screen_unsubscribe", "chat_id": "chat-1"}
    )

    assert connection.events("screen_unsubscribed")[0]["chat_id"] == "chat-1"
    assert channel._screens.active_keys == []  # noqa: SLF001


async def test_disconnect_releases_screen_sinks_and_stops_the_pump() -> None:
    channel = _scripted_channel([_png(fill=1)] * 50)
    connection = FakeConnection()

    await channel._dispatch_envelope(  # noqa: SLF001
        connection, "c", {"type": "screen_subscribe", "chat_id": "chat-1", "interval_s": 0.01}
    )
    await asyncio.sleep(0.05)
    assert channel._screens.active_keys == ["chat-1"]  # noqa: SLF001

    await channel._cleanup_connection(connection)  # noqa: SLF001

    assert channel._screens.active_keys == []  # noqa: SLF001
    stream = channel._screens._streams.get("chat-1")  # noqa: SLF001
    assert stream is None


async def test_subscribing_twice_does_not_double_deliver() -> None:
    """A panel that re-subscribes (reconnect) must not create two pumps."""
    channel = _scripted_channel()
    connection = FakeConnection()

    for _ in range(2):
        await channel._dispatch_envelope(  # noqa: SLF001
            connection, "c", {"type": "screen_subscribe", "chat_id": "chat-1", "interval_s": 0.01}
        )
    await asyncio.sleep(0.15)

    stream = channel._screens.stream("chat-1")  # noqa: SLF001
    assert stream is not None
    assert stream.subscriber_count == 1, "the same connection must hold one sink, not two"
    await channel._screens.shutdown()  # noqa: SLF001


async def test_invalid_display_falls_back_to_the_default() -> None:
    channel = _scripted_channel()
    connection = FakeConnection()

    await channel._dispatch_envelope(  # noqa: SLF001
        connection, "c", {"type": "screen_subscribe", "chat_id": "chat-1", "display": ":99; rm -rf /"}
    )

    # The injection attempt is dropped, so the source keeps its own default.
    assert connection.events("screen_subscribed")[0]["display"] == ":99"
    await channel._screens.shutdown()  # noqa: SLF001


async def test_channel_stop_shuts_down_screen_streams() -> None:
    channel = _scripted_channel([_png(fill=1)] * 50)
    connection = FakeConnection()
    await channel._dispatch_envelope(  # noqa: SLF001
        connection, "c", {"type": "screen_subscribe", "chat_id": "chat-1", "interval_s": 0.01}
    )
    await asyncio.sleep(0.05)
    channel._running = True  # noqa: SLF001 - stop() is a no-op otherwise

    await channel.stop()

    assert channel._screens.active_keys == []  # noqa: SLF001


# ---------------------------------------------------------------------------
# Local host source
# ---------------------------------------------------------------------------


async def test_local_source_reports_a_diagnostic_on_a_missing_display() -> None:
    """A missing display must produce a readable reason, not an exception."""
    source = LocalScreenSource(display=":987")

    data = await source.capture()

    assert data is None
    assert await source.diagnostic()
