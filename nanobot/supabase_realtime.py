"""Supabase Realtime publisher for agent feedback delivery.

Replaces the Render reverse-proxy WebSocket path for push notifications.
When the agent finishes a turn, a row is INSERTed into ``agent_feedback``.
Clients subscribed to the Supabase Realtime channel receive the payload
directly from Supabase, so Render bandwidth is untouched.

The publisher reuses the same Supabase credentials already wired in
``nanobot.supabase_auth`` (``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``)
and the same ``httpx`` async client pattern, so no new dependencies are needed.

"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

if TYPE_CHECKING:
    from nanobot.bus.events import OutboundMessage
    from nanobot.bus.outbound_events import (
        ProgressEvent,
        RetryWaitEvent,
        RuntimeModelUpdatedEvent,
        StreamDeltaEvent,
        StreamEndEvent,
        StreamedResponseEvent,
        TurnEndEvent,
    )

_MAX_CONTENT_CHARS = 12_000
_MAX_METADATA_CHARS = 8_000


class SupabaseRealtimeError(RuntimeError):
    """Raised when publishing agent feedback to Supabase Realtime fails."""


def _event_type(msg: OutboundMessage) -> str:
    """Map an OutboundMessage's event to a realtime event_type string."""
    event = msg.event
    if event is None:
        return "message"
    # Local imports to avoid a circular import at module load time.
    from nanobot.bus.outbound_events import (
        ProgressEvent,
        RetryWaitEvent,
        RuntimeModelUpdatedEvent,
        StreamDeltaEvent,
        StreamEndEvent,
        StreamedResponseEvent,
        TurnEndEvent,
    )

    if isinstance(event, StreamDeltaEvent):
        return "delta"
    if isinstance(event, StreamEndEvent):
        return "stream_end"
    if isinstance(event, StreamedResponseEvent):
        return "streamed_response"
    if isinstance(event, ProgressEvent):
        if event.reasoning_delta:
            return "reasoning_delta"
        if event.reasoning_end:
            return "reasoning_end"
        if event.reasoning:
            return "reasoning"
        return "progress"
    if isinstance(event, RetryWaitEvent):
        return "retry_wait"
    if isinstance(event, TurnEndEvent):
        return "turn_end"
    if isinstance(event, RuntimeModelUpdatedEvent):
        return "runtime_model_updated"
    return "message"


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit]


class SupabaseRealtimePublisher:
    """INSERT agent feedback rows that Supabase Realtime broadcasts to clients.

    The publisher is intentionally fire-and-forget: a failed INSERT is logged
    as a warning but never raises into the message-bus dispatcher, so the agent
    keeps working even if Supabase is temporarily unreachable.
    """

    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        self.service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        self._enabled: bool | None = None
        # Persistent httpx client for TCP/TLS connection pooling.
        # Reusing one client across publish calls avoids a full TLS
        # handshake on every INSERT, which is the dominant bandwidth
        # cost for high-frequency stream-delta publishing.
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.service_key)

    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        env_flag = os.getenv("SUPABASE_REALTIME_ENABLED", "true").strip().lower()
        self._enabled = self.configured and env_flag not in {"0", "false", "no"}
        return self._enabled

    def _get_client(self) -> httpx.AsyncClient:
        """Return a cached persistent httpx.AsyncClient for connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish one outbound message to the agent_feedback table.

        Only "terminal" feedback events are published — the final response,
        turn-end marker, progress, stream deltas/ends, and runtime model
        updates. Pure lifecycle events (RetryWait, etc.) are skipped to avoid
        noise, matching the Render WebSocket flow the user wants to replace.
        """
        if not self.enabled:
            return

        event_type = _event_type(msg)
        # Skip events that the Render WebSocket path would also skip
        # (the Render proxy relays everything, but the WebUI only acts on
        # these types — sending the rest is pure bandwidth waste).
        if event_type in {"retry_wait"}:
            return

        chat_id = str(msg.chat_id or "").strip()
        if not chat_id:
            return

        content = _bounded(str(msg.content or ""), _MAX_CONTENT_CHARS)

        # Build metadata payload — serialize safely to JSON, then truncate.
        raw_metadata: dict[str, Any] = {}
        if msg.metadata:
            for key, value in msg.metadata.items():
                # Skip internal underscore-prefixed keys — they are
                # nanobot-internal flags, not client-facing data.
                if isinstance(key, str) and key.startswith("_"):
                    continue
                raw_metadata[key] = value
        metadata_json = _bounded(
            json.dumps(raw_metadata, default=str, ensure_ascii=False),
            _MAX_METADATA_CHARS,
        )

        stream_id = ""
        event = msg.event
        if event is not None:
            stream_id = str(getattr(event, "stream_id", "") or "")

        body: dict[str, Any] = {
            "chat_id": chat_id,
            "channel": str(msg.channel or ""),
            "content": content,
            "event_type": event_type,
            "metadata": metadata_json,
        }
        if stream_id:
            body["stream_id"] = stream_id

        try:
            client = self._get_client()
            response = await client.post(
                f"{self.url}/rest/v1/agent_feedback",
                headers=self._headers(),
                json=body,
            )
            if not response.is_success:
                logger.warning(
                    "Supabase Realtime publish failed (HTTP {}): {}",
                    response.status_code,
                    response.text[:300],
                )
        except httpx.HTTPError as exc:
            logger.warning("Supabase Realtime publish error: {}", type(exc).__name__)
        except Exception as exc:
            logger.warning("Supabase Realtime unexpected error: {}", type(exc).__name__)