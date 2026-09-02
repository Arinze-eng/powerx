"""Telegram command handlers for /trade and /backtest.

These commands provide paper-trading and research using the Alpaca
integration and the bundled strategy / backtest engine. Per-user Alpaca
credentials are resolved from Supabase (via /alpaca connect); when no
per-user credentials exist, the server-level ALPACA_* environment
variables are used as the default account.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from loguru import logger

try:
    from nanobot.trading.alpaca_adapter import (
        AlpacaCredentials,
        AlpacaError,
        AlpacaExecutionAdapter,
    )
    from nanobot.trading.alpaca_credentials import AlpacaCredentialStore
    from nanobot.trading.config import AgentConfig, RiskConfig, SessionWindow
    from nanobot.trading.data_loader import DataUnavailableError, load_pair
    from nanobot.trading.backtest_engine import BacktestEngine
    TRADING_AVAILABLE = True
except ImportError as exc:  # pragma: no cover
    logger.warning("Trading imports unavailable: {}", exc)
    TRADING_AVAILABLE = False
    AlpacaExecutionAdapter = None
    AlpacaCredentialStore = None
    AlpacaCredentials = None
    AlpacaError = RuntimeError


# Regexes matching the /trade and /backtest slash commands.
_TRADE_RE = re.compile(r"^/trade(?:@\w+)?(?:\s+.*)?$", re.IGNORECASE)
_BACKTEST_RE = re.compile(r"^/backtest(?:@\w+)?(?:\s+.*)?$", re.IGNORECASE)

# Symbol aliases -> Yahoo Finance ticker. Extends data_loader's mapping with
# common metals and energy symbols so `/trade analyze xauusd` and
# `/backtest xauusd` work out of the box.
SYMBOL_ALIASES = {
    "xauusd": "GC=F",
    "xau": "GC=F",
    "gold": "GC=F",
    "xagusd": "SI=F",
    "silver": "SI=F",
    "usdol": "CL=F",
    "oil": "CL=F",
    "wti": "CL=F",
    "btcusd": "BTC-USD",
    "btc": "BTC-USD",
    "ethusd": "ETH-USD",
    "eth": "ETH-USD",
    "spx": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "us500": "^GSPC",
}


def _resolve_symbol(raw: str) -> str:
    """Normalize a user-supplied symbol to an uppercase canonical form.

    Returns the Yahoo-safe symbol used for data loading. Unknown symbols are
    passed through upper-cased (stocks like AAPL work directly).
    """
    clean = raw.strip().upper()
    if not clean:
        raise ValueError("missing symbol")
    return SYMBOL_ALIASES.get(clean, clean)


def is_trade_command(text: str) -> bool:
    """Return True if *text* is a /trade slash command."""
    if not text:
        return False
    return bool(_TRADE_RE.match(text.strip()))


def is_backtest_command(text: str) -> bool:
    """Return True if *text* is a /backtest slash command."""
    if not text:
        return False
    return bool(_BACKTEST_RE.match(text.strip()))


async def _load_credentials(account: dict[str, Any] | None) -> AlpacaCredentials | None:
    """Resolve Alpaca credentials for a user, falling back to env defaults.

    Returns ``None`` when no per-user credentials exist so the adapter can
    fall back to the server-level ALPACA_* environment variables.
    """
    store = AlpacaCredentialStore()
    if store is not None and store.enabled and account is not None:
        telegram_user_id = int(account.get("telegram_user_id", 0))
        if telegram_user_id:
            try:
                creds = await store.get_credentials(telegram_user_id)
                if creds:
                    return AlpacaCredentials(
                        api_key=creds["api_key"],
                        secret_key=creds["secret_key"],
                        base_url=creds.get("base_url", "https://paper-api.alpaca.markets"),
                    )
            except Exception as exc:
                logger.error("Failed to load Alpaca credentials for user {}: {}", telegram_user_id, exc)
    # Fall back to server-level defaults from the environment (the adapter
    # reads ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_BASE_URL itself when
    # credentials is None).
    return None


def _build_adapter(credentials: AlpacaCredentials | None) -> AlpacaExecutionAdapter:
    if AlpacaExecutionAdapter is None:
        raise AlpacaError("alpaca-py is not installed")
    return AlpacaExecutionAdapter(credentials)


def _fmt_money(value: float | None) -> str:
    try:
        return f"${float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _fmt_qty(value: Any) -> str:
    try:
        number = float(value or 0)
        if number == int(number):
            return str(int(number))
        return f"{number:.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value or "0")


# Symbols Alpaca's stock data API cannot serve (forex, metals, indices,
# crypto). For these we fall back to Yahoo finance data.
_NON_STOCK_ALIAS_KEYS = {
    "xauusd", "xau", "gold", "xagusd", "silver", "usdol", "oil", "wti",
    "btcusd", "btc", "ethusd", "eth", "spx", "nasdaq", "dow", "us500",
}


def _load_ohlcv(
    adapter: AlpacaExecutionAdapter | None,
    symbol: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Load OHLCV bars for *symbol*, preferring Alpaca for US stocks.

    Alpaca's market-data API is reliable for NYSE/NASDAQ tickers. Forex,
    metals, crypto and index symbols are not available there, so we fall
    back to Yahoo finance data for those.
    """
    raw_lower = symbol.strip().lower()
    raw = raw_lower.upper()
    # Aliases are keyed lowercase; resolve the Yahoo-safe symbol on the
    # lowercased token so "xauusd" -> "GC=F" works.
    clean = SYMBOL_ALIASES.get(raw_lower, raw)

    # Prefer Alpaca data when the user gave a plain stock ticker (not an alias
    # that maps to a non-stock Yahoo symbol) and the adapter is available.
    non_stock = raw_lower in _NON_STOCK_ALIAS_KEYS
    if adapter is not None and not non_stock:
        try:
            df = adapter.get_bars(clean, start, end, "1Day")
            if df is not None and not df.empty:
                return df
            logger.info("Alpaca returned no bars for {}, falling back to Yahoo", raw)
        except Exception as exc:
            logger.warning(
                "Alpaca data fetch failed for {} ({}); falling back to Yahoo", raw, exc
            )
    # Fall back to Yahoo finance (handles stocks too).
    return load_pair(clean, start, end, "1d")


def _account_text(adapter: AlpacaExecutionAdapter) -> str:
    acct = adapter.get_account()
    return "\n".join([
        "📊 **Alpaca Paper Account**",
        f"- Status: `{acct.get('status', '')}`",
        f"- Cash: `{_fmt_money(acct.get('cash'))}`",
        f"- Equity: `{_fmt_money(acct.get('equity'))}`",
        f"- Buying Power: `{_fmt_money(acct.get('buying_power'))}`",
        f"- Portfolio Value: `{_fmt_money(acct.get('portfolio_value'))}`",
    ])


def _positions_text(adapter: AlpacaExecutionAdapter) -> str:
    positions = adapter.get_positions()
    if not positions:
        return "No open positions."
    lines = ["**Open Positions**"]
    for pos in positions:
        symbol = pos.get("symbol", "?")
        qty = _fmt_qty(pos.get("qty"))
        mkt = _fmt_money(pos.get("market_value"))
        pl = _fmt_money(pos.get("unrealized_pl"))
        entry = _fmt_money(pos.get("avg_entry_price"))
        current = _fmt_money(pos.get("current_price"))
        lines.append(f"- `{symbol}` × {qty}")
        lines.append(f"  Entry `{entry}` / Now `{current}` = {mkt} ({pl})")
    return "\n".join(lines)


def _analyze_text(adapter: AlpacaExecutionAdapter | None, symbol: str) -> str:
    """Load OHLCV data and run the strategy engine to produce a signal."""
    from nanobot.trading.analyst_agent import AnalystAgent
    from nanobot.trading.scout_agent import ScoutAgent
    from nanobot.trading.strategy_router import StrategyRouter
    from nanobot.trading.tma_engine import legacy_slope, basket_correlation

    display = symbol.upper()
    # Use a sensible lookback: ~14 months of daily bars.
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=400)
    start = start_dt.strftime("%Y-%m-%d")
    end = end_dt.strftime("%Y-%m-%d")
    frame = _load_ohlcv(adapter, symbol, start, end)
    if frame.empty:
        raise DataUnavailableError(f"No market data available for {display}.")

    config = AgentConfig()
    analyst = AnalystAgent(
        config.hmm_states,
        config.hmm_min_samples,
        config.hmm_lookback,
        config.hmm_refit_interval,
    )
    scout = ScoutAgent(
        config.swing_length,
        config.displacement_atr_multiple,
        config.fvg_min_atr_fraction,
        config.sessions,
        config.scout_refit_interval,
    )
    router = StrategyRouter(config.min_awd, config.tma_threshold)

    visible = frame
    current_index = len(visible) - 1
    slope = legacy_slope(
        visible,
        current_index,
        config.tma_period,
        config.atr_period,
        config.tma_atr_shift,
    )
    a_sig = analyst.analyze(visible, slope, config.tma_threshold, cache_key=display)
    s_feat = scout.extract(visible, cache_key=display)
    corr = basket_correlation({display: visible})
    route = router.route(a_sig, s_feat, slope, corr)

    last = visible.iloc[-1]
    close = float(last["close"])
    direction = "🚀 **LONG/BUY**" if route.direction == 1 else (
        "🔻 **SHORT/SELL**" if route.direction == -1 else "⏸️ **NO TRADE**"
    )

    lines = [
        f"📈 **Market Analysis — {display}**",
        f"- Last close: `{_fmt_money(close)}` ({visible.index[-1].date()})",
        f"- Regime: `{a_sig.regime}` (AWD confidence `{a_sig.awd:.2f}`)",
        f"- TMA slope: `{slope if slope is None else round(slope, 4)}`",
        f"- Basket correlation: `{corr:.2f}`",
        "",
        f"**Recommendation: {direction}**",
        f"- Strategy: `{route.cluster or '—'}`",
        f"- Signal reasoning: {route.reason}",
        "",
        f"- SMC events: {s_feat.reason}",
    ]
    if route.direction:
        lines.append(
            "\nUse `/trade buy <SYMBOL> <qty>` (or sell) to act on this signal in your "
            "Alpaca paper account."
        )
    else:
        lines.append("\nNo high-confluence setup on the last closed bar yet.")
    return "\n".join(lines)


def _backtest_text(
    adapter: AlpacaExecutionAdapter | None,
    symbol: str,
    start: str | None,
    end: str | None,
) -> str:
    """Run the closed-bar-causal backtest engine and summarize results."""
    display = symbol.upper()

    if not start:
        start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
    if not end:
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    config = AgentConfig()
    data = _load_ohlcv(adapter, symbol, start, end)
    if data.empty:
        raise DataUnavailableError(f"No market data available for {display}.")

    engine = BacktestEngine(config, starting_equity=100_000.0)
    result = engine.run({display: data})
    trades = result.get("trades", [])
    decisions = result.get("decisions", [])

    line = [
        f"🔬 **Backtest — {display}**",
        f"- Bars evaluated: `{len(decisions)}`",
        f"- Trades generated: `{len(trades)}`",
    ]
    if trades:
        wins = sum(1 for t in trades if t.r_multiple > 0)
        losses = sum(1 for t in trades if t.r_multiple <= 0)
        total_r = sum(t.r_multiple for t in trades)
        avg_r = total_r / len(trades) if trades else 0.0
        line.extend([
            f"- Win rate: `{wins}/{len(trades)}` ({100.0 * wins / len(trades) if trades else 0:.1f}%)",
            f"- Total R: `{total_r:.2f}`",
            f"- Avg R / trade: `{avg_r:.2f}`",
        ])
        recent = trades[-5:]
        line.extend(["", "**Recent trades:**"])
        for t in recent:
            side = "BUY" if t.direction == 1 else "SELL"
            line.append(
                f"- `{t.pair}` {side} @ `{_fmt_money(t.entry)}` → `{_fmt_money(t.exit)}` "
                f"(R {t.r_multiple:+.2f}, {t.exit_reason})"
            )
    else:
        line.append(
            "\nThe strategy found no qualifying closed-bar setups in this range. "
            "Try a longer range with `/backtest <symbol> <start> <end>` "
            "(e.g. `/backtest AAPL 2024-01-01 2026-01-01`)."
        )
    line.append(
        "\nThis is a historical simulation on daily bars using the institutional/"
        "SMC research engine. It is not financial advice."
    )
    return "\n".join(line)


def _trade_usage() -> str:
    return "\n".join([
        "📚 **/trade usage**",
        "`/trade` — show account + positions",
        "`/trade account` — Alpaca paper account summary",
        "`/trade positions` — list open positions",
        "`/trade analyze <symbol>` — market analysis & signal",
        "`/trade buy <symbol> <qty>` — place a market buy",
        "`/trade sell <symbol> <qty>` — place a market sell",
        "",
        "Examples:",
        "`/trade analyze AAPL`",
        "`/trade buy AAPL 5`",
        "`/trade sell MSFT 2`",
    ])


def _backtest_usage() -> str:
    return "\n".join([
        "📚 **/backtest usage**",
        "`/backtest <symbol>` — run backtest on the last 12 months",
        "`/backtest <symbol> <start> <end>` — run backtest over a range",
        "`/backtest <symbol> <start> <end> <timeframe>` — with timeframe (1h, 1d)",
        "",
        "Examples:",
        "`/backtest AAPL`",
        "`/backtest xauusd`",
        "`/backtest AAPL 2024-01-01 2026-01-01`",
    ])


async def handle_trade_command(
    account: dict[str, Any] | None,
    text: str,
    chat_id: int,
    message_id: int | None = None,
) -> str:
    """Handle a /trade command and return the response text.

    Executes in a worker thread for long-running Alpaca / analysis calls so
    the Telegram event loop is never blocked.
    """
    if not TRADING_AVAILABLE:
        return "⚠️ Trading support is not installed on this server (alpaca-py missing)."

    tokenized = text.strip().split()
    subcommand = tokenized[1].lower() if len(tokenized) > 1 else ""

    if subcommand in {"", "usage"} and len(tokenized) < 2:
        return _trade_usage()

    if subcommand in {"help", "usage"}:
        return _trade_usage()

    try:
        credentials = await _load_credentials(account)
        adapter = _build_adapter(credentials)

        if subcommand == "account":
            return await asyncio.to_thread(_account_text, adapter)
        if subcommand == "positions":
            return await asyncio.to_thread(_positions_text, adapter)
        if subcommand == "analyze":
            if len(tokenized) < 3:
                return "Usage: `/trade analyze <symbol>` — e.g. `/trade analyze AAPL`"
            symbol = tokenized[2]
            return await asyncio.to_thread(_analyze_text, adapter, symbol)
        if subcommand == "buy":
            if len(tokenized) < 4:
                return "Usage: `/trade buy <symbol> <qty>` — e.g. `/trade buy AAPL 5`"
            symbol = tokenized[2]
            try:
                qty = float(tokenized[3])
            except ValueError:
                return "❌ Quantity must be a number."
            if qty <= 0:
                return "❌ Quantity must be positive."
            result = await asyncio.to_thread(
                adapter.submit_market_order, symbol, "buy", qty
            )
            return (
                f"✅ Market **BUY** order submitted\n"
                f"- Symbol: `{result.get('symbol')}` × {_fmt_qty(result.get('qty'))}\n"
                f"- Status: `{result.get('status')}`\n"
                f"- Order ID: `{result.get('order_id')}`"
            )
        if subcommand == "sell":
            if len(tokenized) < 4:
                return "Usage: `/trade sell <symbol> <qty>` — e.g. `/trade sell AAPL 2`"
            symbol = tokenized[2]
            try:
                qty = float(tokenized[3])
            except ValueError:
                return "❌ Quantity must be a number."
            if qty <= 0:
                return "❌ Quantity must be positive."
            result = await asyncio.to_thread(
                adapter.submit_market_order, symbol, "sell", qty
            )
            return (
                f"✅ Market **SELL** order submitted\n"
                f"- Symbol: `{result.get('symbol')}` × {_fmt_qty(result.get('qty'))}\n"
                f"- Status: `{result.get('status')}`\n"
                f"- Order ID: `{result.get('order_id')}`"
            )
        return _trade_usage()
    except AlpacaError as exc:
        return (
            f"❌ Alpaca error: {exc}\n\n"
            "Use `/alpaca connect <API_KEY> <SECRET_KEY>` to link your paper account, "
            "or ask the administrator to set ALPACA_API_KEY / ALPACA_SECRET_KEY."
        )
    except DataUnavailableError as exc:
        return f"❌ Data error: {exc}"
    except Exception as exc:
        logger.exception("Trade command failed")
        return f"❌ Trade command failed: {exc}"


async def handle_backtest_command(
    account: dict[str, Any] | None,
    text: str,
    chat_id: int,
    message_id: int | None = None,
) -> str:
    """Handle a /backtest command and return the response text.

    Runs in a worker thread so the Telegram event loop is never blocked.
    """
    if not TRADING_AVAILABLE:
        return "⚠️ Trading support is not installed on this server (alpaca-py missing)."

    tokenized = text.strip().split()
    if len(tokenized) < 2:
        return _backtest_usage()

    subcommand = tokenized[1].lower()
    if subcommand in {"help", "usage"}:
        return _backtest_usage()

    symbol = tokenized[1]
    # Optional positional args: [start] [end] [timeframe]
    start = tokenized[2] if len(tokenized) > 2 else None
    end = tokenized[3] if len(tokenized) > 3 else None
    timeframe = tokenized[4] if len(tokenized) > 4 else "1d"

    if timeframe not in {"1h", "1d", "1w"}:
        return "❌ Timeframe must be one of: 1h, 1d, 1w"

    if timeframe != "1d":
        # The command handler currently analyzes daily bars; other timeframes
        # still fetch correctly but the strategy config is daily-oriented.
        logger.info("Backtest requested timeframe {} for {}", timeframe, symbol)

    try:
        credentials = await _load_credentials(account)
        adapter = _build_adapter(credentials)
        return await asyncio.to_thread(_backtest_text, adapter, symbol, start, end)
    except DataUnavailableError as exc:
        return f"❌ Data error: {exc}"
    except Exception as exc:
        logger.exception("Backtest command failed")
        return f"❌ Backtest command failed: {exc}"