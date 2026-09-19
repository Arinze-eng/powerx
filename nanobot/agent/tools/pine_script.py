"""Agent tool: write, fix, test and run Pine Script without TradingView.

Actions
-------
``lint``      static analysis, returns diagnostics and an auto-fixed script
``run``       execute against real OHLCV: plots, signals, backtest, precursor
``chart``     render an mplfinance PNG and/or an interactive HTML chart
``self_check``run the engine against its own output (test-before-you-answer)
``indicators``list the supported ``ta.*`` / ``math.*`` surface
``template``  emit a known-good starting script

The tool deliberately keeps compute inside the request so the agent can verify
its own answer before replying.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["lint", "run", "chart", "self_check", "indicators", "template"],
            "description": (
                "lint: check/fix a Pine script and get diagnostics. "
                "run: execute the script on real market data and report signals + backtest. "
                "chart: render the script to an image (PNG) and/or interactive HTML. "
                "self_check: run lint+parse+execute+backtest and return a pass/fail verdict. "
                "indicators: list supported ta.*/math.* functions. "
                "template: return a known-good Pine starter script."
            ),
        },
        "script": {
            "type": "string",
            "description": (
                "The Pine Script source. Required for lint/run/chart/self_check. "
                "Paste the user's script verbatim; the engine auto-corrects common "
                "v4->v5 mistakes."
            ),
        },
        "symbol": {
            "type": "string",
            "description": "Ticker, e.g. BTC-USD, AAPL, EURUSD, XAUUSD, SPX. Default BTC-USD.",
        },
        "timeframe": {
            "type": "string",
            "description": "Bar interval: 1m,5m,15m,30m,1h,4h,1d,1wk. Default 1d.",
        },
        "bars": {
            "type": "integer",
            "description": "How many bars of history to load (default 500).",
        },
        "preview_bars": {
            "type": "integer",
            "description": (
                "Precursor horizon: warn when a crossover is projected within this many "
                "bars (default 5). Use 5 on a 1m chart for '5 minutes before it plays'."
            ),
        },
        "chart_format": {
            "type": "string",
            "enum": ["png", "html", "both"],
            "description": "For action=chart: PNG image, interactive HTML, or both. Default png.",
        },
        "output_dir": {
            "type": "string",
            "description": "Directory for chart files (default: charts under the workspace).",
        },
        "theme": {
            "type": "string",
            "enum": ["dark", "light"],
            "description": "Chart theme. Default dark.",
        },
        "last_bars": {
            "type": "integer",
            "description": "How many of the most recent bars to draw (default 250).",
        },
    },
    "required": ["action"],
}


@tool_parameters(_SCHEMA)
class PineScriptTool(Tool):
    """Write, fix, test and run Pine Script; render charts without TradingView."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "pine_script"

    @property
    def description(self) -> str:
        return (
            "Work with Pine Script (TradingView's language) WITHOUT TradingView. "
            "Use this whenever the user gives you a Pine/TradingView strategy or indicator "
            "script, asks you to write/fix/explain one, or asks for a chart of it. "
            "Actions: 'lint' checks and auto-fixes syntax (v4->v5, ta.* names, indentation); "
            "'run' executes the script on real OHLCV data and reports plots, entry/exit signals, "
            "a backtest, and a PRECURSOR signal that fires 1-5 bars BEFORE a crossover; "
            "'chart' renders a clear candlestick image with indicators + signal markers; "
            "'self_check' verifies the whole pipeline and returns pass/fail. "
            "Always 'self_check' or 'run' after writing or fixing a script, then 'chart', "
            "so the user sees a verified result."
        )

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    # -- workspace helpers ------------------------------------------------
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

    def _resolve_dir(self, output_dir: str | None) -> Path:
        base = self._workspace()
        target = Path(output_dir) if output_dir else base / "charts"
        if not target.is_absolute():
            target = base / target
        target.mkdir(parents=True, exist_ok=True)
        return target

    # -- execute ----------------------------------------------------------
    async def execute(self, **kwargs: Any) -> Any:
        action = (kwargs.get("action") or "").strip()
        try:
            if action == "lint":
                return self._lint(kwargs)
            if action == "run":
                return self._run(kwargs)
            if action == "chart":
                return self._chart(kwargs)
            if action == "self_check":
                return self._self_check(kwargs)
            if action == "indicators":
                return self._indicators()
            if action == "template":
                return ToolResult(_TEMPLATE)
            return ToolResult.error(f"unknown action: {action}")
        except ImportError as exc:
            return ToolResult.error(
                f"Pine engine dependencies missing: {exc}. "
                "Ensure pandas/numpy/matplotlib/mplfinance/yfinance are installed."
            )
        except Exception as exc:
            logger.exception("pine_script tool failed")
            return ToolResult.error(f"{type(exc).__name__}: {exc}")

    def _require_script(self, kwargs: dict[str, Any]) -> str:
        script = kwargs.get("script") or ""
        if not script.strip():
            raise ValueError("'script' is required for this action")
        return script

    def _lint(self, kwargs: dict[str, Any]) -> ToolResult:
        from nanobot.trading.pine import lint

        script = self._require_script(kwargs)
        report = lint(script, autofix_enabled=True)
        lines = [
            f"Pine lint: {'PASS' if report.ok else 'FAIL'} ({report.summary()})",
            f"detected version: {report.version or 'none'}",
        ]
        for diag in report.diagnostics:
            marker = {"error": "x", "warning": "!", "info": "-"}.get(diag.severity, "-")
            pos = f"L{diag.line} " if diag.line else ""
            lines.append(f"  {marker} {pos}{diag.message} [{diag.code}]")
        if report.fixed_source and report.fixed_source != script:
            lines.append("")
            lines.append("--- corrected script ---")
            lines.append(report.fixed_source)
        return ToolResult("\n".join(lines))

    def _run(self, kwargs: dict[str, Any]) -> ToolResult:
        from nanobot.trading.pine import run_pine_script

        script = self._require_script(kwargs)
        run = run_pine_script(
            script,
            symbol=kwargs.get("symbol") or "BTC-USD",
            timeframe=kwargs.get("timeframe") or "1d",
            bars=int(kwargs.get("bars") or 500),
            autofix=True,
            backtest=True,
            preview_bars=int(kwargs.get("preview_bars") or 5),
        )
        text = run.summary_text()
        if not run.ok:
            return ToolResult.error(text)
        return ToolResult(text)

    def _chart(self, kwargs: dict[str, Any]) -> ToolResult:
        from nanobot.trading.pine import run_pine_script

        script = self._require_script(kwargs)
        fmt = (kwargs.get("chart_format") or "png").lower()
        theme = kwargs.get("theme") or "dark"
        last_bars = int(kwargs.get("last_bars") or 250)
        symbol = (kwargs.get("symbol") or "BTC-USD").upper()
        timeframe = kwargs.get("timeframe") or "1d"

        run = run_pine_script(
            script,
            symbol=symbol,
            timeframe=timeframe,
            bars=int(kwargs.get("bars") or 500),
            autofix=True,
            backtest=True,
            preview_bars=int(kwargs.get("preview_bars") or 5),
        )
        if not run.ok:
            return ToolResult.error(run.summary_text())

        out_dir = self._resolve_dir(kwargs.get("output_dir"))
        slug = f"{symbol.replace('/', '-').replace('=', '')}_{timeframe}".lower()
        written: list[str] = []

        if fmt in ("png", "both"):
            path = run.render_png(out_dir / f"{slug}_pine.png", last_bars=last_bars, theme=theme)
            written.append(str(path))
        if fmt in ("html", "both"):
            path = run.render_html(out_dir / f"{slug}_pine.html", last_bars=max(last_bars, 300), theme=theme)
            written.append(str(path))

        lines = [run.summary_text(), ""]
        lines.append("--- chart files ---")
        for path in written:
            lines.append(f"  {path}")
        if run.linter_note():
            lines.append(f"  note: {run.linter_note()}")
        return ToolResult("\n".join(lines))

    def _self_check(self, kwargs: dict[str, Any]) -> ToolResult:
        from nanobot.trading.pine import self_check

        script = self._require_script(kwargs)
        outcome = self_check(
            script,
            symbol=kwargs.get("symbol") or "BTC-USD",
            timeframe=kwargs.get("timeframe") or "1d",
            bars=int(kwargs.get("bars") or 400),
        )
        lines = [f"Self-check verdict: {outcome['verdict'].upper()}"]
        for check in outcome["checks"]:
            mark = "PASS" if check["passed"] else "FAIL"
            lines.append(f"  [{mark}] {check['check']}: {check['detail']}")
        lines.append("")
        lines.append(outcome["summary"])
        if outcome["verdict"] != "pass":
            lines.append("")
            lines.append(
                "Do not tell the user this script works until every check passes."
            )
        return ToolResult("\n".join(lines))

    def _indicators(self) -> ToolResult:
        from nanobot.trading.pine.registry import describe_surface

        surface = describe_surface()
        lines = ["Supported Pine surface:"]
        for ns in ("ta", "math", "strategy", "barstate", "input", "color", "shape", "sort"):
            members = surface.get(ns)
            if members:
                lines.append(f"  {ns}: {', '.join(members)}")
        lines.append("")
        lines.append(
            "Everything else parses; unsupported calls are reported as notes rather "
            "than failing the run."
        )
        return ToolResult("\n".join(lines))

_TEMPLATE = """//@version=5
// Known-good starting point: EMA cross with RSI filter.
indicator("EMA Cross + RSI", overlay=true)

fastLen = input.int(9,  "Fast EMA")
slowLen = input.int(21, "Slow EMA")
rsiLen  = input.int(14, "RSI Length")

fast = ta.ema(close, fastLen)
slow = ta.ema(close, slowLen)
rsi  = ta.rsi(close, rsiLen)

plot(fast, title="EMA fast", color=color.yellow)
plot(slow, title="EMA slow", color=color.blue)

longCond  = ta.crossover(fast, slow) and rsi > 50
shortCond = ta.crossunder(fast, slow) and rsi < 50

plotshape(longCond,  title="Long",  style=shape.triangleup,   location=location.belowbar, color=color.green)
plotshape(shortCond, title="Short", style=shape.triangledown, location=location.abovebar, color=color.red)

if longCond
    strategy.entry("Long", strategy.long)

if shortCond
    strategy.entry("Short", strategy.short)
"""


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)
