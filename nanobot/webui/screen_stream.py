"""Live frames of the sandbox's GUI desktop, pushed to the WebUI.

Why this exists and why it is shaped this way
---------------------------------------------
MetaTrader — and any other GUI app the agent drives — lives inside the execution
sandbox on an Xvfb display. There is **no way to dial into that sandbox**: none
of the backends expose an inbound port (no ``expose_port`` / ``port_forward`` /
``public_url`` anywhere in ``nanobot/agent/tools``), so the conventional
``x11vnc`` + ``noVNC`` setup, which needs a listening TCP port, cannot bind. The
pixels therefore have to be *pulled out* of the sandbox and pushed down the
WebSocket the WebUI already holds.

The alternative that looks easiest — publishing frames through
``nanobot.utils.file_share`` (catbox / onlyfiles) — is deliberately not used. The
sandbox holds live broker credentials, so uploading frames there would publish a
trading terminal, its account number and its open positions, to a public URL.

Numbers this file is built on, measured against a live MT5 terminal
(2026-09-24, 1920x1080):

* 1080p PNG of the terminal: **85 KB**. The same frame as JPEG q60: 272 KB, and
  downscaling it to 720p made the PNG **3.4x larger** — interpolation noise
  defeats deflate where flat UI colours and sharp text compress beautifully. So:
  capture at native geometry and ship PNG. Never resize, never assume JPEG.
* Capture costs ~180 ms (ffmpeg) / ~200 ms (ImageMagick), on a 1 s budget.
* A genuinely static screen captures **byte-identically** (verified with both
  capturers), which is what makes the byte comparison below a valid change
  detector rather than a heuristic: measured **5 of 6 polls suppressed**.

Be clear-eyed about what that dedup does and does not buy, because the obvious
reading is wrong. A *live* terminal is not static — it ticks, changing 1.26 % of
its pixels every 4 s — and byte comparison is all-or-nothing, so a single changed
pixel ships the whole frame: measured **0 of 6 polls suppressed** on the running
terminal, ~88 KB/frame, ~117 KB/frame once base64-wrapped.

So: dedup is a large win for an idle screen and **no win at all** for an active
one. Closing that gap needs tile diffing (send only the changed tiles), which is
deliberately not attempted here. The 1.26 % figure is an argument for tile
diffing, not for this.

Two things this module is deliberately *not*:

* **Not MT5-specific.** The display belongs to the sandbox, not to the trading
  terminal, so any GUI app on that display shows up here.
* **Not driven by the agent loop.** A refresh that only happens during an agent
  turn freezes exactly when a human wants to look at it. The pump is a plain
  background task owned by the gateway process, which is the same process as the
  agent loop (see ``nanobot/cli/gateway_runtime.py``) and therefore already holds
  the sandbox handles.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import shlex
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from loguru import logger

from nanobot.agent.tools.workspace_bridge import (
    RemoteExecutor,
    fetch_remote_file,
    remote_workspace_root,
    resolve_remote_executor,
    run_remote,
)

#: The X display GUI apps inside the sandbox are started on. ``Xvfb :99`` is what
#: ``scripts/install_mt5_sandbox.sh`` brings up, but nothing here depends on MT5:
#: any process drawing to this display is visible.
DEFAULT_DISPLAY = ":99"
DISPLAY_ENV = "POWERX_SCREEN_DISPLAY"

#: Geometry used only by the ffmpeg fallback path. The primary capturer needs no
#: geometry, so a wrong value here cannot crop a frame.
DEFAULT_SIZE = "1920x1080"
SIZE_ENV = "POWERX_SCREEN_SIZE"

#: One capture per this many seconds. Capture alone costs ~0.2 s, so anything
#: below ~0.25 s spends the whole budget inside ImageMagick/ffmpeg.
DEFAULT_INTERVAL_S = 1.0
MIN_INTERVAL_S = 0.25
MAX_INTERVAL_S = 10.0

#: Re-send an unchanged frame at least this often, so a client that subscribes
#: mid-stream, or reconnects after a blip, is never left showing a stale image.
DEFAULT_KEEPALIVE_S = 10.0

#: Frames are written *inside* the sandbox workspace on purpose: every backend's
#: ``download`` is workspace-confined (Runloop and VPS both run a path guard that
#: refuses anything outside it), so a frame in ``/tmp`` could not be fetched.
REMOTE_DIR = ".powerx-screen"
REMOTE_FRAME = "frame.png"

#: A frame is tens of KB. The bound separates "too big" from "transfer failed".
MAX_FRAME_BYTES = 6 * 1024 * 1024

#: How long to wait before retrying after a failed capture, so a sandbox that is
#: down does not turn into a hot loop hammering it.
ERROR_BACKOFF_S = 3.0

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: What the gateway calls to hand one frame to one subscriber.
FrameSink = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class ScreenFrame:
    """One captured desktop image."""

    seq: int
    data: bytes
    content_type: str
    captured_at: float
    width: int | None
    height: int | None
    changed: bool
    display: str

    def payload(self, *, replay: bool = False) -> dict[str, object]:
        """Serialise for the wire.

        The image is base64 in a JSON envelope rather than a binary WebSocket
        frame. Base64 costs 33 % on an 85 KB frame — about 28 KB — and buys the
        client no new protocol to implement: it already parses JSON envelopes and
        dispatches on ``event``. A duplex binary channel is what tier 3 (real VNC
        input) needs, not this.
        """
        body: dict[str, object] = {
            "seq": self.seq,
            "content_type": self.content_type,
            "width": self.width,
            "height": self.height,
            "changed": self.changed,
            "display": self.display,
            "captured_at": self.captured_at,
            "bytes": len(self.data),
            "image": base64.b64encode(self.data).decode("ascii"),
        }
        if replay:
            body["replay"] = True
        return body


def image_size(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` from a PNG's IHDR chunk, or ``None``.

    Read from the bytes rather than trusting a probed geometry: the header is
    authoritative about what was actually captured, so a mismatch shows up as a
    smaller frame instead of silently cropping.
    """
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return None
    if data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


class ScreenSource(Protocol):
    """Where frames come from. Implemented for the sandbox, and for the host."""

    display: str

    #: ``"sandbox"`` or ``"host"``. Reported to the panel so an operator can
    #: tell which machine they are looking at instead of assuming.
    location: str

    async def resolve(self) -> None:
        """Settle anything that decides :attr:`location`, before it is reported.

        A source that picks its delegate lazily would otherwise have its
        location read before the choice was made, and the panel would be told
        the wrong machine. Implementations that know already do nothing.
        """
        ...

    async def capture(self) -> bytes | None: ...

    async def diagnostic(self) -> str: ...


# ---------------------------------------------------------------------------
# Sandbox source
# ---------------------------------------------------------------------------


def host_display_is_reachable(display: str) -> bool:
    """True when *display* looks like it is served by **this** machine.

    Checked against the X11 unix socket directory rather than by running
    ``xdpyinfo``: it is a filesystem stat instead of a subprocess, and it cannot
    be fooled by a stale ``DISPLAY`` pointing at another host. Only local
    ``:N`` displays are recognised — a ``host:0`` TCP display is somebody else's
    machine and must not be mistaken for the gateway's own desktop.
    """
    spec = display.strip()
    if not spec.startswith(":"):
        return False
    number = spec[1:].split(".", 1)[0]
    if not number.isdigit():
        return False
    return os.path.exists(f"/tmp/.X11-unix/X{number}")


class SandboxScreenSource:
    """Capture the sandbox's X display and pull the frame out as PNG bytes."""

    def __init__(
        self,
        *,
        display: str | None = None,
        size: str | None = None,
        executor: RemoteExecutor | None = None,
        session_key: str | None = None,
    ) -> None:
        self.display = (
            display or os.environ.get(DISPLAY_ENV) or DEFAULT_DISPLAY
        ).strip() or DEFAULT_DISPLAY
        self.size = (size or os.environ.get(SIZE_ENV) or DEFAULT_SIZE).strip() or DEFAULT_SIZE
        self._executor = executor
        # Which sandbox to attach to. The pump has no request context, so it must
        # be told — see :func:`resolve_remote_executor`.
        self.session_key = session_key
        self.location = "sandbox"
        self._frame_path: str | None = None
        self.last_error: str | None = None

    async def _resolve(self) -> RemoteExecutor:
        """Resolve (and cache) the sandbox handle, re-resolving when it drops."""
        if self._executor is None or not self._executor.available:
            self._executor = await resolve_remote_executor(session_key=self.session_key)
        return self._executor

    async def _path(self, executor: RemoteExecutor) -> str | None:
        """Absolute in-sandbox path for the frame, cached per source."""
        if self._frame_path is not None:
            return self._frame_path
        if executor.backend is not None:
            root = (
                await remote_workspace_root(session_key=self.session_key)
            ) or "/workspace"
        else:
            from nanobot.agent.tools.novita_sandbox import _WORKSPACE  # noqa: PLC2701

            root = _WORKSPACE
        self._frame_path = f"{root.rstrip('/')}/{REMOTE_DIR}/{REMOTE_FRAME}"
        return self._frame_path

    def _script(self, out: str) -> str:
        """Shell that captures the display and prints the frame's byte count.

        ImageMagick is tried first even though ffmpeg is marginally faster
        (~180 ms vs ~200 ms, on a 1 s budget). ``import -window root`` needs no
        geometry argument, so it cannot crop or letterbox; ``ffmpeg -f x11grab``
        needs an exact ``-video_size`` and silently produces a wrong frame when
        the display does not match the guess. Robustness is worth 20 ms here.
        """
        display = shlex.quote(self.display)
        quoted_out = shlex.quote(out)
        ffmpeg = (
            f"DISPLAY={display} ffmpeg -hide_banner -loglevel error "
            f"-f x11grab -video_size {shlex.quote(self.size)} -i {display} "
            f"-frames:v 1 -y {quoted_out} >/dev/null 2>&1"
        )
        return (
            "set -u; "
            f'mkdir -p "$(dirname {quoted_out})"; '
            f"rm -f {quoted_out}; "
            f"if command -v import >/dev/null 2>&1; then "
            f"DISPLAY={display} import -window root {quoted_out} >/dev/null 2>&1; "
            f"fi; "
            f"if [ ! -s {quoted_out} ] && command -v ffmpeg >/dev/null 2>&1; then "
            f"{ffmpeg}; "
            f"fi; "
            f"if [ ! -s {quoted_out} ]; then "
            f"echo 'no capturer on DISPLAY={self.display}: need ImageMagick import "
            f"or ffmpeg, and a running X display' >&2; exit 3; "
            f"fi; "
            f"stat -c %s {quoted_out}"
        )

    async def resolve(self) -> None:
        """Nothing to decide: this source always captures in the sandbox."""
        return None

    async def capture(self) -> bytes | None:
        """Capture one frame, or ``None`` when the sandbox cannot produce it."""
        executor = await self._resolve()
        if not executor.available:
            self.last_error = "no execution sandbox is configured"
            return None
        out = await self._path(executor)
        if out is None:
            self.last_error = "could not resolve the sandbox workspace"
            return None
        ok, output = await run_remote(self._script(out), timeout=60, executor=executor)
        if not ok:
            self.last_error = (output or "").strip()[-300:] or "capture command failed"
            return None
        data = await fetch_remote_file(out, max_bytes=MAX_FRAME_BYTES, executor=executor)
        if not data:
            self.last_error = "capture produced no readable frame"
            return None
        self.last_error = None
        return data

    async def diagnostic(self) -> str:
        """Explain why no frames are arriving, for the panel to show."""
        executor = await self._resolve()
        if not executor.available:
            return "No execution sandbox is configured, so there is no desktop to show."
        return self.last_error or "No frames yet."


# ---------------------------------------------------------------------------
# Host source (tests, and a gateway running on the GUI box itself)
# ---------------------------------------------------------------------------


class LocalScreenSource:
    """Capture a display on the **host** by running the same script locally.

    Used by tests, and legitimately useful when the gateway runs on the same
    machine as the GUI (a VPS the operator owns), where the sandbox round trip is
    pure overhead.
    """

    def __init__(self, *, display: str | None = None, size: str | None = None) -> None:
        self.display = (
            display or os.environ.get(DISPLAY_ENV) or DEFAULT_DISPLAY
        ).strip() or DEFAULT_DISPLAY
        self.size = (size or os.environ.get(SIZE_ENV) or DEFAULT_SIZE).strip() or DEFAULT_SIZE
        self.location = "host"
        self.last_error: str | None = None
        self._delegate = SandboxScreenSource(display=self.display, size=self.size)

    async def resolve(self) -> None:
        """Nothing to decide: this source always captures on the host."""
        return None

    async def capture(self) -> bytes | None:
        out = f"{os.path.sep}tmp{os.path.sep}powerx-screen-local.png"
        proc = await asyncio.create_subprocess_shell(
            self._delegate._script(out),  # noqa: SLF001 - same package, same script
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError:
            proc.kill()
            self.last_error = "capture timed out"
            return None
        if proc.returncode != 0:
            self.last_error = (stderr or b"").decode("utf-8", "replace").strip()[-300:]
            return None
        try:
            with open(out, "rb") as handle:
                data = handle.read(MAX_FRAME_BYTES + 1)
        except OSError as exc:
            self.last_error = f"could not read frame: {exc}"
            return None
        if not data or len(data) > MAX_FRAME_BYTES:
            self.last_error = "capture produced no readable frame"
            return None
        self.last_error = None
        return data

    async def diagnostic(self) -> str:
        return self.last_error or "No frames yet."


class AutoScreenSource:
    """Prefer the sandbox; fall back to the host when there is no sandbox to use.

    Two deployments have to work, and they disagree about where the display is:

    * **A hosted sandbox** (Novita, Runloop, Daytona, ...). The gateway runs
      somewhere else entirely, the sandbox holds the desktop, and the frame has
      to be captured remotely and pulled out. The host has no such display, so
      :func:`host_display_is_reachable` is false and this always takes this path.
    * **The GUI box itself** (a VPS the operator owns, or the machine this repo's
      installer targets). No execution backend is provisioned at all, the desktop
      is local, and routing a capture through a sandbox handle that does not
      exist produced nothing but an error.

    The fallback is therefore gated on both halves of the question — *is there a
    usable sandbox* **and** *is this display actually reachable here* — rather
    than on either alone:

    * No sandbox handle and a reachable local display: the desktop is local.
    * No sandbox handle and no local display: nothing to show. Staying on the
      sandbox source makes ``diagnostic()`` say so instead of quietly capturing
      an unrelated machine.
    * A sandbox handle that is merely *unreachable*: no fallback. The frame would
      otherwise show the gateway host's desktop while the operator believed they
      were looking at the sandbox, which is worse than an error.

    Whichever wins is reported as :attr:`location`, so the panel says where the
    pixels came from instead of the operator having to guess.
    """

    def __init__(
        self,
        *,
        display: str | None = None,
        size: str | None = None,
        session_key: str | None = None,
    ) -> None:
        self.display = (
            display or os.environ.get(DISPLAY_ENV) or DEFAULT_DISPLAY
        ).strip() or DEFAULT_DISPLAY
        self.session_key = session_key
        self._size = size
        self._delegate: SandboxScreenSource | LocalScreenSource = SandboxScreenSource(
            display=display, size=size, session_key=session_key
        )
        self._decided = False
        self.last_error: str | None = None

    @property
    def location(self) -> str:
        return self._delegate.location

    async def _pick(self) -> None:
        """Choose a delegate once, on the first capture."""
        executor = await self._delegate._resolve()  # noqa: SLF001 - same package
        if not executor.available and host_display_is_reachable(self.display):
            logger.debug(
                "screen_stream: no {} sandbox handle for {}, capturing {} on the host",
                executor.name,
                self.session_key,
                self.display,
            )
            self._delegate = LocalScreenSource(display=self.display, size=self._size)
        self._decided = True

    async def resolve(self) -> None:
        """Settle the delegate now, so :attr:`location` is truthful when read."""
        if not self._decided:
            await self._pick()

    async def capture(self) -> bytes | None:
        await self.resolve()
        data = await self._delegate.capture()
        self.last_error = self._delegate.last_error
        return data

    async def diagnostic(self) -> str:
        await self.resolve()
        return await self._delegate.diagnostic()


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


class ScreenStream:
    """One session's capture pump and subscriber fan-out.

    The pump runs while at least one subscriber is attached and stops when the
    last one leaves, so closing the panel costs nothing.
    """

    def __init__(
        self,
        key: str,
        source: ScreenSource,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        keepalive_s: float = DEFAULT_KEEPALIVE_S,
    ) -> None:
        self.key = key
        self.source = source
        self.interval_s = min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, float(interval_s)))
        self.keepalive_s = max(self.interval_s, float(keepalive_s))
        self._subscribers: set[FrameSink] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._last_pushed: ScreenFrame | None = None
        self._last_push_at = 0.0
        self._seq = 0
        self.error: str | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def last_frame(self) -> ScreenFrame | None:
        return self._last_pushed

    async def subscribe(self, sink: FrameSink) -> None:
        """Attach *sink*; it immediately receives the newest frame if there is one."""
        async with self._lock:
            self._subscribers.add(sink)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._pump(), name=f"screen-pump:{self.key}")
        if self._last_pushed is not None:
            # A new subscriber must not stare at an empty panel until the screen
            # next changes — an idle terminal can be static for minutes.
            await self._emit(sink, self._last_pushed, replay=True)

    async def unsubscribe(self, sink: FrameSink) -> None:
        async with self._lock:
            self._subscribers.discard(sink)
            if not self._subscribers:
                await self._stop_locked()

    async def stop(self) -> None:
        async with self._lock:
            self._subscribers.clear()
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            # Await the cancelled task so the pump cannot outlive the stream and
            # keep writing frames nobody is listening for.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _emit(self, sink: FrameSink, frame: ScreenFrame, *, replay: bool) -> None:
        try:
            await sink(frame, replay=replay)
        except Exception as exc:  # noqa: BLE001 - a dead subscriber is expected
            logger.debug("screen_stream: dropping subscriber for {}: {}", self.key, exc)
            self._subscribers.discard(sink)

    async def _fanout(self, frame: ScreenFrame) -> None:
        for sink in tuple(self._subscribers):
            await self._emit(sink, frame, replay=False)

    async def _pump(self) -> None:
        """Capture on a timer, and push only when the screen actually moved."""
        try:
            while True:
                started = time.monotonic()
                data = await self.source.capture()
                if data is None:
                    self.error = await self.source.diagnostic()
                    await asyncio.sleep(ERROR_BACKOFF_S)
                    continue

                now = time.monotonic()
                # Byte equality against the last frame we *pushed* (not the last
                # we captured) is the change detector. Verified: an unchanged
                # screen produces identical bytes from both capturers, and 5 of 6
                # polls are suppressed when nothing moves.
                #
                # Note the limit: this is all-or-nothing. A ticking terminal ships
                # every frame (~88 KB each) no matter how few pixels moved — see
                # the module docstring. Tile diffing is the fix, and is not done.
                changed = self._last_pushed is None or data != self._last_pushed.data
                keepalive_due = (now - self._last_push_at) >= self.keepalive_s
                if changed or keepalive_due:
                    self._seq += 1
                    size = image_size(data)
                    frame = ScreenFrame(
                        seq=self._seq,
                        data=data,
                        content_type="image/png",
                        captured_at=now,
                        width=size[0] if size else None,
                        height=size[1] if size else None,
                        changed=changed,
                        display=self.source.display,
                    )
                    self._last_pushed = frame
                    self._last_push_at = now
                    self.error = None
                    await self._fanout(frame)

                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, self.interval_s - elapsed))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the pump must outlive surprises
            logger.warning("screen_stream: pump for {} stopped: {}", self.key, exc)
            self.error = str(exc)[:300]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ScreenStreamManager:
    """Owns the per-session streams for a gateway process."""

    def __init__(
        self,
        source_factory: Callable[[str | None, str | None], ScreenSource] | None = None,
        *,
        default_interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        """Create a manager.

        ``source_factory`` maps ``(display, session_key)`` to the capture source.
        It exists so tests can supply a scripted source, without this class having
        to know how a display is reached.
        """
        self._streams: dict[str, ScreenStream] = {}
        self._lock = asyncio.Lock()
        self._source_factory = source_factory or (
            lambda display, session_key: AutoScreenSource(
                display=display, session_key=session_key
            )
        )
        self._default_interval_s = default_interval_s

    def stream(self, key: str) -> ScreenStream | None:
        return self._streams.get(key)

    async def subscribe(
        self,
        key: str,
        sink: FrameSink,
        *,
        display: str | None = None,
        interval_s: float | None = None,
        session_key: str | None = None,
    ) -> ScreenStream:
        """Attach *sink* to the session's stream, creating the stream if needed.

        *session_key* is the sandbox this stream captures, and is only consulted
        when the stream is created: one stream per key means every later
        subscriber joins the sandbox the first one named.
        """
        async with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                source = self._source_factory(display, session_key)
                stream = ScreenStream(
                    key,
                    source,
                    interval_s=interval_s or self._default_interval_s,
                )
                # Settle the source's choice of machine before anyone can read
                # ``location`` off it, so the subscribe ack cannot name the wrong
                # one. Not under the lock beyond construction: the resolver only
                # builds a client object, it does not wait on the sandbox.
                with contextlib.suppress(Exception):
                    await source.resolve()
                self._streams[key] = stream
        await stream.subscribe(sink)
        return stream

    async def unsubscribe(self, key: str, sink: FrameSink) -> None:
        async with self._lock:
            stream = self._streams.get(key)
        if stream is None:
            return
        await stream.unsubscribe(sink)
        if stream.subscriber_count == 0:
            async with self._lock:
                # Re-check under the lock: a concurrent subscribe may have
                # re-attached between the check and here.
                if stream.subscriber_count == 0 and self._streams.get(key) is stream:
                    self._streams.pop(key, None)
            if stream.subscriber_count == 0:
                await stream.stop()

    async def release_sink(self, sink: FrameSink) -> None:
        """Detach one sink from every stream — used when a client disconnects."""
        for key in list(self._streams):
            await self.unsubscribe(key, sink)

    async def shutdown(self) -> None:
        async with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            await stream.stop()

    @property
    def active_keys(self) -> list[str]:
        return sorted(self._streams)
