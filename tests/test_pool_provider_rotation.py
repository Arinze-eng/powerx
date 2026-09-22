"""Tests for provider-pool rotation and failover.

The pool spreads requests round-robin across enabled lanes and retries the next
lane whenever a lane answers with a transient error. These tests pin that
contract so a single rate-limited key can never take the whole agent down.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

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


def test_chat_reports_a_transient_error_over_a_terminal_one() -> None:
    # Exactly the production shape: a primary lane that is rate-limited and a
    # lane whose key was rejected. The 401 is the dead lane's own problem - the
    # pool parks it - so the rate limit is what actually blocked the request and
    # the one worth reporting. Surfacing the 401 sent the operator chasing a key
    # while the real cause was upstream overload.
    healthy = _lane("primary", [LLMResponse(content=None, error_kind="rate_limit")])
    dead = _lane("dead", [LLMResponse(content=None, error_status_code=401)])
    pool = _provider(healthy, dead)
    assert asyncio.run(pool.chat(_MESSAGES)).error_kind == "rate_limit"


def test_chat_returns_the_first_error_when_every_lane_fails_terminally() -> None:
    # With only terminal failures on offer the first lane tried is reported:
    # parking rotates a newly dead lane to the back of the order, so the last
    # lane's error says the least about the pool's health.
    first = _lane("a", [LLMResponse(content=None, error_status_code=401)])
    second = _lane("b", [LLMResponse(content=None, error_status_code=402)])
    pool = _provider(first, second)
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.error_status_code == 401
    assert first[1].calls == 1
    assert second[1].calls == 1


def test_chat_error_is_stable_however_the_lanes_rotate() -> None:
    # A dead lane is parked after its first failure, which rotates it to the
    # back of the order. The reported error must not change with the rotation.
    healthy = _lane("primary", [LLMResponse(content=None, error_status_code=503)])
    dead = _lane("dead", [LLMResponse(content=None, error_status_code=401)])
    pool = _provider(healthy, dead)
    assert asyncio.run(pool.chat(_MESSAGES)).error_status_code == 503
    assert asyncio.run(pool.chat(_MESSAGES)).error_status_code == 503


def test_stream_reports_a_transient_error_over_a_terminal_one() -> None:
    healthy = _lane("primary", [LLMResponse(content=None, error_kind="overloaded")])
    dead = _lane("dead", [LLMResponse(content=None, error_status_code=401)])
    pool = _provider(healthy, dead)
    assert asyncio.run(pool.chat_stream(_MESSAGES)).error_kind == "overloaded"


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


def test_chat_fails_over_when_primary_key_is_rejected() -> None:
    # A revoked/expired key on the primary lane must not strand the request:
    # the backup lane is the entire reason the pool exists.
    first = _lane("a", [LLMResponse(content=None, error_status_code=401)])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.content == "b-answer"
    assert first[1].calls == 1
    assert second[1].calls == 1


def test_chat_fails_over_on_forbidden_key() -> None:
    first = _lane("a", [LLMResponse(content=None, error_status_code=403)])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    assert asyncio.run(pool.chat(_MESSAGES)).content == "b-answer"


def test_chat_fails_over_when_primary_is_out_of_credit() -> None:
    # 402 = balance exhausted. The backup lane should still serve the request.
    first = _lane("a", [LLMResponse(content=None, error_status_code=402)])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    assert asyncio.run(pool.chat(_MESSAGES)).content == "b-answer"


def test_chat_fails_over_on_auth_error_kind() -> None:
    first = _lane("a", [LLMResponse(content=None, error_kind="auth")])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    assert asyncio.run(pool.chat(_MESSAGES)).content == "b-answer"


def test_chat_fails_over_on_invalid_api_key_error_code() -> None:
    first = _lane("a", [LLMResponse(content=None, error_code="invalid_api_key")])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    assert asyncio.run(pool.chat(_MESSAGES)).content == "b-answer"


def test_chat_fails_over_when_model_is_missing_on_the_lane() -> None:
    # 404 usually means the model id is not served by that lane, which is a
    # lane-specific problem rather than a bad request.
    first = _lane("a", [LLMResponse(content=None, error_status_code=404)])
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    assert asyncio.run(pool.chat(_MESSAGES)).content == "b-answer"


def test_stream_fails_over_on_auth_error_before_output() -> None:
    class _AuthFailing(_FakeProvider):
        async def chat_stream(self, messages, **kwargs):  # type: ignore[override]
            self.calls += 1
            return LLMResponse(content=None, error_status_code=401)

    first = ({"id": "a", "model": "m-a"}, _AuthFailing("a"))
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    response = asyncio.run(pool.chat_stream(_MESSAGES))
    assert response.content == "b-answer"
    assert second[1].calls == 1


def test_dead_lane_fails_over_instead_of_hanging() -> None:
    """The core bug: a lane that never answers must not block the backup.

    A host that does not resolve, or that accepts the socket and never replies,
    produces no error at all. Before the per-lane timeout the pool awaited that
    lane forever, so the user saw "no response" and the backup key was never
    tried.
    """
    import asyncio as _asyncio
    import os

    os.environ["PROVIDER_POOL_LANE_TIMEOUT_S"] = "0.2"

    class _NeverAnswers(_FakeProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls += 1
            await _asyncio.sleep(30)
            return LLMResponse(content="too late")

    first = ({"id": "a", "model": "m-a"}, _NeverAnswers("a"))
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    try:
        response = _asyncio.run(pool.chat(_MESSAGES))
        assert response.content == "b-answer"
        assert first[1].calls == 1, "the dead lane should have been tried once"
        assert second[1].calls == 1, "the backup lane must be reached"
    finally:
        os.environ.pop("PROVIDER_POOL_LANE_TIMEOUT_S", None)


def test_lane_timeout_is_reported_as_a_failover_error() -> None:
    import asyncio as _asyncio
    import os

    os.environ["PROVIDER_POOL_LANE_TIMEOUT_S"] = "0.2"

    class _NeverAnswers(_FakeProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls += 1
            await _asyncio.sleep(30)
            return LLMResponse(content="too late")

    only = ({"id": "a", "label": "Key one", "model": "m-a"}, _NeverAnswers("a"))
    pool = PoolProvider([only])
    try:
        response = _asyncio.run(pool.chat(_MESSAGES))
        assert response.content is None
        assert response.error_kind == "timeout"
        assert response.error_type == "lane_timeout"
    finally:
        os.environ.pop("PROVIDER_POOL_LANE_TIMEOUT_S", None)


def test_lane_timeout_can_be_disabled() -> None:
    # A non-positive value restores the old unbounded behaviour on purpose.
    import os

    os.environ["PROVIDER_POOL_LANE_TIMEOUT_S"] = "0"
    try:
        from nanobot.providers.pool_provider import _lane_timeout_s

        assert _lane_timeout_s() is None
    finally:
        os.environ.pop("PROVIDER_POOL_LANE_TIMEOUT_S", None)


def test_stream_dead_lane_fails_over_before_output() -> None:
    import asyncio as _asyncio
    import os

    os.environ["PROVIDER_POOL_LANE_TIMEOUT_S"] = "0.2"

    class _NeverAnswers(_FakeProvider):
        async def chat_stream(self, messages, **kwargs):  # type: ignore[override]
            self.calls += 1
            await _asyncio.sleep(30)
            return LLMResponse(content="too late")

    first = ({"id": "a", "model": "m-a"}, _NeverAnswers("a"))
    second = _lane("b", [LLMResponse(content="b-answer")])
    pool = _provider(first, second)
    try:
        response = _asyncio.run(pool.chat_stream(_MESSAGES))
        assert response.content == "b-answer"
        assert second[1].calls == 1
    finally:
        os.environ.pop("PROVIDER_POOL_LANE_TIMEOUT_S", None)


def test_empty_pool_reports_a_connection_error() -> None:
    pool = PoolProvider([])
    response = asyncio.run(pool.chat(_MESSAGES))
    assert response.content is None
    assert response.error_kind == "connection"


def test_default_model_comes_from_the_first_lane() -> None:
    pool = _provider(({"id": "a", "model": "model-a"}, _FakeProvider("a")), ({"id": "b", "model": "model-b"}, _FakeProvider("b")))
    assert pool.get_default_model() == "model-a"


def test_failed_lane_stops_taking_the_first_attempt() -> None:
    """An exhausted lane must not keep being the first try of every request.

    This is the reported symptom: the pool handed the next request to the same
    unusable lane first, so what the user saw was that lane's error rather than
    an answer from a healthy one.
    """
    dead = _lane("dead", [LLMResponse(content=None, error_status_code=402)])
    live = _lane("live", [LLMResponse(content="live-answer")])
    pool = _provider(dead, live)

    assert asyncio.run(pool.chat(_MESSAGES)).content == "live-answer"
    assert asyncio.run(pool.chat(_MESSAGES)).content == "live-answer"

    assert dead[1].calls == 1, "the exhausted lane must not get the next first attempt"
    assert live[1].calls == 2


def test_parking_never_starves_the_pool() -> None:
    """With every lane parked the pool still tries them rather than giving up."""
    first = _lane("a", [LLMResponse(content=None, error_kind="rate_limit")])
    second = _lane("b", [LLMResponse(content=None, error_kind="rate_limit")])
    pool = _provider(first, second)

    assert asyncio.run(pool.chat(_MESSAGES)).error_kind == "rate_limit"
    assert asyncio.run(pool.chat(_MESSAGES)).error_kind == "rate_limit"

    assert first[1].calls == 2
    assert second[1].calls == 2


def test_terminal_lane_failure_parks_longer_than_a_transient_one() -> None:
    """An out-of-credit or revoked key is parked for longer than a 429."""
    pool = _provider(_lane("a"), _lane("b"))
    terminal = pool._park_window_s(LLMResponse(content=None, error_status_code=402))
    transient = pool._park_window_s(LLMResponse(content=None, error_kind="rate_limit"))
    assert terminal > transient > 0


def test_parking_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER_POOL_LANE_PARK_S", "0")
    monkeypatch.setenv("PROVIDER_POOL_LANE_COOLDOWN_S", "0")
    dead = _lane("dead", [LLMResponse(content=None, error_status_code=402)])
    live = _lane("live", [LLMResponse(content="live-answer")])
    pool = _provider(dead, live)
    assert asyncio.run(pool.chat(_MESSAGES)).content == "live-answer"
    assert pool._is_parked(dead[0]) is False, "parking disabled keeps the lane in rotation"
