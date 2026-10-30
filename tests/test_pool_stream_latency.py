"""Latency contract for streamed pool requests.

The pool used to wrap a whole streamed reply in one wall-clock cap. Two bad
results followed: a healthy lane still generating past the cap was cancelled
and re-run on another lane (the work was wasted and the healthy lane got
parked), and a dead lane could hold the reply for the full cap before the
backup was tried.

The contract now: a lane must show *some* activity (content, thinking or a
tool-call delta) within the first-byte budget, or it is failed over at once.
Once it is streaming, it is not cut short by the pool.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from nanobot.providers.base import LLMResponse
from nanobot.providers.pool_provider import PoolProvider, reset_lane_affinity

_MESSAGES = [{"role": "user", "content": "hi"}]


class _StreamLane:
    """Streams a scripted sequence: (delay_before_s, delta_or_None) steps."""

    def __init__(self, name: str, steps: list[tuple[float, str | None]]) -> None:
        self.name = name
        self.steps = steps
        self.calls = 0

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
        text = ""
        for delay, delta in self.steps:
            await asyncio.sleep(delay)
            if delta is not None:
                text += delta
                if on_content_delta is not None:
                    await on_content_delta(delta)
        return LLMResponse(content=text or f"{self.name}-ok")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_lane_affinity()
    monkeypatch.delenv("PROVIDER_POOL_LANE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("PROVIDER_POOL_FIRST_BYTE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("PROVIDER_POOL_STICKY", "0")
    yield
    reset_lane_affinity()


def test_healthy_stream_running_past_first_byte_budget_is_not_cut(monkeypatch) -> None:
    """Streaming keeps going after the first byte; the pool must not cancel it."""
    monkeypatch.setenv("PROVIDER_POOL_FIRST_BYTE_TIMEOUT_S", "0.3")
    slow = _StreamLane("a", [(0.05, "hello "), (1.0, "world")])
    backup = _StreamLane("b", [(0.0, "from-b")])
    pool = PoolProvider([({"id": "a", "model": "m"}, slow), ({"id": "b", "model": "m"}, backup)])

    response = asyncio.run(pool.chat_stream(_MESSAGES))

    assert response.content == "hello world"
    assert backup.calls == 0


def test_silent_lane_fails_over_within_first_byte_budget(monkeypatch) -> None:
    """A lane that never starts streaming hands the request to the backup fast."""
    monkeypatch.setenv("PROVIDER_POOL_FIRST_BYTE_TIMEOUT_S", "0.3")
    dead = _StreamLane("a", [(5.0, "too-late")])
    backup = _StreamLane("b", [(0.0, "from-b")])
    pool = PoolProvider([({"id": "a", "model": "m"}, dead), ({"id": "b", "model": "m"}, backup)])

    started = time.monotonic()
    response = asyncio.run(pool.chat_stream(_MESSAGES))
    elapsed = time.monotonic() - started

    assert response.content == "from-b"
    assert elapsed < 2.0, f"failover took {elapsed:.2f}s; the first-byte budget was not applied"
