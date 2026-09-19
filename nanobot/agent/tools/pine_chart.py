"""Agent tool: draw a market chart directly, without needing a Pine script.

Use when the user asks for "a chart of X with indicator Y" rather than handing
over a script. Renders with mplfinance (PNG) and/or an interactive HTML chart.
No TradingView is involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext

_SUPPORTED = (
    "sma", "ema", "wma", "hma", "vwma", "rsi", "macd", "bb", "atr", "stoch",
    "cci", "mfi", "vwap", "supertrend", "donchian", "keltner", "obv", "adx",
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": "Ticker: BTC-USD, AAPL, EURUSD, XAUUSD, SPX, ... Default BTC-USD.",
        },
        "timeframe": {
            "type": "string",
            "description": "Bar interval: 1m,5m,15m,30m,1h,4h,1d,1wk. Default 1d.",
        },
        "indicators": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Indicators to overlay, as name or name:param. Examples: "
                "['ema:9','ema:21','bb:20:2'] for price panel, ['rsi:14','macd'] for sub-panels. "
                "Supported: " + ", ".join(_SUPPORTED)
            ),
        },
        "last_bars": {
            "type": "integer",
            "description": "How many recent bars to draw (default 200).",
        },
        "bars": {
            "type": "integer",
            "description": "Bars of history to load for computation (default 500).",
        },
        "chart_format": {
            "type": "string",
            "enum": ["png", "html", "both"],
            "description": "Output format. Default png.",
        },
        "theme": {
            "type": "string",
            "enum": ["dark", "light"],
            "description": "Chart theme. Default dark.",
        },
        "output_dir": {
            "type": "string",
            "description": "Where to write chart files. Default: charts/ in the workspace.",
        },
    },
    "required": ["symbol"],
}


@tool_parameters(_SCHEMA)
class PineChartTool(Tool):
    """Draw a candlestick chart with indicators (mplfinance + interactive HTML)."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "pine_chart"

    @property
    def description(self) -> str:
        return (
            "Draw a clear market chart for a symbol with indicators, WITHOUT TradingView. "
            "Use this when the user asks to 'show me a chart', 'plot X with RSI/EMA', or wants "
            "a visual of a market rather than a Pine script. Price-scale indicators (sma, ema, "
            "bb, vwap) overlay the candles; oscillators (rsi, macd, stoch, cci, mfi, atr) get "
            "their own panel; volume gets its own panel. Returns PNG and/or interactive HTML "
            "file paths. Prefer chart_format='both' when the user will want to zoom."
        )

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    def _workspace(self) -> Path:
        try:
            from nanobot.agent.tools.context import current_request_context

            ctx = current_request_context()
            if ctx is not None:
                for attr in ("workspace", "workspace_dir", "cwd"):
                    value = getattr(ctx, attr, None)
                    if value:
                        return Path(value)
                metadata = getattr(ctx, "metadata", None) or {}
                for key in ("workspace", "workspace_dir", "cwd"):
                    if metadata.get(key):
                        return Path(metadata[key])
        except Exception:
            pass
        return Path.cwd()

    async def execute(self, **kwargs: Any) -> Any:
        try:
            return self._render(kwargs)
        except Exception as exc:
            logger.exception("pine_chart tool failed")
            return ToolResult.error(f"{type(exc).__name__}: {exc}")

    def _render(self, kwargs: dict[str, Any]) -> ToolResult:
        import pandas as pd

        from nanobot.trading.pine import indicators as ind
        from nanobot.trading.pine.chart import ChartSpec, render_interactive_html, render_mplfinance
        from nanobot.trading.pine.runner import load_ohlcv

        symbol = (kwargs.get("symbol") or "BTC-USD").strip().upper()
        timeframe = kwargs.get("timeframe") or "1d"
        bars = int(kwargs.get("bars") or 500)
        last_bars = int(kwargs.get("last_bars") or 200)
        fmt = (kwargs.get("chart_format") or "png").lower()
        theme = kwargs.get("theme") or "dark"
        specs = kwargs.get("indicators") or []

        data, display = load_ohlcv(symbol, timeframe, bars=bars)
        if data is None or data.empty:
            return ToolResult.error(f"no data for {symbol} on {timeframe}")

        overlays: dict[str, pd.Series] = {}
        panels: dict[str, pd.Series] = {}
        warnings: list[str] = []

        for raw in specs:
            name, params = self._parse_spec(raw)
            try:
                series, panel = self._compute(name, params, data)
            except Exception as exc:
                warnings.append(f"{raw}: {exc}")
                continue
            if series is None:
                warnings.append(f"{raw}: unsupported indicator")
                continue
            if isinstance(series, dict):
                target = panels if panel else overlays
                for label, value in series.items():
                    target[label] = value
            else:
                (panels if panel else overlays)[str(raw)] = series

        if not specs:
            overlays["EMA 9"] = ind.ema(data["close"], 9)
            overlays["EMA 21"] = ind.ema(data["close"], 21)
            panels["RSI 14"] = ind.rsi(data["close"], 14)

        out_dir = Path(kwargs.get("output_dir") or (self._workspace() / "charts"))
        if not out_dir.is_absolute():
            out_dir = self._workspace() / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        spec = ChartSpec(
            symbol=display,
            timeframe=timeframe,
            title=", ".join(specs) if specs else "EMA 9/21 + RSI",
            theme=theme,
            last_bars=last_bars,
            overlay_indicators=overlays,
            panel_indicators=panels,
        )

        slug = f"{display.replace('/', '-').replace('=', '')}_{timeframe}".lower()
        written: list[str] = []
        if fmt in ("png", "both"):
            written.append(str(render_mplfinance(data, spec, out_dir / f"{slug}_chart.png")))
        if fmt in ("html", "both"):
            written.append(
                str(render_interactive_html(data, spec, out_dir / f"{slug}_chart.html"))
            )

        lines = [
            f"Chart: {display} · {timeframe} · {len(data)} bars loaded",
            f"  overlay: {', '.join(overlays) or 'none'}",
            f"  panels : {', '.join(panels) or 'none'}",
        ]
        if warnings:
            lines.append(f"  warnings: {'; '.join(warnings)}")
        lines.append("")
        lines.append("--- files ---")
        for path in written:
            lines.append(f"  {path}")
        return ToolResult("\n".join(lines))

    @staticmethod
    def _parse_spec(raw: str) -> tuple[str, list[str]]:
        parts = [p.strip() for p in str(raw).replace(":", " ").split()]
        return (parts[0].lower() if parts else "", parts[1:])

    def _compute(self, name: str, params: list[str], data: Any):
        from nanobot.trading.pine import indicators as ind

        def _int(idx: int, default: int) -> int:
            try:
                return int(float(params[idx]))
            except (IndexError, ValueError):
                return default

        close = data["close"]
        high, low, volume = data["high"], data["low"], data.get("volume")

        if name in {"sma", "ema", "wma", "hma", "vwma"}:
            period = _int(0, 20)
            fn = {"sma": ind.sma, "ema": ind.ema, "wma": ind.wma, "hma": ind.hma, "vwma": ind.vwma}[name]
            return fn(close, period), False
        if name == "bb":
            period = _int(0, 20)
            mult = float(params[1]) if len(params) > 1 else 2.0
            basis, upper, lower = ind.bb(close, period, mult)
            return {"BB basis": basis, "BB upper": upper, "BB lower": lower}, False
        if name == "vwap":
            return ind.vwap(high, low, close, volume), False
        if name == "donchian":
            period = _int(0, 20)
            return {"Donchian hi": ind.highest(high, period), "Donchian lo": ind.lowest(low, period)}, False
        if name == "keltner":
            period = _int(0, 20)
            basis = ind.ema(close, period)
            atr = ind.atr(high, low, close, period)
            return {"KC basis": basis, "KC upper": basis + 2 * atr, "KC lower": basis - 2 * atr}, False
        if name == "supertrend":
            period = _int(0, 10)
            mult = float(params[1]) if len(params) > 1 else 3.0
            atr = ind.atr(high, low, close, period)
            hl2 = (high + low) / 2
            upper = hl2 + mult * atr
            lower = hl2 - mult * atr
            return {"Supertrend upper": upper, "Supertrend lower": lower}, False
        if name == "rsi":
            return ind.rsi(close, _int(0, 14)), True
        if name == "macd":
            line, sig, hist = ind.macd(close, _int(0, 12), _int(1, 26), _int(2, 9))
            return {"MACD": line, "MACD signal": sig, "MACD hist": hist}, True
        if name == "atr":
            return ind.atr(high, low, close, _int(0, 14)), True
        if name in {"stoch", "stochastic"}:
            return ind.stoch(close, high, low, _int(0, 14)), True
        if name == "cci":
            return ind.cci(high, low, close, _int(0, 20)), True
        if name == "mfi":
            return ind.mfi(high, low, close, volume, _int(0, 14)), True
        if name == "obv":
            vol = volume if volume is not None else close * 0
            direction = close.diff().apply(lambda v: 1 if v > 0 else (-1 if v < 0 else 0))
            return (vol * direction).cumsum(), True
        if name == "adx":
            period = _int(0, 14)
            up = high.diff()
            down = -low.diff()
            plus_dm = up.where((up > down) & (up > 0), 0.0)
            minus_dm = down.where((down > up) & (down > 0), 0.0)
            tr = ind.true_range(high, low, close)
            atr = ind.rma(tr, period)
            plus_di = 100 * ind.rma(plus_dm, period) / atr.replace(0, float("nan"))
            minus_di = 100 * ind.rma(minus_dm, period) / atr.replace(0, float("nan"))
            dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
            return {"ADX": ind.rma(dx, period), "+DI": plus_di, "-DI": minus_di}, True
        raise ValueError(f"unsupported indicator '{name}'")
