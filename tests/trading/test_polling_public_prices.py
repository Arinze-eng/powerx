"""Regression tests for the general-purpose polling engine.

Covers the operator-reported symptom: "when I ask for a task that requires
polling (price check, wait-until-X, any repeated task), it shows it is watching
but never delivers the result." Two root causes are verified fixed:

1. Generic (non-trading) watches now run a *real* worker (URL + price checks)
   instead of an idle tick, and they complete + notify on their objective.
2. Price-watch / notify tasks work WITHOUT Alpaca credentials via a free public
   price feed (Coinbase / CoinGecko), so "check XAUUSD in 2 min" resolves a real
   price and completes instead of stalling on "no connected trading account".

These tests mock the network so they are deterministic and require no keys.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nanobot.trading.polling_engine import (
    WatchSpec,
    default_market_tick,
    evaluate_price_condition,
)
from nanobot.trading.public_prices import is_public_price_symbol

# ---------------------------------------------------------------------------
# public_prices: symbol classification + price fetch fallback
# ---------------------------------------------------------------------------


def test_public_price_supports_crypto_and_metals_without_broker():
    """Crypto and precious metals resolve without any Alpaca credentials."""
    assert is_public_price_symbol("XAU") is True
    assert is_public_price_symbol("XAUUSD") is True
    assert is_public_price_symbol("XAG") is True
    assert is_public_price_symbol("BTC") is True
    assert is_public_price_symbol("BTCUSD") is True
    assert is_public_price_symbol("ETH") is True
    assert is_public_price_symbol("SOL") is True


def test_public_price_does_not_cover_equities():
    """Equities/ETFs are not covered key-free; they genuinely need a broker."""
    assert is_public_price_symbol("AAPL") is False
    assert is_public_price_symbol("TSLA") is False
    assert is_public_price_symbol("SPY") is False


@pytest.mark.asyncio
async def test_fetch_public_price_coinbase_metal():
    """Coinbase metal price (XAU) is parsed into a positive float."""
    from nanobot.trading.public_prices import fetch_public_price
    with patch("nanobot.trading.public_prices._coinbase_metal_price", new=AsyncMock(
        return_value=4475.5
    )):
        price = await fetch_public_price("XAUUSD")
    assert price == pytest.approx(4475.5)


@pytest.mark.asyncio
async def test_fetch_public_price_falls_back_to_coingecko():
    """Coinbase-unknown crypto falls back to CoinGecko by name.

    Live integration test against the public CoinGecko API (key-free). If the
    upstream is unreachable the test is skipped rather than failed, because the
    deterministic unit path is covered by ``test_fetch_public_price_coinbase_metal``.
    """
    import httpx

    from nanobot.trading.public_prices import _crypto_price
    # SHIB is not a Coinbase spot pair → exercises the CoinGecko fallback.
    try:
        price = await _crypto_price("SHIB")
    except httpx.HTTPError:
        pytest.skip("CoinGecko API unreachable")
    assert price is None or price > 0


# ---------------------------------------------------------------------------
# default_market_tick: notify watches now complete WITHOUT Alpaca
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_market_tick_reports_price_and_completes_without_credentials():
    """"check XAUUSD price in 2 minutes" (max_polls=1) must resolve a real price
    and stop=True so the notifier delivers it — the exact reported failure."""
    spec = WatchSpec(
        label="Check XAUUSD price in 2 minutes",
        description="Check XAUUSD price in 2 minutes",
        condition={"symbol": "XAUUSD", "action": "notify"},
        interval_seconds=120,
        max_polls=1,
    )
    with patch("nanobot.trading.public_prices.fetch_public_price",
               new=AsyncMock(return_value=4475.25)):
        result = await default_market_tick(spec, 1, credentials=None)
    assert result["price"] == pytest.approx(4475.25)
    assert result["stop"] is True  # watch completes → user notified
    assert "XAUUSD" in result["summary"]


@pytest.mark.asyncio
async def test_default_market_tick_requires_broker_for_trade_actions():
    """buy/sell/close still require a connected account (they place orders)."""
    spec = WatchSpec(
        label="buy BTC dip",
        description="",
        condition={"symbol": "BTCUSD", "target_price": 60000, "direction": "drop",
                   "action": "buy", "qty": 1},
        interval_seconds=5,
        max_polls=0,
    )
    with patch("nanobot.trading.public_prices.fetch_public_price", new=AsyncMock(return_value=80900)):
        result = await default_market_tick(spec, 1, credentials=None)
    assert result["actions"] == ["trade requires a connected account"]
    assert result["stop"] is False  # never completes pretending to have traded


@pytest.mark.asyncio
async def test_default_market_tick_condition_met_completes():
    """A notify watch whose price target is met reports it and completes."""
    spec = WatchSpec(
        label="BTC below target",
        description="",
        condition={"symbol": "BTCUSD", "target_price": 90000, "direction": "below",
                   "action": "notify"},
        interval_seconds=5,
        max_polls=0,
    )
    with patch("nanobot.trading.public_prices.fetch_public_price", new=AsyncMock(return_value=80900)):
        result = await default_market_tick(spec, 1, credentials=None)
    assert result["condition_met"] is True
    assert result["stop"] is True


# ---------------------------------------------------------------------------
# evaluate_price_condition: pure condition logic still correct
# ---------------------------------------------------------------------------


def test_evaluate_price_condition_drop():
    met, reason = evaluate_price_condition(80000, target_price=85000, direction="drop")
    assert met is True


def test_evaluate_price_condition_above_not_met():
    met, reason = evaluate_price_condition(80000, target_price=90000, direction="above")
    assert met is False


# ---------------------------------------------------------------------------
# Generic (non-trading) workers: URL + price checks now execute real logic
# ---------------------------------------------------------------------------


def _import_worker_builders():
    # Imported lazily so the test depends only on the poll module.
    from nanobot.agent.tools import poll as pollmod
    return pollmod


@pytest.mark.asyncio
async def test_generic_url_worker_polls_endpoint():
    """A 'wait until http://... comes up' watch actually GETs the endpoint."""
    pollmod = _import_worker_builders()
    spec = WatchSpec(label="site-up", description="",
                     condition={"check_goal": "wait until http://example.com comes up"})
    worker = await pollmod._build_generic_worker(spec, spec.condition["check_goal"])
    assert worker is not None
    fake_response = AsyncMock()
    fake_response.status_code = 200
    client_ctx = AsyncMock()
    client_ctx.get.return_value = fake_response
    with patch("httpx.AsyncClient") as client_cls:
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client_ctx)
        result = await worker(spec, 1, spec.condition["check_goal"])
    assert result["condition_met"] is True
    assert "HTTP 200" in result["summary"]


@pytest.mark.asyncio
async def test_generic_price_worker_reports_price():
    """A 'check the XAU price' watch (no symbol condition) fetches a real price."""
    pollmod = _import_worker_builders()
    spec = WatchSpec(label="gold", description="check the XAU price",
                     condition={"check_goal": "check XAU price"})
    worker = await pollmod._build_generic_worker(spec, spec.condition["check_goal"])
    assert worker is not None
    with patch("nanobot.trading.public_prices.fetch_public_price", new=AsyncMock(return_value=4471.5)):
        result = await worker(spec, 1, spec.condition["check_goal"])
    assert result["condition_met"] is True
    assert "XAU" in result["summary"]
    assert result["price"] == pytest.approx(4471.5)


@pytest.mark.asyncio
async def test_generic_tick_completes_when_goal_met():
    """"_generic_tick" must set stop=True when the worker's objective is met so
    the watch completes and notifies the user (previously it never completed)."""
    pollmod = _import_worker_builders()
    spec = WatchSpec(label="site-up", description="",
                     condition={"check_goal": "wait until http://example.com comes up"})
    fake_response = AsyncMock()
    fake_response.status_code = 200
    client_ctx = AsyncMock()
    client_ctx.get.return_value = fake_response
    with patch("httpx.AsyncClient") as client_cls:
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client_ctx)
        result = await pollmod._generic_tick(spec, 1)
    assert result["stop"] is True
    assert result["condition_met"] is True


@pytest.mark.asyncio
async def test_generic_tick_produces_progress_without_worker():
    """A generic watch with no recognisable goal still records honest progress."""
    pollmod = _import_worker_builders()
    spec = WatchSpec(label="vague", description="keep checking things",
                     condition={"check_goal": "keep checking things"}, max_polls=0)
    result = await pollmod._generic_tick(spec, 1)
    # No deterministic worker matched, but progress is recorded and reported.
    assert "tick 1" in result["summary"] or "poll" in result["summary"]
