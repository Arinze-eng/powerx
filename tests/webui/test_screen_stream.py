"""Tests for the live GUI screen stream and its WebSocket wiring.

The capture path itself is exercised against a fake source rather than a real
display: the point here is the pump's behaviour (dedup, hydration, subscriber
lifecycle, teardown), not that ImageMagick works. The real capture is verified
separately against a live terminal.
"""

from __future__ import annotations

import asyncio
import shlex
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
    desktop_provision_command,
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


# ---------------------------------------------------------------------------
# Desktop provisioning: a bare sandbox has no display to capture
#
# Tenki sessions are stock Ubuntu with no Xvfb, no ImageMagick and no window
# manager (measured 2026-09-28), so the panel had nothing to show. The fix is to
# notice *that* failure specifically and install a desktop; these tests pin the
# detection, the once-only behaviour, the cap, and the shell the install uses.
# ---------------------------------------------------------------------------


def _bare_sandbox_source(monkeypatch, module, *, capture_failure: str | None = None):
    """A SandboxScreenSource whose sandbox is scripted to fail the capture."""
    calls: list[str] = []
    marker_failure = capture_failure or module.NO_CAPTURER_MARKER + ":99"

    async def fake_run(command, *, timeout=120, executor=None):
        calls.append(command)
        # The capture script is the one that echoes the marker; the provisioning
        # command only *contains* that text nowhere -- but it does contain
        # ``import -window`` inside its detached body, so dispatching on that
        # would read the installer as a capture and the install would never be
        # seen as started.
        if module.NO_CAPTURER_MARKER in command:
            if marker_failure:
                return False, marker_failure
            return True, "4096"
        return True, "provisioning-started"

    async def fake_root(session_key=None):
        return "/home/tenki"

    async def fake_fetch(remote_path, *, max_bytes=0, executor=None):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 32

    monkeypatch.setattr(module, "run_remote", fake_run)
    monkeypatch.setattr(module, "remote_workspace_root", fake_root)
    monkeypatch.setattr(module, "fetch_remote_file", fake_fetch)

    source = module.SandboxScreenSource(display=":99", size="1920x1080")
    source._executor = RemoteExecutor(name="tenki", backend=object())  # noqa: SLF001
    return source, calls


def _provision_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if "PX_DISPLAY=" in c]


def test_a_bare_sandbox_is_given_a_desktop(monkeypatch) -> None:
    from nanobot.webui import screen_stream as module

    source, calls = _bare_sandbox_source(monkeypatch, module)

    assert asyncio.run(source.capture()) is None

    started = _provision_calls(calls)
    assert len(started) == 1, "the install must be started exactly once per attempt"
    assert "PX_DISPLAY=:99" in started[0]
    assert "PX_SIZE=1920x1080" in started[0]
    # The panel is told what is happening; a blank rectangle for a minute reads
    # as a fault, which is the whole reason this message exists.
    assert source.last_error == module.PROVISIONING_MESSAGE


def test_the_install_is_not_restarted_while_it_runs(monkeypatch) -> None:
    """A poll every second must not launch a second apt inside the first."""
    from nanobot.webui import screen_stream as module

    source, calls = _bare_sandbox_source(monkeypatch, module)

    asyncio.run(source.capture())
    asyncio.run(source.capture())
    asyncio.run(source.capture())

    assert len(_provision_calls(calls)) == 1
    assert source.last_error == module.PROVISIONING_MESSAGE


def test_the_install_stops_being_retried_and_says_so(monkeypatch) -> None:
    from nanobot.webui import screen_stream as module

    source, calls = _bare_sandbox_source(monkeypatch, module)

    for attempt in range(module.PROVISION_MAX_ATTEMPTS):
        # Clear the in-flight window so each poll counts as a fresh attempt.
        source._provision_at = None  # noqa: SLF001 - the seam under test
        asyncio.run(source.capture())

    source._provision_at = None  # noqa: SLF001
    asyncio.run(source.capture())

    attempts = len(_provision_calls(calls))
    assert attempts == module.PROVISION_MAX_ATTEMPTS
    assert source.last_error == module.PROVISION_FAILED_MESSAGE


def test_an_unrelated_capture_failure_does_not_install_anything(monkeypatch) -> None:
    """Only "no capturer" means a bare image; anything else is a real fault."""
    from nanobot.webui import screen_stream as module

    source, calls = _bare_sandbox_source(
        monkeypatch, module, capture_failure="capture command failed: display busy"
    )

    assert asyncio.run(source.capture()) is None

    assert _provision_calls(calls) == []
    assert source.last_error == "capture command failed: display busy"


@pytest.mark.parametrize(
    "hostile", ["", "   ", ":99; rm -rf /", ":99$(id)", "localhost:99", ":", None, 42]
)
def test_provisioning_refuses_a_display_that_is_not_one(hostile) -> None:
    """The display reaches a shell as the sandbox user, with sudo available."""
    command = desktop_provision_command(hostile, "/home/tenki/.powerx-screen")

    assert "PX_DISPLAY=:99 " in command
    assert "rm -rf" not in command
    assert "$(id)" not in command
    assert "localhost" not in command


@pytest.mark.parametrize("hostile", ["", "1920x1080; rm -rf /", "$(id)", "1920"])
def test_provisioning_refuses_a_geometry_that_is_not_one(hostile) -> None:
    command = desktop_provision_command(":99", "/home/tenki/.powerx-screen", hostile)

    assert "PX_SIZE=1920x1080" in command
    assert "rm -rf" not in command


def test_the_provisioning_body_survives_sh_c_wrapping() -> None:
    """The body is spliced into ``sh -c '<body>'``, so it cannot contain a quote.

    An apostrophe anywhere in it would close the wrapper and hand the rest of the
    install to the sandbox as a second command -- checked as an invariant rather
    than trusted, because the body is edited by hand.
    """
    from nanobot.webui import screen_stream as module

    body = module._PROVISION_BODY  # noqa: SLF001 - the invariant under test
    assert "'" not in body

    command = desktop_provision_command(":99", "/home/tenki/.powerx-screen", "1280x1024")
    # The wrapped body must round-trip as exactly one argument, or the install
    # would run truncated.
    assert body in shlex.split(command)


def test_the_provisioning_body_takes_the_apt_lock() -> None:
    """Both writers of apt on one sandbox must serialise, or both lose.

    MEASURED FAILURE (2026-09-28, live Tenki session, MT5 + Live screen together):
    ``install_mt5_sandbox.sh`` was running its own ``apt-get install xvfb ...``
    when the pump, finding no capturer, started a second apt. The installer's
    transaction then died with

      dpkg: error processing archive ....deb (--unpack):
            cannot access archive ....deb: No such file or directory

    for the last sixteen packages -- xvfb among them -- and the pump's install
    answered ``E: Could not get lock /var/lib/dpkg/lock-frontend``. Net result:
    no display, so no frames, which is exactly the bug this provisioning exists
    to fix. Novita never showed it because that image already ships Xvfb.
    """
    from nanobot.webui import screen_stream as module

    body = module._PROVISION_BODY  # noqa: SLF001 - the invariant under test

    # A shared lock every apt writer on the sandbox takes, with the whole wait
    # bounded so a poll thread cannot be pinned forever.
    assert module.APT_LOCK_PATH in body
    assert f"flock -w {module.APT_LOCK_TIMEOUT_S} 9" in body
    # And apt's own patience as well: the lock file only helps while both sides
    # are updated copies of this repo, these help regardless.
    assert f"APT::Lock::Timeout={module.APT_LOCK_TIMEOUT_S}" in body
    assert f"DPkg::Lock::Timeout={module.APT_LOCK_TIMEOUT_S}" in body
    # A single lost transaction is not a verdict on the sandbox, so the install
    # is retried -- including with --fix-missing, which is what repairs the
    # half-unpacked archive list a lost transaction leaves behind.
    assert "while [ \"$n\" -lt 4 ]" in body
    assert "--fix-missing install" in body
    # The lock is only worth taking where flock exists; without it the install
    # still runs rather than skipping itself into a desktop-less sandbox.
    assert "command -v flock" in body


def test_the_install_is_detached_into_its_own_session() -> None:
    """On Tenki a backgrounded install still blocks the caller's shell call.

    MEASURED (2026-09-28): the detached install took 72 s of wall clock on the
    caller even though it backgrounds its work, because the sandbox's ``shell``
    call does not return until the process group does. ``setsid`` plus
    ``< /dev/null`` gives it its own session with no controlling terminal, so it
    survives the caller's timeout -- and the caller's timeout is raised to match
    the longest the lock wait can legally take rather than the install itself.
    """
    from nanobot.webui import screen_stream as module

    command = desktop_provision_command(":99", "/home/tenki/.powerx-screen")

    assert "setsid nohup" in command
    assert "< /dev/null &" in command
    # A sandbox without setsid must still get the install, not a syntax error.
    assert "else nohup" in command
    assert command.rstrip().endswith("echo provisioning-started")


def test_the_provision_call_outlasts_the_apt_lock_wait(monkeypatch) -> None:
    """The caller's ceiling must not land on an install that is merely queueing."""
    from nanobot.webui import screen_stream as module

    timeouts: list[float] = []

    async def fake_run(command, *, timeout=120, executor=None):
        timeouts.append(timeout)
        return False, module.NO_CAPTURER_MARKER + ":99"

    async def fake_root(session_key=None):
        return "/home/tenki"

    monkeypatch.setattr(module, "run_remote", fake_run)
    monkeypatch.setattr(module, "remote_workspace_root", fake_root)

    source = module.SandboxScreenSource(display=":99", size="1920x1080")
    source._executor = RemoteExecutor(name="tenki", backend=object())  # noqa: SLF001

    assert asyncio.run(source.capture()) is None

    # capture, then provisioning.
    assert len(timeouts) == 2
    assert timeouts[1] > module.APT_LOCK_TIMEOUT_S, (
        "the provisioning call must outlast a legal lock wait, not the install"
    )


def test_the_retry_window_spans_a_metatrader_install() -> None:
    """One apt writer finishes, then the other: the wait cannot be 45 s.

    A WineHQ install that holds apt measured 2 m 21 s on a live Tenki session
    (2026-09-28), and while it holds apt the pump's own install is only queueing.
    Re-running the install every 45 s would stack a third attempt on top of two
    that have not had a turn yet, so the window and the attempt budget are sized
    to outlast an installer.
    """
    from nanobot.webui import screen_stream as module

    assert module.PROVISION_RETRY_S >= 90.0
    assert module.PROVISION_MAX_ATTEMPTS * module.PROVISION_RETRY_S >= 360.0


def test_the_window_manager_check_cannot_match_its_own_shell() -> None:
    """Both halves of this were live bugs; neither may come back.

    ``pgrep -f matchbox-window-manager`` matched the shell *running the body*,
    because that shell's command line contains the pattern -- so the check
    decided a window manager was already up and never started one. The pattern is
    ``-x`` (process name) and truncated to 15 characters because that is where the
    kernel truncates it. And ``pgrep ... || cmd &`` backgrounds the whole and-or
    list, racing the script past the start, so every backgrounded start is wrapped
    in ``if``.
    """
    from nanobot.webui import screen_stream as module

    body = module._PROVISION_BODY  # noqa: SLF001 - the invariant under test
    assert "pgrep -x matchbox-window >/dev/null 2>&1" in body
    assert "pgrep -f matchbox-window-manager" not in body
    assert "if ! pgrep -x matchbox-window >/dev/null 2>&1; then" in body
    assert "if ! pgrep -f \"Xvfb $D\" >/dev/null 2>&1; then" in body


# ---------------------------------------------------------------------------
# Error sinks: an empty panel has to say why
# ---------------------------------------------------------------------------


class ErrorSinkRecorder:
    def __init__(self) -> None:
        self.details: list[str] = []

    async def __call__(self, detail: str) -> None:
        self.details.append(detail)


async def test_a_failing_stream_explains_itself_to_the_error_sink() -> None:
    """No frame means no explanation, so the diagnostic travels on its own event."""
    source = FakeSource([None, None, None])
    stream = ScreenStream("err1", source, interval_s=_FAST, keepalive_s=60.0)
    errors = ErrorSinkRecorder()
    await stream.add_error_sink(errors)

    await stream.subscribe(Sink())
    await _settle(stream, _WINDOW)

    assert errors.details, "an empty panel must be told why it is empty"
    assert errors.details[0] == "fake source ran out of frames"
    # Repeated identical diagnostics are dropped: the pump re-diagnoses on every
    # backoff and the operator does not need the same sentence once a second.
    assert len(set(errors.details)) == 1
    await stream.stop()


async def test_the_error_sink_is_told_immediately_when_one_is_added_late() -> None:
    source = FakeSource([None, None])
    stream = ScreenStream("err2", source, interval_s=_FAST, keepalive_s=60.0)
    await stream.subscribe(Sink())
    await _settle(stream, _WINDOW)
    assert stream.error is not None

    errors = ErrorSinkRecorder()
    await stream.add_error_sink(errors)

    assert errors.details == [stream.error]
    await stream.stop()


async def test_a_removed_error_sink_is_not_told_again() -> None:
    source = FakeSource([None, None])
    stream = ScreenStream("err3", source, interval_s=_FAST, keepalive_s=60.0)
    errors = ErrorSinkRecorder()
    await stream.add_error_sink(errors)
    await stream.remove_error_sink(errors)

    await stream.subscribe(Sink())
    await _settle(stream, _WINDOW)

    assert errors.details == []
    await stream.stop()


async def test_recording_frames_does_not_notify_error_sinks() -> None:
    """Recovery is carried by the frame itself, not by a second event."""
    source = FakeSource([_png(fill=1), _png(fill=2)])
    stream = ScreenStream("err4", source, interval_s=_FAST, keepalive_s=60.0)
    errors = ErrorSinkRecorder()
    await stream.add_error_sink(errors)

    await stream.subscribe(Sink())
    await _settle(stream, _WINDOW)

    assert errors.details == []
    await stream.stop()
