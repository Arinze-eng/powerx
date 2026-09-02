"""Real Alpaca paper-trading adapter built on alpaca-py.

Supports per-user credentials (stored encrypted in Supabase) and
a server-level default account from environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from loguru import logger

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    HAS_ALPACA = True
except ImportError:  # pragma: no cover
    HAS_ALPACA = False
    TradingClient = None


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str
    secret_key: str
    base_url: str = "https://paper-api.alpaca.markets"


class AlpacaError(RuntimeError):
    pass


class AlpacaExecutionAdapter:
    """Live adapter that submits orders to Alpaca paper trading."""

    def __init__(self, credentials: AlpacaCredentials | None = None):
        if not HAS_ALPACA:
            raise AlpacaError(
                "alpaca-py is not installed. Run: pip install alpaca-py"
            )
        if credentials is None:
            credentials = AlpacaCredentials(
                api_key=os.getenv("ALPACA_API_KEY", ""),
                secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
                base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            )
        if not credentials.api_key or not credentials.secret_key:
            raise AlpacaError(
                "Alpaca API credentials are not configured. Set ALPACA_API_KEY "
                "and ALPACA_SECRET_KEY environment variables, or connect your "
                "Alpaca account via /alpaca connect."
            )
        self.credentials = credentials
        self._trading_client = TradingClient(
            api_key=credentials.api_key,
            secret_key=credentials.secret_key,
            paper=True,
            url_override=credentials.base_url,
        )
        self._data_client = StockHistoricalDataClient(
            api_key=credentials.api_key,
            secret_key=credentials.secret_key,
        )

    def get_account(self) -> dict[str, Any]:
        account = self._trading_client.get_account()
        return {
            "account_id": getattr(account, "id", ""),
            "status": getattr(account, "status", ""),
            "cash": float(getattr(account, "cash", 0) or 0),
            "portfolio_value": float(getattr(account, "portfolio_value", 0) or 0),
            "buying_power": float(getattr(account, "buying_power", 0) or 0),
            "equity": float(getattr(account, "equity", 0) or 0),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        positions = self._trading_client.get_all_positions()
        result = []
        for pos in positions:
            result.append({
                "symbol": getattr(pos, "symbol", ""),
                "qty": float(getattr(pos, "qty", 0) or 0),
                "side": getattr(pos, "side", ""),
                "market_value": float(getattr(pos, "market_value", 0) or 0),
                "unrealized_pl": float(getattr(pos, "unrealized_pl", 0) or 0),
                "unrealized_plpc": float(getattr(pos, "unrealized_plpc", 0) or 0),
                "avg_entry_price": float(getattr(pos, "avg_entry_price", 0) or 0),
                "current_price": float(getattr(pos, "current_price", 0) or 0),
            })
        return result

    def submit_market_order(self, symbol: str, side: str, qty: float) -> dict[str, Any]:
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        order = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
        )
        result = self._trading_client.submit_order(order)
        return {
            "order_id": getattr(result, "id", ""),
            "status": getattr(result, "status", ""),
            "symbol": symbol.upper(),
            "side": side.lower(),
            "qty": qty,
        }

    def submit_limit_order(self, symbol: str, side: str, qty: float, limit_price: float) -> dict[str, Any]:
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        order = LimitOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        )
        result = self._trading_client.submit_order(order)
        return {
            "order_id": getattr(result, "id", ""),
            "status": getattr(result, "status", ""),
            "symbol": symbol.upper(),
            "side": side.lower(),
            "qty": qty,
            "limit_price": limit_price,
        }

    def close_position(self, symbol: str) -> dict[str, Any]:
        self._trading_client.close_position(symbol.upper())
        return {"symbol": symbol.upper(), "status": "closed"}

    def get_bars(self, symbol: str, start: str, end: str, timeframe: str = "1Hour") -> "pd.DataFrame":
        import pandas as pd
        tf_map = {
            "1Min": TimeFrame.Minute,
            "1Hour": TimeFrame.Hour,
            "1Day": TimeFrame.Day,
        }
        tf = tf_map.get(timeframe, TimeFrame.Hour)
        request = StockBarsRequest(
            symbol_or_symbols=symbol.upper(),
            start=pd.Timestamp(start, tz="UTC"),
            end=pd.Timestamp(end, tz="UTC"),
            timeframe=tf,
        )
        bars = self._data_client.get_stock_bars(request)
        df = bars.df if hasattr(bars, "df") else pd.DataFrame()
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index(level=0, drop=True)
        df.index = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
        return df
