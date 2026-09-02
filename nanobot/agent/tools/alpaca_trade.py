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
                "enum": ["analyze", "backtest", "buy", "sell", "positions", "account", "close"],
                "description": "The trading action to perform: 'analyze' runs strategy analysis on a symbol (use for 'what do you think about X', 'analyze X', technical signals); 'backtest' runs a historical backtest; 'buy'/'sell' submit paper orders (use for 'buy/sell X', 'open a position'); 'positions' lists open positions (use for 'what am I holding', 'my positions'); 'account' shows paper account info (use for 'how much money do I have', 'account balance'); 'close' closes a position.",
            },
            "symbol": {
                "type": "string",
                "description": "Stock or FX symbol, e.g. AAPL, MSFT, TSLA. Use this for analyze/backtest/buy/sell/close.",
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
            "pairs": {
                "type": "string",
                "description": "Comma-separated FX pairs for backtest/analyze, e.g. EURGBP,EURCAD.",
            },
            "start": {
                "type": "string",
                "description": "Start date (YYYY-MM-DD) for backtest or bar data.",
            },
            "end": {
                "type": "string",
                "description": "End date (YYYY-MM-DD) for backtest or bar data.",
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
            "asks about trading, positions, account balance, buying/selling stocks, market analysis, "
            "or technical signals. Actions: "
            "'analyze' runs the five-cluster strategy engine on a symbol; "
            "'backtest' runs a historical backtest; 'buy'/'sell' submit paper orders; "
            "'positions' lists open positions; 'account' shows paper account info; "
            "'close' closes a position."
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
            elif action == "analyze":
                return await self._analyze(kwargs)
            elif action == "backtest":
                return await self._backtest(kwargs)
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

    async def _analyze(self, params: dict) -> ToolResult:
        symbol = params.get("symbol", "") or params.get("pairs", "")
        if not symbol:
            return ToolResult.error("symbol or pairs is required for analysis")
        pairs = [p.strip().upper() for p in symbol.split(",") if p.strip()]
        from nanobot.trading.config import AgentConfig
        from nanobot.trading.data_loader import load_market_data
        from nanobot.trading.backtest_engine import BacktestEngine

        config = AgentConfig(pairs=tuple(pairs))
        try:
            market_data = load_market_data(
                pairs,
                config.start,
                config.end,
                config.timeframe,
                str(config.data_dir) if config.data_dir else None,
            )
        except Exception as exc:
            return ToolResult.error(f"Data loading failed: {exc}")
        engine = BacktestEngine(config)
        result = engine.run(dict(market_data))
        last_decisions = [d for d in result["decisions"] if d.get("action") in ("signal", "no_trade")]
        if not last_decisions:
            return ToolResult("No analysis generated — insufficient data.")
        latest = last_decisions[-1]
        lines = [
            f"Strategy Analysis for {symbol}",
            f"  Regime: {latest.get('regime', 'N/A')}",
            f"  AWD Confidence: {latest.get('awd', 0):.2f}",
            f"  TMA Slope: {latest.get('tma_slope', 'N/A')}",
            f"  Basket Correlation: {latest.get('basket_correlation', 0):.2f}",
            f"  Cluster: {latest.get('cluster', 'None')}",
            f"  Direction: {latest.get('direction', 0)}",
            f"  Killzone: {'Yes' if latest.get('killzone_active') else 'No'}",
            f"  Reason: {latest.get('reason', '')}",
        ]
        return ToolResult("\n".join(lines))

    async def _backtest(self, params: dict) -> ToolResult:
        pairs_str = params.get("pairs", "") or params.get("symbol", "")
        if not pairs_str:
            return ToolResult.error("pairs is required for backtest")
        pairs = [p.strip().upper() for p in pairs_str.split(",") if p.strip()]
        start = params.get("start")
        end = params.get("end")
        from nanobot.trading.config import AgentConfig
        from nanobot.trading.data_loader import load_market_data
        from nanobot.trading.backtest_engine import BacktestEngine

        config = AgentConfig(pairs=tuple(pairs), start=start, end=end)
        try:
            market_data = load_market_data(
                pairs, start, end, config.timeframe, str(config.data_dir) if config.data_dir else None
            )
        except Exception as exc:
            return ToolResult.error(f"Data loading failed: {exc}")
        engine = BacktestEngine(config)
        result = engine.run(dict(market_data))
        summary = engine.write_results(result)
        combined = summary.get("combined", {})
        lines = [
            f"Backtest Results for {pairs_str}",
            f"  Trades: {combined.get('trades', 0)}",
            f"  Wins: {combined.get('wins', 0)} | Losses: {combined.get('losses', 0)}",
            f"  Win Rate: {combined.get('win_rate', 0)*100:.1f}%",
            f"  Total R: {combined.get('total_r', 0):.2f}R",
            f"  Avg R/Trade: {combined.get('average_r_per_trade', 0):.2f}R",
            f"  Max Drawdown: {combined.get('max_drawdown_pct', 0):.1f}%",
            f"  Sharpe: {combined.get('sharpe_trade_r', 0):.2f}",
        ]
        if "in_sample" in summary:
            ins = summary["in_sample"]
            lines.append("  --- In-Sample ---")
            lines.append(
                f"  Trades: {ins.get('trades', 0)} | Win Rate: {ins.get('win_rate', 0)*100:.1f}% | Total R: {ins.get('total_r', 0):.2f}R"
            )
        if "out_of_sample" in summary:
            oos = summary["out_of_sample"]
            lines.append("  --- Out-of-Sample ---")
            lines.append(
                f"  Trades: {oos.get('trades', 0)} | Win Rate: {oos.get('win_rate', 0)*100:.1f}% | Total R: {oos.get('total_r', 0):.2f}R"
            )
        return ToolResult("\n".join(lines))
