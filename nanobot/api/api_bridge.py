"""Bridge the public OpenAI-compatible API onto the live gateway agent.

Render (and any other single-process deployment) runs ``nanobot gateway``, not
``nanobot serve`` — so without this bridge the ``/v1/*`` routes 404.

Unlike the Mini App bridge (which funnels bus traffic through a websocket
sink), this module calls ``AgentLoop.process_direct`` *directly*, mirroring the
proven ``nanobot serve`` path. Publishing through the bus with
``channel="telegram"`` does not work here: the ChannelManager routes the agent's
reply back out via the real Telegram channel, so the sink never sees it and the
request hangs until timeout.

Billing: the turn runs with ``channel="api"`` and metadata carrying
``api_key_id`` + ``supabase_user_id``, which activates
:class:`nanobot.agent.hooks.supabase_credit.ApiCreditHook` (registered by the
gateway runtime) for per-step charging and request logging.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

# Per-request wall clock. Long enough for tool-heavy sandbox tasks, bounded so
# a wedged agent turn cannot pin a connection forever.
TURN_TIMEOUT_SECONDS = 600.0


class ApiBridge:
    """Singleton bridging /v1 requests to the gateway agent loop."""

    def __init__(self) -> None:
        self._agent: Any = None

    def wire(self, agent: Any) -> None:
        """Attach the live AgentLoop (called from the gateway runtime)."""
        if agent is not self._agent:
            logger.info("public API bridge wired to gateway agent")
        self._agent = agent

    @property
    def ready(self) -> bool:
        return self._agent is not None

    # -- main entry point -----------------------------------------------------

    async def run_turn(
        self,
        *,
        user_id: str,
        content: str,
        media_paths: list[str] | None = None,
        api_key_id: int | None = None,
        supabase_user_id: str | None = None,
        model: str = "",
        stream: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Run one agent turn; returns (final_text, meta).

        ``meta`` may carry ``{"error": ...}`` when the turn failed or timed out,
        otherwise it carries ``{"usage": {...}}`` best-effort token counts.
        ``user_id`` is the key owner's agentx user id (billing identity); the
        session is shared with their Telegram chat so context carries over.
        """
        agent = self._agent
        if agent is None:
            raise RuntimeError("agent is starting up, try again in a moment")

        # Share the user's Telegram private-chat session (chat id == telegram id
        # for agentx users is unknown here, so keep an api-namespaced key that
        # still persists memory per user).
        session_key = f"api:{user_id}"
        metadata: dict[str, Any] = {}
        if api_key_id is not None:
            metadata["api_key_id"] = api_key_id
        if supabase_user_id:
            metadata["supabase_user_id"] = supabase_user_id
        if model:
            metadata["api_model"] = model
        metadata["api_stream"] = bool(stream)

        try:
            async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                response = await agent.process_direct(
                    content=content,
                    media=list(media_paths or []) or None,
                    session_key=session_key,
                    channel="api",
                    chat_id=user_id or "default",
                    sender_id=str(supabase_user_id or user_id or "api"),
                    extra_metadata=metadata,
                )
        except (TimeoutError, asyncio.TimeoutError):
            return "", {"error": "turn timed out"}
        except Exception as exc:  # noqa: BLE001 - surface as API error
            logger.warning("public API turn failed: {}", str(exc)[:300])
            message = str(exc)
            lowered = message.lower()
            if "credit" in lowered or "exhausted" in lowered:
                return "", {"error": f"Insufficient credits: {message[:300]}"}
            return "", {"error": message[:300] or "agent error"}

        text = ""
        if response is not None:
            text = str(getattr(response, "content", "") or "").strip()
        usage = getattr(agent, "_last_usage", None) or {}
        if not text:
            return "", {"error": "no response from agent"}
        return text, {"usage": dict(usage) if isinstance(usage, dict) else {}}


api_bridge = ApiBridge()
