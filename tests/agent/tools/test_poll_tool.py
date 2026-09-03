"""Tests for the polling/vigilance engine and poll tool.

Covers:
* condition evaluation (drop/above/move/breakout)
* natural-language price extraction
* WatchManager running a background loop and stopping it
* PollTool schema/discovery
"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.poll import PollTool
from nanobot.trading.polling_engine import (
    WatchManager,
    WatchSpec,
    evaluate_price_condition,
    get_manager,
    is_expired,
    parse_natural_language_price,
)


def test_condition_drop() -> None:
    met, reason = evaluate_price_condition(250.0, target_price=245.0, direction="drop")
    assert not met
    met, reason = evaluate_price_condition(240.0, target_price=245.0, direction="drop")
    assert met


def test_condition_above() -> None:
    met, _ = evaluate_price_condition(240.0, target_price=245.0, direction="above")
    assert not met
    met, _ = evaluate_price_condition(250.0, target_price=245.0, direction="above")
    assert met


def test_condition_move_percent() -> None:
    # 260 -> 250 is a -3.85% move, under the 5% threshold.
    met, _ = evaluate_price_condition(
        250.0, target_price=245.0, direction="drop", reference_price=260.0, move_percent=5.0
    )
    assert not met
    met, _ = evaluate_price_condition(
        240.0, target_price=245.0, direction="drop", reference_price=260.0, move_percent=5.0
    )
    assert met


def test_condition_breakout() -> None:
    met, _ = evaluate_price_condition(
        300.0, trigger_price=295.0, reference_price=290.0, direction="breakout"
    )
    assert met


def test_natural_language_price() -> None:
    p = parse_natural_language_price("buy APPL when it drops to 245 dollars")
    assert p.get("direction") == "drop"
    assert p.get("target_price") == 245.0


def test_expired() -> None:
    spec = WatchSpec(label="x", expires_at="2020-01-01T00:00:00+00:00")
    assert is_expired(spec)
    spec2 = WatchSpec(label="x", expires_at="2099-01-01T00:00:00+00:00")
    assert not is_expired(spec2)


def test_poll_limit_reached() -> None:
    spec = WatchSpec(label="x", max_polls=3)
    # Not reached during ticks 1 and 2.
    assert not spec.poll_limit_reached(1)
    assert not spec.poll_limit_reached(2)
    # Reached once tick == max_polls.
    assert spec.poll_limit_reached(3)
    # Unlimited watches never "reach" the limit.
    spec_unlimited = WatchSpec(label="x", max_polls=0)
    assert not spec_unlimited.poll_limit_reached(1000)


def test_engine_single_tick() -> None:
    """A single-tick watch should complete after the first poll."""

    async def tick(spec: WatchSpec, n: int) -> dict:
        return {"summary": "ok", "condition_met": True, "stop": True, "price": 100.0}

    async def run() -> dict:
        mgr = get_manager()
        spec = WatchSpec(label="tick", interval_seconds=0.01, max_polls=5)
        result: dict = {}

        async def on_complete(wid: int, res) -> None:
            result["status"] = res.status
            result["tick"] = res.tick
            result["met"] = res.condition_met
            result["price"] = res.last_price

        await mgr.start(spec, tick, on_complete=on_complete)
        await asyncio.sleep(0.05)
        return result

    res = asyncio.run(run())
    assert res.get("status") == "completed", res
    assert res.get("met") is True
    assert res.get("price") == 100.0


def test_engine_multi_tick_until_condition() -> None:
    """A market-style watch should keep polling until the condition is met."""

    async def run() -> dict:
        prices = iter([300.0, 275.0])
        mgr = WatchManager()

        async def tick(spec: WatchSpec, n: int) -> dict:
            price = next(prices)
            met, reason = evaluate_price_condition(
                price,
                target_price=spec.condition.get("target_price"),
                direction=spec.condition.get("direction", "breakout"),
            )
            return {
                "price": price,
                "condition_met": met,
                "actions": ["notify"] if met else [],
                "stop": met,
                "summary": reason,
            }

        spec = WatchSpec(
            label="tsla",
            description="notify when it drops to 280",
            condition={"symbol": "TSLA", "target_price": 280.0, "direction": "drop", "action": "notify"},
            interval_seconds=0.01,
            max_polls=5,
        )
        result: dict = {}

        async def on_complete(wid: int, res) -> None:
            result["status"] = res.status
            result["met"] = res.condition_met
            result["price"] = res.last_price
            result["tick"] = res.tick

        await mgr.start(spec, tick, on_complete=on_complete)
        for _ in range(25):
            await asyncio.sleep(0.05)
            if result:
                break
        return result

    res = asyncio.run(run())
    assert res.get("status") == "completed", res
    assert res.get("met") is True
    assert res.get("tick") == 2
    assert res.get("price") == 275.0


def test_poll_tool_discovered() -> None:
    loader = ToolLoader()
    names = [cls.__name__ for cls in loader.discover()]
    assert "PollTool" in names


def test_poll_tool_schema() -> None:
    tool = PollTool()
    assert tool.name == "poll"
    props = tool.parameters["properties"]
    assert "action" in props
    assert props["action"]["enum"] == ["poll", "status", "stop"]
    assert tool.parameters["required"] == ["action"]


def test_poll_tool_stop_requires_id() -> None:
    tool = PollTool()
    res = asyncio.run(tool.execute(action="stop"))
    assert res.is_error
    assert "watch_id" in str(res)


def test_generic_task_start_builds_check_goal(monkeypatch) -> None:
    """A non-trading generic watch stores its natural-language `check_goal` and
    does not require any broker credentials."""
    # No Alpaca env (generic tasks must not need trading creds).
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    # Pretend Supabase is unset so the store is skipped during the test.
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    tool = PollTool()
    res = asyncio.run(
        tool.execute(
            action="poll",
            label="staging",
            description="poll until the staging server returns 200",
            check_goal="staging server returns HTTP 200",
            interval_seconds=5,
            max_polls=3,
        )
    )
    assert not res.is_error, res
    assert "generic task watch" in str(res)
    assert "staging server returns HTTP 200" in str(res)


def test_store_create_and_list(monkeypatch) -> None:
    """PollingStore persistence uses Supabase REST; verify with a mocked transport."""

    import httpx

    from nanobot.trading.polling_engine import PollingStore

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")

    hit_watches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hit_watches.append(str(request.url))
        if "polling_watch_runs" in str(request.url) and request.method == "GET":
            return httpx.Response(200, json=[])
        if "polling_watches" in str(request.url) and request.method == "GET":
            return httpx.Response(200, json=[{"id": 10, "label": "x", "status": "running"}])
        return httpx.Response(201, json=[{"id": 10}])

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return original(**kwargs, transport=transport)

        # Each `async with AsyncClient()` in _request opens+closes its client,
        # so return a fresh client every call, sharing the same MockTransport.
        httpx.AsyncClient = _client  # type: ignore[assignment,misc]
        try:
            store = PollingStore()
            assert store.enabled
            spec = WatchSpec(label="x", description="test")
            wid = await store.create_watch(spec)
            await store.list_watches()
            assert wid == 10
        finally:
            httpx.AsyncClient = original  # type: ignore[assignment,misc]

    asyncio.run(run())
    assert any("polling_watches" in url for url in hit_watches)
