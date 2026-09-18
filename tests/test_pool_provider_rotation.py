"""Tests for provider-pool rotation and failover.

The pool spreads requests round-robin across enabled lanes and retries the next
lane whenever a lane answers with a transient error. These tests pin that
contract so a single rate-limited key can never take the whole agent down.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobot.providers.base import LLMResponse
from nanobot.providers.pool_provider import PoolProvider


class _FakeProvider:
    """Minimal LLMProvider stand-in that replays a scripted list of responses."""

    def __init__(self, name: str, responses: list[LLMResponse] | None = None) -> None:
        self.name = name
        self.calls = 0
        self._responses = responses or [LLMResponse(content=f"{name}-ok")]

    def _next(self) -> LLMResponse:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]

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
        return self._next()

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
        return self._next()


def _lane(name: str, responses: list[LLMResponse] | None = None) -> tuple[dict[str, Any], _FakeProvider]:
    return ({"id": name, "model": f"model-{name}"}, _FakeProvider(name, responses))


_MESSAGES = [{"role": "user", "content": "hi"}]


def _provider(*lanes: tuple[dict[str, Any], _FakeProvider]) -> PoolProvider:
    return PoolProvider(list(lanes))


def test_rotation_visits_lanes_round_robin() -> None:
    pool = _provider(_lane("a"), _lane("b"), _lane("c"))
    seen = [pool._order()[0][0]["id"] for _ in range(6)]
    assert seen == ["a", "b", "c", "a", "b", "c"]


def test_rotation_wraps_the_lane_list() -> None:
    pool = _provider(_lane("a"), _lane("b"), _lane("c"))
    order = pool._order()
    assert [entry["id"] for entry, _ in order] == ["a", "b", "c"]
    assert [entry["id"] for entry, _ in pool._order()] == ["b", "c", "a"]


def test_chat_uses_the_first_lane_when_it_succeeds() -> None:
    first = _lane("a", [LLMResponse(content="a-answer")])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.content == "a-answer"
    assert first[1].calls == 1
    assert second[1].calls == 0


def test_chat_fails_over_on_rate_limit() -> None:
    first = _lane("a", [LLMResponse(content=None, error_kind="rate_limit")])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.content == "b-answer"
    assert first[1].calls == 1
    assert second[1].calls == 1


def test_chat_fails_over_on_http_429() -> None:
    first = _lane("a", [LLMResponse(content=None, error_status_code=429)])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.content == "b-answer"


def test_chat_fails_over_on_quota_error_code() -> None:
    first = _lane("a", [LLMResponse(content=None, error_code="insufficient_quota")])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    assert asyncio.run(pool.chat(_MESSAGES)).content == "b-answer"


def test_chat_returns_last_error_when_every_lane_fails() -> None:
    first = _lane("a", [LLMResponse(content=None, error_kind="rate_limit")])
    second = _lane("b", [LLMResponse(content=None, error_kind="server_error")])
    pool = _provider(first, second)
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.error_kind == "server_error"
    assert first[1].calls == 1
    assert second[1].calls == 1


def test_chat_does_not_rotate_on_a_real_answer() -> None:
    # A 400 from the provider is a request problem, not a lane problem: the
    # response must surface instead of burning every lane.
    first = _lane("a", [LLMResponse(content=None, error_status_code=400)])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.error_status_code == 400
    assert second[1].calls == 0


def test_stream_does_not_rotate_after_output_started() -> None:
    async def _run() -> LLMResponse:
        chunks: list[str] = []

        async def _on_delta(delta: str) -> None:
            chunks.append(delta)

        class _StreamingProvider(_FakeProvider):
            async def chat_stream(self, messages, **kwargs):  # type: ignore[override]
                callback = kwargs.get("on_content_delta")
                if callback is not None:
                    await callback("partial")
                self.calls += 1
                return LLMResponse(content="partial", error_kind="rate_limit")

        first = ({"id": "a", "model": "m-a"}, _StreamingProvider("a"))
        second = _lane("b", [LLMResponse(content="b-answer")])
        pool = _provider(first, second)
        response = await pool.chat_stream(_MESSAGES, on_content_delta=_on_delta)
        assert chunks == ["partial"]
        assert second[1].calls == 0
        return response

    response = asyncio.run(_run())
    assert response.content == "partial"


def test_empty_pool_reports_a_connection_error() -> None:
    pool = PoolProvider([])
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.content is None
    assert response.error_kind == "connection"


def test_default_model_comes_from_the_first_lane() -> None:
    pool = _provider(({"id": "a", "model": "model-a"}, _FakeProvider("a")), ({"id": "b", "model": "model-b"}, _FakeProvider("b")))
    assert pool.get_default_model() == "model-a"
