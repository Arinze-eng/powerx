"""Bridge the public OpenAI-compatible API onto the live gateway agent.

Render (and any other single-process deployment) runs ``nanobot gateway``, not
``nanobot serve`` — so without this bridge the ``/v1/*`` routes 404. This module
mirrors :mod:`nanobot.api.miniapp_bridge`: it publishes an InboundMessage on the
*telegram* channel into the key owner's own session and collects the agent's
replies through a temporary websocket sink, then wraps them in OpenAI-shaped
responses.

Billing: unlike the Mini App bridge, API turns must be charged per iteration.
The telegram credit hook keys off ``metadata["supabase_user_id"]``; we mint a
one-shot auth token for the key owner and pass it in metadata so the *existing*
SupabaseCreditHook bills their credits — no second billing path.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage

# Per-request wall clock. Long enough for tool-heavy sandbox tasks, bounded so
# a wedged agent turn cannot pin a connection forever.
TURN_TIMEOUT_SECONDS = 600.0


class ApiBridge:
    """Singleton bridging /v1 requests to the gateway websocket channel."""

    def __init__(self) -> None:
        self._channel: Any = None

    def wire(self, channel: Any) -> None:
        """Attach the live WebSocketChannel (called from its start())."""
        if channel is not self._channel:
            logger.info("public API bridge wired to gateway agent")
        self._channel = channel

    @property
    def ready(self) -> bool:
        return self._channel is not None

    @staticmethod
    def _session_key(user_id: str) -> str:
        # Same session as the user's Telegram private chat, so context carries
        # over between bot chats and API calls (identical to the Mini App).
        return f"telegram:{user_id}"

    class _Sink:
        """Stand-in websocket connection that funnels frames into a queue."""

        def __init__(self, queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
            self._queue = queue
            self._loop = loop
            self.id = f"api-{uuid.uuid4().hex[:8]}"

        async def send(self, raw: str) -> None:
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, raw)
            except RuntimeError:  # loop closed during shutdown
                pass

    async def run_turn(
        self,
        *,
        user_id: str,
        content: str,
        media_paths: list[str] | None = None,
        auth_token: str | None = None,
        api_key_id: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Run one agent turn; returns (final_text, meta).

        ``meta`` may carry ``{"error": ...}`` when the turn failed or timed out.
        """
        channel = self._channel
        if channel is None:
            raise RuntimeError("agent is starting up, try again in a moment")

        cid = self._session_key(user_id)
        turn_started = time.monotonic()
        queue: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        sink = self._Sink(queue, loop)

        metadata: dict[str, Any] = {"api_request": True}
        if auth_token:
            metadata["supabase_auth_token"] = auth_token
        if api_key_id is not None:
            metadata["api_key_id"] = api_key_id

        subs_added = False
        final_parts: list[str] = []
        got_message = False
        error: str | None = None
        try:
            channel._attach(sink, cid)  # noqa: SLF001 - intentional integration seam
            subs_added = True
            await channel.bus.publish_inbound(
                InboundMessage(
                    channel="telegram",
                    sender_id=user_id,
                    chat_id=user_id,
                    content=content,
                    media=list(media_paths or []),
                    metadata=metadata,
                    session_key_override=cid,
                )
            )

            deadline = turn_started + TURN_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = "turn timed out"
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
                    text = str(frame.get("text") or "")
                    if text:
                        got_message = True
                        final_parts.append(text)
                elif kind == "turn_end":
                    break
                elif kind == "error":
                    error = str(frame.get("detail") or "agent error")
                    break
        finally:
            if subs_added:
                conns = getattr(channel, "_subs", {}).get(cid)  # noqa: SLF001
                if conns is not None:
                    conns.discard(sink)
                    if not conns:
                        channel._subs.pop(cid, None)  # noqa: SLF001
            tracked = getattr(channel, "_conn_chats", None)
            if tracked is not None:
                tracked.pop(sink, None)

        text = "\n\n".join(p for p in final_parts if p.strip()).strip()
        if error and not text:
            return "", {"error": error}
        if not got_message and not text:
            return "", {"error": "no response from agent"}
        return text, {}


api_bridge = ApiBridge()
