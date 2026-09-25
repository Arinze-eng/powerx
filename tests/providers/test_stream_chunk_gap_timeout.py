"""A stream that goes quiet mid-flight must not wait out the prefill budget.

DEFAULT_STREAM_IDLE_TIMEOUT_S is 1800s and used to bound every wait for the next
chunk -- time to the first token and every gap after it. The stall-recovery path
(on_stream_recover / re-stream) is driven by that timeout, so one value for both
meant recovery could not fire for half an hour. These tests pin the split: the
first chunk keeps the generous budget, the gaps after it get a tight ceiling.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nanobot.providers import base as base_module
from nanobot.providers.base import (
    DEFAULT_STREAM_CHUNK_GAP_TIMEOUT_S,
    MIN_STREAM_CHUNK_GAP_TIMEOUT_S,
    resolve_stream_chunk_gap_timeout_s,
    resolve_stream_idle_timeout_s,
)
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

_MESSAGES = [{"role": "user", "content": "hi"}]


# --- the resolver ----------------------------------------------------------


def test_default_is_a_mid_stream_ceiling_not_the_prefill_budget() -> None:
    assert resolve_stream_chunk_gap_timeout_s(env_value=None) == (
        DEFAULT_STREAM_CHUNK_GAP_TIMEOUT_S
    )
    assert DEFAULT_STREAM_CHUNK_GAP_TIMEOUT_S < resolve_stream_idle_timeout_s(
        env_value=""
    )


def test_an_env_value_is_honoured() -> None:
    assert resolve_stream_chunk_gap_timeout_s(env_value="12.5") == 12.5


def test_it_can_never_be_configured_higher_than_the_prefill_budget() -> None:
    """A gap ceiling above the prefill budget would be less safe than before."""
    assert resolve_stream_chunk_gap_timeout_s(env_value="99999") == (
        resolve_stream_idle_timeout_s(env_value="")
    )


def test_a_typo_cannot_turn_every_healthy_stream_into_a_retry_storm() -> None:
    assert resolve_stream_chunk_gap_timeout_s(env_value="1") == (
        MIN_STREAM_CHUNK_GAP_TIMEOUT_S
    )


@pytest.mark.parametrize("raw", ["", "   ", "abc", "-5", "0"])
def test_unusable_values_fall_back_to_the_default(raw: str) -> None:
    assert resolve_stream_chunk_gap_timeout_s(env_value=raw) == (
        DEFAULT_STREAM_CHUNK_GAP_TIMEOUT_S
    )


# --- the wiring ------------------------------------------------------------


class _Delta:
    def __init__(self, content: str) -> None:
        self.content = content
        self.reasoning_content = None
        self.reasoning = None
        self.tool_calls = None
        self.function_call = None


class _Choice:
    def __init__(self, content: str, finish_reason: str | None = None) -> None:
        self.delta = _Delta(content)
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, content: str, finish_reason: str | None = None) -> None:
        self.choices = [_Choice(content, finish_reason)]
        self.usage = None


class _FakeStream:
    """Yields one chunk after ``first_delay``, then hangs for ``then`` seconds."""

    def __init__(self, *, first_delay: float, then: float) -> None:
        self._first_delay = first_delay
        self._then = then
        self._n = 0

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> Any:
        self._n += 1
        if self._n == 1:
            await asyncio.sleep(self._first_delay)
            return _Chunk("Hel")
        await asyncio.sleep(self._then)
        raise StopAsyncIteration


def _provider_for(stream: _FakeStream) -> OpenAICompatProvider:
    class _Completions:
        async def create(self, **kwargs: Any) -> _FakeStream:
            return stream

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    provider = OpenAICompatProvider(api_key="sk-test", default_model="test-model")
    provider._ensure_client = AsyncMock(return_value=_Client())  # type: ignore[method-assign]
    return provider


@pytest.mark.asyncio
async def test_a_mid_stream_stall_is_cut_off_rather_than_waited_out(monkeypatch) -> None:
    """The regression: this used to be bounded by 1800s, not by the gap ceiling."""
    monkeypatch.setattr(base_module, "MIN_STREAM_CHUNK_GAP_TIMEOUT_S", 0.05)
    monkeypatch.setenv("NANOBOT_STREAM_CHUNK_GAP_TIMEOUT_S", "0.1")

    provider = _provider_for(_FakeStream(first_delay=0, then=3))
    started = time.monotonic()
    response = await provider.chat_stream(_MESSAGES, model="test-model")
    elapsed = time.monotonic() - started

    assert elapsed < 1, "the prefill budget leaked into the gap"
    # chat_stream converts the timeout into the error response that
    # _run_chat_with_retry reads to decide a stall is recoverable.
    assert response.finish_reason == "error"
    assert response.error_kind == "timeout"


@pytest.mark.asyncio
async def test_the_first_chunk_keeps_the_generous_budget(monkeypatch) -> None:
    """A slow cold start must still be tolerated -- that is what 1800s is for.

    The gap ceiling is deliberately tiny here and the first chunk arrives well
    after it, so a pass proves the first wait is not using the gap timeout.
    """
    monkeypatch.setattr(base_module, "MIN_STREAM_CHUNK_GAP_TIMEOUT_S", 0.01)
    monkeypatch.setenv("NANOBOT_STREAM_CHUNK_GAP_TIMEOUT_S", "0.02")

    seen: list[str] = []

    async def on_content_delta(delta: str) -> None:
        seen.append(delta)

    provider = _provider_for(_FakeStream(first_delay=0.3, then=0.01))
    await provider.chat_stream(
        _MESSAGES,
        model="test-model",
        on_content_delta=on_content_delta,
    )

    assert seen == ["Hel"]
