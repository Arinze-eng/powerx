"""Public, key-free price lookup for the polling & vigilance engine.

The ``poll`` tool is a general-purpose polling engine for *any* task, on the
WebUI and Telegram alike — trading is only one use case. Before this module
existed, any watch that carried a ``symbol`` (e.g. "check XAUUSD price in 2
minutes") was routed through the Alpaca market tick, which *requires* trading
credentials to fetch a price. Without a connected account every tick failed
with "cannot fetch price … no connected trading account", the condition was
never met, the watch never completed and the user was never notified. That is
exactly the reported symptom: "it shows it is watching but when the time comes
it does not deliver the result".

This module provides a **key-free** fallback so price-watch / notify / "tell me
the price of X" tasks actually complete for anyone, without an Alpaca account:

* Crypto (BTC, ETH, SOL, …) and precious metals (XAU⚛ gold, XAG⚛ silver) are
  covered by the Coinbase public spot API (no key, geo-reachable from the
  deployment regions we use).
* CoinGecko provides a name-based crypto fallback (bitcoin, ethereum, …).

Only genuine **trading** actions (buy / sell / close a position) still require
Alpaca credentials — those must place orders and cannot be satisfied by a price
feed alone. Everything else can now resolve a real, live price and deliver it.

Every request is wrapped defensively: a failure just returns ``None`` so the
polling loop degrades gracefully instead of crashing.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

# Coinbase public spot price endpoint (no API key required).
_COINBASE_SPOT = "https://api.coinbase.com/v2/prices/{pair}/spot"

# Coinbase covers crypto (BTC-USD, ETH-USD, ...) and precious metals (XAU-USD,
# XAG-USD). Known metal symbols we route to Coinbase directly.
_METALS = {"XAU", "XAG", "XPT", "XPD", "PAXG"}

# CoinGecko name map for crypto symbols Coinbase does not carry or that a user
# gave as a plain ticker we want a second source for. Maps symbol -> coingecko id.
_COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "DOT": "polkadot",
    "LTC": "litecoin",
    "AVAX": "avalanche-2",
    "MATIC": "matic-network",
    "LINK": "chainlink",
    "BNB": "binancecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
    "SHIB": "shiba-inu",
    "UNI": "uniswap",
    "TON": "the-open-network",
    "TRX": "tron",
    "NEAR": "near",
    "APT": "aptos",
    "ARB": "arbitrum",
    "OP": "optimism",
    "SUI": "sui",
    "SEI": "sei-network",
}

_TIMEOUT_S = 8.0


def _normalize_symbol(symbol: str) -> str:
    """Upper-case and strip common suffixes to a base ticker like 'XAU'/'BTC'."""
    return re.sub(r"(?i)(USDT|USDC|USD|EUR)$", "", symbol.strip().upper()) or symbol.strip().upper()


def is_public_price_symbol(symbol: str) -> bool:
    """Return whether ``symbol`` is resolvable without broker credentials.

    Cryptocurrencies and precious metals are covered. Equities/ETFs (AAPL,
    SPY, …) are not, because no stable key-free public feed is available;
    those genuinely need Alpaca (or another broker data source).
    """
    base = _normalize_symbol(symbol)
    if base in _METALS:
        return True
    return base in _COINGECKO_IDS


async def fetch_public_price(symbol: str) -> float | None:
    """Return the latest USD price for ``symbol`` without any API key.

    Tries Coinbase first (crypto + metals), then CoinGecko (crypto by name).
    Returns ``None`` when the symbol is unsupported or every upstream fails.
    """
    base = _normalize_symbol(symbol)
    if base in _METALS:
        return await _coinbase_metal_price(base)
    return await _crypto_price(base)


async def _coinbase_metal_price(base: str) -> float | None:
    pair = f"{base}-USD"
    return await _coinbase_price(pair)


async def _crypto_price(base: str) -> float | None:
    # Try Coinbase first for crypto pairs it lists (BTC-USD, ETH-USD, ...).
    pair = f"{base}-USD"
    price = await _coinbase_price(pair)
    if price is not None:
        return price
    # Fall back to CoinGecko by name when Coinbase does not list the symbol.
    cg_id = _COINGECKO_IDS.get(base)
    if cg_id is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd"},
            )
        if not response.is_success:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        coin = payload.get(cg_id)
        if not isinstance(coin, dict):
            return None
        return _as_price(coin.get("usd"))
    except (httpx.HTTPError, ValueError):
        return None


async def _coinbase_price(pair: str) -> float | None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(_COINBASE_SPOT.format(pair=pair))
        if not response.is_success:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        return _as_price(data.get("amount"))
    except (httpx.HTTPError, ValueError):
        return None


def _as_price(value: Any) -> float | None:
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None
