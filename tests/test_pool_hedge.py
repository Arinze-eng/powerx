"""Hedged streaming: a silent lane hands over to the next lane early.

After the hedge window with no reply, the next lane starts alongside the silent
one. The first lane to produce a delta owns the reply; the other is cancelled,
and nothing it produced reaches the user.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from nanobot.providers.base import LLMResponse
from nanobot.providers.pool_provider import PoolProvider, reset_lane_affinity

_MESSAGES = [{"role": "user", "content": "hi"}]


class _Lane:
    """Streams scripted (delay_before_s, delta_or_None) steps, or fails at once."""

    def __init__(
        self,
        name: str,
        steps: list[tuple[float, str | None]] | None = None,
        *,
        fail: LLMResponse | None = None,
    ) -> None:
        self.name = name
        self.steps = steps or []
        self.fail = fail
        self.calls = 0
        self.cancelled = False

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return LLMResponse(content=f"{self.name}-ok")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Any = None,
        on_thinking_delta: Any = None,
        on_tool_call_delta: Any = None,
    ) -> LLMResponse:
        self.calls += 1
        if self.fail is not None:
            return self.fail
        text = ""
        try:
            for delay, delta in self.steps:
                await asyncio.sleep(delay)
                if delta is not None:
                    text += delta
                    if on_content_delta is not None:
                        await on_content_delta(delta)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return LLMResponse(content=text or f"{self.name}-ok")


def _rate_limited() -> LLMResponse:
    return LLMResponse(
        content="rate limit exceeded",
        finish_reason="error",
        error_kind="rate_limit",
        error_status_code=429,
        error_should_retry=True,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_lane_affinity()
    monkeypatch.delenv("PROVIDER_POOL_LANE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("PROVIDER_POOL_STICKY", "0")
    monkeypatch.setenv("PROVIDER_POOL_FIRST_BYTE_TIMEOUT_S", "5")
    monkeypatch.setenv("PROVIDER_POOL_HEDGE_AFTER_S", "0.2")
    yield
    reset_lane_affinity()


def _entry(name: str) -> dict[str, Any]:
    return {"id": name, "model": "m"}


def _run(pool: PoolProvider, deltas: list[str]) -> tuple[LLMResponse, float]:
    async def _capture(delta: str) -> None:
        deltas.append(delta)

    started = time.monotonic()
    response = asyncio.run(pool.chat_stream(_MESSAGES, on_content_delta=_capture))
    return response, time.monotonic() - started


def test_silent_lane_is_hedged_by_the_next_lane_after_the_hedge_window() -> None:
    silent = _Lane("a", [(10.0, "too-late")])
    backup = _Lane("b", [(0.0, "from-b")])
    pool = PoolProvider([(_entry("a"), silent), (_entry("b"), backup)])
    deltas: list[str] = []

    response, elapsed = _run(pool, deltas)

    assert response.content == "from-b"
    assert deltas == ["from-b"]
    assert elapsed < 1.5, f"hedge took {elapsed:.2f}s; expected about the 0.2s window"
    assert silent.cancelled, "the silent lane should be cancelled once another owns the reply"
    assert not pool._is_parked(_entry("a")), "a hedged-away lane is not a failure"


def test_output_from_a_lane_that_does_not_own_the_reply_never_reaches_the_user() -> None:
    late = _Lane("a", [(0.5, "a-late")])
    first = _Lane("b", [(0.0, "b1"), (0.05, "b2")])
    pool = PoolProvider([(_entry("a"), late), (_entry("b"), first)])
    deltas: list[str] = []

    response, _ = _run(pool, deltas)

    assert response.content == "b1b2"
    assert deltas == ["b1", "b2"]
    assert "a-late" not in deltas


def test_lane_that_speaks_before_the_hedge_window_keeps_the_reply() -> None:
    owner = _Lane("a", [(0.05, "a1"), (0.1, "a2")])
    backup = _Lane("b", [(0.0, "b")])
    pool = PoolProvider([(_entry("a"), owner), (_entry("b"), backup)])
    deltas: list[str] = []

    response, _ = _run(pool, deltas)

    assert response.content == "a1a2"
    assert deltas == ["a1", "a2"]
    assert backup.calls == 0, "no hedge should start once a lane is already streaming"


def test_rate_limited_lane_rotates_at_once_even_when_the_next_lane_is_silent() -> None:
    limited = _Lane("a", fail=_rate_limited())
    silent = _Lane("b", [(10.0, "late")])
    answer = _Lane("c", [(0.0, "from-c")])
    pool = PoolProvider([
        (_entry("a"), limited),
        (_entry("b"), silent),
        (_entry("c"), answer),
    ])
    deltas: list[str] = []

    response, elapsed = _run(pool, deltas)

    assert response.content == "from-c"
    assert deltas == ["from-c"]
    assert elapsed < 1.5, f"rotation took {elapsed:.2f}s"


def test_hedging_disabled_tries_lanes_in_turn(monkeypatch) -> None:
    monkeypatch.setenv("PROVIDER_POOL_HEDGE_AFTER_S", "0")
    monkeypatch.setenv("PROVIDER_POOL_FIRST_BYTE_TIMEOUT_S", "2")
    slow = _Lane("a", [(0.6, "a-slow")])
    backup = _Lane("b", [(0.0, "b")])
    pool = PoolProvider([(_entry("a"), slow), (_entry("b"), backup)])

    response, _ = _run(pool, [])

    assert response.content == "a-slow"
    assert backup.calls == 0
