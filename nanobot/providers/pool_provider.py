"""Provider wrapper that rotates across a pool of OpenAI-compatible endpoints.

The admin-managed provider pool (:mod:`nanobot.provider_pool`) holds up to 40
lanes, each with its own base URL, API key and model. This wrapper spreads
requests across the enabled lanes round-robin and, when a lane answers with a
rate-limit / overload / server / connection / timeout error, retries the next
lane before surfacing a failure. That is what stops a single key from
rate-limiting the whole agent.
"""

# pyright: reportIncompatibleMethodOverride=false, reportIncompatibleVariableOverride=false

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse

# Error kinds that mean "try another lane" rather than "fail the request".
#
# `auth`/`permission`/`billing` belong here just as much as the transient
# network kinds do: a lane whose key expired, was revoked, or whose account ran
# out of credit is permanently unusable for this request, and the whole point of
# the pool is that the backup lane still answers. Without these the wrapper
# returned the primary lane's 401/402 immediately and rotation never happened.
_FAILOVER_KINDS = frozenset(
    {
        "rate_limit",
        "overloaded",
        "timeout",
        "connection",
        "server_error",
        "auth",
        "authentication",
        "permission",
        "unauthorized",
        "forbidden",
        "billing",
        "quota",
    }
)
# HTTP statuses worth rotating away from.
# 401/403 = bad or revoked key; 402 = out of credit; 404 = model decommissioned
# on this lane. All are lane-specific, so the next lane is worth trying.
_FAILOVER_STATUS = frozenset(
    {401, 402, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504, 522, 524}
)
# Tokens matched against provider error type/code (never against model output).
_FAILOVER_TOKENS = (
    "rate_limit",
    "rate limit",
    "too_many_requests",
    "too many requests",
    "overloaded",
    "server_error",
    "server error",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "connection",
    "insufficient_quota",
    "insufficient quota",
    "quota_exceeded",
    "quota exceeded",
    "insufficient_balance",
    "balance",
    "out of credits",
    # Auth/permission failures that are lane-specific. Matched against the
    # provider's error type/code only, never against model output.
    "invalid_api_key",
    "invalid api key",
    "unauthorized",
    "authentication",
    "authenticationerror",
    "forbidden",
    "permission",
    "no auth credentials",
    "credit_balance",
    "billing",
)

# Hard ceiling on how long a single lane may hold the whole request.
#
# This is the fix for "the backup lane is never used, it just hangs". A lane
# whose host does not resolve, black-holes the connection, or accepts the socket
# and then never replies produced NO error at all, so `_should_failover` had
# nothing to act on: the pool awaited that first lane forever and never reached
# the backup. The per-provider timeout cannot cover this, because the stream
# idle timeout is 1800s by default, the request timeout is 120s, and the layer
# above retries the *same* lane several times before giving up.
#
# Raising this guards the "everything is genuinely slow" case; the default is
# deliberately well inside a user's patience so a dead primary fails over fast.
DEFAULT_LANE_TIMEOUT_S = 45.0
LANE_TIMEOUT_ENV = "PROVIDER_POOL_LANE_TIMEOUT_S"


def _lane_timeout_s() -> float | None:
    """Per-lane wall-clock budget, or None to disable the guard entirely."""
    raw = os.environ.get(LANE_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_LANE_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_LANE_TIMEOUT_S
    if value <= 0:
        # Explicit opt-out for anyone who wants the old unbounded behaviour.
        return None
    return value


class PoolProvider(LLMProvider):
    """Rotate chat requests across pool lanes, failing over on transient errors."""

    supports_progress_deltas = True

    def __init__(
        self,
        lanes: list[tuple[dict[str, Any], LLMProvider]],
        *,
        generation: GenerationSettings | None = None,
    ) -> None:
        super().__init__(api_key=None, api_base=None)
        self._lanes: list[tuple[dict[str, Any], LLMProvider]] = list(lanes)
        self._lock = threading.Lock()
        self._cursor = 0
        self.generation = generation or GenerationSettings()

    @property
    def lanes(self) -> list[tuple[dict[str, Any], LLMProvider]]:
        return self._lanes

    def _order(self) -> list[tuple[dict[str, Any], LLMProvider]]:
        """Return the lanes starting at the rotating cursor (round-robin)."""
        if not self._lanes:
            return []
        with self._lock:
            start = self._cursor % len(self._lanes)
            self._cursor = (self._cursor + 1) % len(self._lanes)
        return self._lanes[start:] + self._lanes[:start]

    @staticmethod
    def _should_failover(response: LLMResponse | None) -> bool:
        if response is None:
            return True
        kind = str(response.error_kind or "").lower()
        if kind in _FAILOVER_KINDS:
            return True
        status = response.error_status_code
        if isinstance(status, int) and status in _FAILOVER_STATUS:
            return True
        if response.error_type or response.error_code:
            text = f"{response.error_type or ''} {response.error_code or ''}".lower()
            if any(token in text for token in _FAILOVER_TOKENS):
                return True
        return False

    async def _call_lane(
        self,
        call: Callable[[], Awaitable[LLMResponse]],
        entry: dict[str, Any],
    ) -> LLMResponse:
        """Await one lane's call under a wall-clock budget.

        A lane that never answers is treated as a lane failure so the loop can
        move on to the next one. Without this the pool awaited a black-holed
        endpoint indefinitely and the caller saw no response at all rather than
        a failover.
        """
        timeout = _lane_timeout_s()
        if timeout is None:
            return await call()
        try:
            return await asyncio.wait_for(call(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            label = entry.get("label") or entry.get("id") or entry.get("baseUrl") or "lane"
            return LLMResponse(
                content=None,
                finish_reason="error",
                error_kind="timeout",
                error_type="lane_timeout",
                error_code=f"lane_timeout:{label}:{timeout:g}s",
                error_should_retry=True,
            )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        last: LLMResponse | None = None
        for entry, provider in self._order():
            response = await self._call_lane(
                lambda p=provider, e=entry: p.chat(
                    messages=messages,
                    tools=tools,
                    model=e.get("model") or model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                ),
                entry,
            )
            if not self._should_failover(response):
                return response
            last = response
        return last if last is not None else LLMResponse(content=None, error_kind="connection")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        last: LLMResponse | None = None
        streamed = False

        async def _track(delta: str) -> None:
            nonlocal streamed
            streamed = True
            if on_content_delta is not None:
                await on_content_delta(delta)

        for entry, provider in self._order():
            response = await self._call_lane(
                lambda p=provider, e=entry: p.chat_stream(
                    messages=messages,
                    tools=tools,
                    model=e.get("model") or model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                    on_content_delta=_track,
                    on_thinking_delta=on_thinking_delta,
                    on_tool_call_delta=on_tool_call_delta,
                ),
                entry,
            )
            # Never rotate after output has reached the user: a retry would duplicate it.
            if streamed or not self._should_failover(response):
                return response
            last = response
        return last if last is not None else LLMResponse(content=None, error_kind="connection")

    def get_default_model(self) -> str:
        if self._lanes:
            model = self._lanes[0][0].get("model")
            if model:
                return str(model)
        return super().get_default_model()
