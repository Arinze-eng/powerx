"""A rate-limited lane hands the request to the next lane with no backoff sleep.

The base retry layer (rate_limit_aware by default) sleeps between attempts. It
must only sleep when every lane has failed, never when a single lane answered
with a 429 and another lane is still untried.
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
    def __init__(self, name: str, response: LLMResponse) -> None:
        self.name = name
        self.response = response
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return self.response

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return self.response


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
    monkeypatch.delenv("PROVIDER_POOL_FIRST_BYTE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("PROVIDER_POOL_STICKY", "0")
    yield
    reset_lane_affinity()


def test_rate_limited_lane_rotates_to_next_lane_without_waiting(monkeypatch) -> None:
    limited = _Lane("a", _rate_limited())
    backup = _Lane("b", LLMResponse(content="from-b"))
    pool = PoolProvider([({"id": "a", "model": "m"}, limited), ({"id": "b", "model": "m"}, backup)])

    started = time.monotonic()
    response = asyncio.run(pool.chat_stream(_MESSAGES))
    elapsed = time.monotonic() - started

    assert response.content == "from-b"
    assert limited.calls == 1 and backup.calls == 1
    assert elapsed < 0.5, f"rotation took {elapsed:.2f}s"


@pytest.mark.parametrize("retry_mode", ["standard", "rate_limit_aware"])
def test_retry_wrapper_does_not_sleep_when_a_later_lane_answers(monkeypatch, retry_mode) -> None:
    """The retry layer around the pool must not back off when the pool recovered."""

    async def _no_sleep(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("retry layer slept although a lane answered")

    monkeypatch.setattr(PoolProvider, "_sleep_with_heartbeat", _no_sleep)
    limited = _Lane("a", _rate_limited())
    backup = _Lane("b", LLMResponse(content="from-b"))
    pool = PoolProvider([({"id": "a", "model": "m"}, limited), ({"id": "b", "model": "m"}, backup)])

    response = asyncio.run(pool.chat_stream_with_retry(_MESSAGES, retry_mode=retry_mode))

    assert response.content == "from-b"
    assert backup.calls == 1


def test_retry_wrapper_still_backs_off_when_every_lane_is_rate_limited(monkeypatch) -> None:
    """Sanity check on the other side: a fully limited pool does wait before retrying."""
    waits: list[float] = []

    async def _record(self: Any, delay: float, **kwargs: Any) -> None:
        waits.append(delay)

    monkeypatch.setattr(PoolProvider, "_sleep_with_heartbeat", _record)
    pool = PoolProvider([
        ({"id": "a", "model": "m"}, _Lane("a", _rate_limited())),
        ({"id": "b", "model": "m"}, _Lane("b", _rate_limited())),
    ])

    asyncio.run(pool.chat_stream_with_retry(_MESSAGES, retry_mode="standard"))

    assert waits, "expected a backoff once every lane was rate limited"
