"""Bridge the Telegram chat Mini App onto the live gateway agent.

The chat page served at /app runs inside a user's Telegram client, but Render
hosts only the WebUI gateway — the standalone OpenAI-compatible API process is
not running there. This module routes mini-app turns through the same message
bus and websocket channel that the WebUI itself uses, so users get the real
agent (tools, sandbox tasks, memory) with zero extra processes or bandwidth.

Flow per turn:
1. ``POST /app/token`` validated the Telegram initData; the bearer token maps
   to the user's Telegram id (their private-chat id == user id).
2. We publish an InboundMessage on the *telegram* channel into the user's own
   private-chat session (chat id == user id), so conversation memory and
   /upload file handoffs are shared with their normal bot chat. The manager
   suppresses the telegram channel's streaming copies for this turn (our
   metadata carries no stream_id, which telegram's send_delta requires), so
   only the final answer reaches Telegram.
3. The gateway's websocket channel is temporarily subscribed with a sink
   connection: "message" and "turn_end" frames arriving for the user's chat id
   are re-emitted to the browser as SSE.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from loguru import logger

from nanobot.api.telegram_auth import miniapp_tokens
from nanobot.bus.events import InboundMessage

# Per-turn wall clock. Long enough for tool-heavy sandbox tasks, bounded so a
# wedged agent turn cannot pin a connection forever.
TURN_TIMEOUT_SECONDS = 600.0


class MiniAppBridge:
    """Route Mini App chat turns through the gateway's websocket channel."""

    def __init__(self) -> None:
        self._channel: Any = None

    # -- wiring -------------------------------------------------------------

    def wire(self, channel: Any) -> None:
        """Register the live websocket channel (called from WebSocketChannel.start)."""
        if channel is not self._channel:
            logger.info("Mini App bridge attached to websocket channel")
        self._channel = channel

    @property
    def ready(self) -> bool:
        return self._channel is not None

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _session_key(user_id: str) -> str:
        """Telegram private-chat session key for this user.

        The Mini App shares the agent's memory/context with the user's normal
        Telegram chat (chat id == user id), so files uploaded via /upload and
        earlier conversation carry over into the app and back.
        """
        return f"telegram:{user_id}"

    @staticmethod
    def _sse(obj: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    class _Sink:
        """Stand-in websocket connection that funnels frames into a queue."""

        def __init__(self, queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
            self._queue = queue
            self._loop = loop
            self.id = f"mapp-{uuid.uuid4().hex[:8]}"

        async def send(self, raw: str) -> None:
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, raw)
            except RuntimeError:  # consumer loop gone; drop silently
                pass

    # -- main entry point -----------------------------------------------------

    async def run_turn(
        self,
        *,
        token: str,
        content: str,
        media_paths: list[str],
    ):
        """Yield SSE-formatted bytes for one agent turn.

        Raises LookupError when the token is unknown/expired and RuntimeError
        when the gateway agent is unavailable.
        """
        user_id = miniapp_tokens.verify(token)
        if not user_id:
            raise LookupError("invalid or expired mini-app token")
        channel = self._channel
        if channel is None:
            raise RuntimeError("agent is starting up, try again in a moment")

        cid = self._session_key(user_id)
        turn_started = time.monotonic()
        queue: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        sink = self._Sink(queue, loop)

        subs_added = False
        got_message = False
        done_sent = False
        try:
            # Subscribe the sink BEFORE publishing so no early reply is missed.
            channel._attach(sink, cid)  # noqa: SLF001 - intentional integration seam
            subs_added = True
            await channel.bus.publish_inbound(
                InboundMessage(
                    channel="telegram",
                    sender_id=user_id,
                    chat_id=user_id,  # private chat id == telegram user id
                    content=content,
                    media=list(media_paths),
                    # No stream_id: telegram's send_delta drops frames without
                    # one, so the browser sink receives exactly one final copy
                    # while Telegram stays quiet during the turn.
                    metadata={"miniapp": True},
                    session_key_override=cid,
                )
            )

            deadline = turn_started + TURN_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield self._sse({"error": "turn timed out"})
                    break
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=min(remaining, 30.0))
                except asyncio.TimeoutError:
                    continue
                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(frame, dict):
                    continue
                kind = frame.get("event")
                if kind == "message":
                    text = frame.get("text") or ""
                    if text:
                        yield self._sse({"delta": text})
                    got_message = True
                elif kind == "turn_end":
                    if not got_message:
                        # Terminal marker without a visible message (e.g. an
                        # empty/suppressed reply); surface nothing further.
                        logger.debug("mini-app turn for {} ended with no message frame", user_id)
                    yield b"data: [DONE]\n\n"
                    done_sent = True
                    break
                elif kind == "error":
                    yield self._sse({"error": frame.get("detail") or "agent error"})
                    done_sent = True
                    break
        finally:
            if subs_added:
                conns = channel._subs.get(cid)  # noqa: SLF001
                if conns is not None:
                    conns.discard(sink)
                    if not conns:
                        channel._subs.pop(cid, None)  # noqa: SLF001
            tracked = getattr(channel, "_conn_chats", None)
            if tracked is not None:
                tracked.pop(sink, None)
            if not done_sent:
                logger.debug("mini-app turn for {} ended without terminal frame", user_id)


# Process-wide singleton wired at gateway startup.
miniapp_bridge = MiniAppBridge()
