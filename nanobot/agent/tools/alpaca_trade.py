"""Alpaca trading tool for the nanobot agent.

Auto-discovered by ToolLoader. Provides analyze, backtest, buy, sell,
positions, and account actions via the Alpaca paper trading API.
Per-user credentials are resolved from Supabase; the server-level
environment keys are used as fallback.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["buy", "sell", "positions", "account", "close"],
                "description": "The trading action to perform: 'buy'/'sell' submit paper orders (use for 'buy/sell X', 'open a position'); 'positions' lists open positions (use for 'what am I holding', 'my positions'); 'account' shows paper account info (use for 'how much money do I have', 'account balance'); 'close' closes a position.",
            },
            "symbol": {
                "type": "string",
                "description": "Stock or FX symbol, e.g. AAPL, MSFT, TSLA. Use this for buy/sell/close.",
            },
            "qty": {
                "type": "number",
                "description": "Quantity to buy or sell.",
            },
            "side": {
                "type": "string",
                "enum": ["buy", "sell"],
                "description": "Order side (for buy/sell actions).",
            },
            "limit_price": {
                "type": "number",
                "description": "Limit price for limit orders.",
            },
        },
        "required": ["action"],
    }
)
class AlpacaTradeTool(Tool):
    """Trade stocks and FX via Alpaca paper trading, with strategy analysis."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "alpaca_trade"

    @property
    def description(self) -> str:
        return (
            "Trade US stocks via the Alpaca paper-trading account. Use this tool whenever the user "
            "asks about trading, positions, account balance, buying/selling stocks, or closing a "
            "position. Actions: 'buy'/'sell' submit paper orders; 'positions' lists open positions; "
            "'account' shows paper account info; 'close' closes a position. For market analysis or "
            "backtesting, run them as normal coding tasks in the novita_sandbox/VPS execution backend "
            "instead - do not use this tool for heavy compute. Paper trading only."
        )
    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    async def _get_credentials(self) -> dict[str, str] | None:
        """Resolve the *current user's* Alpaca credentials.

        Per-user isolation: when the request context identifies a Telegram
        sender, credentials are resolved strictly from that user's Supabase
        row (set via /alpaca connect). If the user has no stored credentials
        (never connected or disconnected), this returns None so the agent
        reports "not connected" — it does NOT fall back to shared server
        environment keys, otherwise per-user disconnect would have no effect
        and every user would see the same account.

        The server-level ALPACA_API_KEY / ALPACA_SECRET_KEY environment
        variables are only used when no per-user identity is present at all
        (e.g. non-Telegram or anonymous contexts).
        """
        user_id = self._current_telegram_user_id()
        if user_id:
            try:
                from nanobot.trading.alpaca_credentials import AlpacaCredentialStore

                store = AlpacaCredentialStore()
                if store.enabled:
                    # Return what belongs to *this* user (or None), never env.
                    return await store.get_credentials(user_id)
            except Exception as exc:
                logger.debug(f"Could not load per-user Alpaca credentials: {exc}")
                return None

        api_key = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        if api_key and secret_key:
            return {
                "api_key": api_key,
                "secret_key": secret_key,
                "base_url": os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            }
        return None

    @staticmethod
    def _current_telegram_user_id() -> int | None:
        """Return the authenticated Telegram sender id from the request context, if any."""
        try:
            from nanobot.agent.tools.context import current_request_context

            ctx = current_request_context()
            if ctx is None:
                return None
            metadata = getattr(ctx, "metadata", None) or {}
            sender = metadata.get("user_id") or metadata.get("telegram_user_id")
            if not sender and getattr(ctx, "attributes", None):
                sender = ctx.attributes.get("telegram_user_id")
            if not sender:
                return None
            return int(sender)
        except Exception:
            return None

    async def _get_adapter(self):
        creds = await self._get_credentials()
        if not creds:
            raise RuntimeError(
                "Your Alpaca account is not connected. Use /alpaca connect to link "
                "your own Alpaca paper trading account."
            )
        from nanobot.trading.alpaca_adapter import AlpacaCredentials, AlpacaExecutionAdapter

        return AlpacaExecutionAdapter(AlpacaCredentials(**creds))

    async def execute(self, **kwargs) -> Any:
        action = kwargs.get("action", "")
        try:
            if action == "account":
                return await self._account(kwargs)
            elif action == "positions":
                return await self._positions(kwargs)
            elif action in ("buy", "sell"):
                return await self._place_order(kwargs)
            elif action == "close":
                return await self._close_position(kwargs)
            else:
                return ToolResult.error(f"Unknown action: {action}")
        except Exception as exc:
            logger.exception("Alpaca trade tool error")
            return ToolResult.error(str(exc))

    async def _account(self, params: dict) -> ToolResult:
        adapter = await self._get_adapter()
        account = adapter.get_account()
        return ToolResult(
            f"Alpaca Paper Account\n"
            f"  Account ID: {account['account_id']}\n"
            f"  Status: {account['status']}\n"
            f"  Cash: ${account['cash']:,.2f}\n"
            f"  Portfolio Value: ${account['portfolio_value']:,.2f}\n"
            f"  Buying Power: ${account['buying_power']:,.2f}\n"
            f"  Equity: ${account['equity']:,.2f}"
        )

    async def _positions(self, params: dict) -> ToolResult:
        adapter = await self._get_adapter()
        positions = adapter.get_positions()
        if not positions:
            return ToolResult("No open positions.")
        lines = ["Open Positions:"]
        for pos in positions:
            lines.append(
                f"  {pos['symbol']}: {pos['qty']} shares ({pos['side']})\n"
                f"    Entry: ${pos['avg_entry_price']:.2f} | Current: ${pos['current_price']:.2f}\n"
                f"    P&L: ${pos['unrealized_pl']:.2f} ({pos['unrealized_plpc']*100:.2f}%)\n"
                f"    Value: ${pos['market_value']:,.2f}"
            )
        return ToolResult("\n".join(lines))

    async def _place_order(self, params: dict) -> ToolResult:
        adapter = await self._get_adapter()
        symbol = params.get("symbol", "")
        qty = params.get("qty")
        side = params.get("side", params.get("action", ""))
        limit_price = params.get("limit_price")
        if not symbol or not qty:
            return ToolResult.error("symbol and qty are required for buy/sell")
        if limit_price:
            result = adapter.submit_limit_order(symbol, side, float(qty), float(limit_price))
        else:
            result = adapter.submit_market_order(symbol, side, float(qty))
        return ToolResult(
            f"Order submitted\n"
            f"  Order ID: {result['order_id']}\n"
            f"  Status: {result['status']}\n"
            f"  Symbol: {result['symbol']}\n"
            f"  Side: {result['side']}\n"
            f"  Qty: {result['qty']}"
            + (f"\n  Limit: ${limit_price:.2f}" if limit_price else "")
        )

    async def _close_position(self, params: dict) -> ToolResult:
        adapter = await self._get_adapter()
        symbol = params.get("symbol", "")
        if not symbol:
            return ToolResult.error("symbol is required to close a position")
        result = adapter.close_position(symbol)
        return ToolResult(f"Position closed: {result['symbol']}")
