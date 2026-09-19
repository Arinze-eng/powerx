"""Pine Script engine: parse, evaluate, backtest, chart — all self-hosted.

Typical flow::

    from nanobot.trading.pine import run_pine_script, PineChartRequest

    outcome = run_pine_script(source, symbol="BTC-USD", timeframe="1h")
    print(outcome.summary_text())
    outcome.render_png("/workspace/charts/btc.png")

No TradingView. Data comes from Yahoo (or a local CSV), charts from mplfinance.
"""

from nanobot.trading.pine.chart import ChartSpec, render_interactive_html, render_mplfinance
from nanobot.trading.pine.evaluator import PineEvaluator, PineResult, PineRuntimeError, evaluate
from nanobot.trading.pine.linter import Diagnostic, LintReport, autofix, lint
from nanobot.trading.pine.parser import PineSyntaxError, Program, parse
from nanobot.trading.pine.precursor import (
    PrecursorSignal,
    detect_precursor,
    scan_pairs,
    timeframe_minutes,
)
from nanobot.trading.pine.runner import (
    BacktestStats,
    PineRun,
    TradeRecord,
    load_ohlcv,
    resolve_symbol,
    run_pine_script,
    save_script,
    self_check,
    simulate_trades,
)

__all__ = [
    "BacktestStats",
    "ChartSpec",
    "Diagnostic",
    "LintReport",
    "PineEvaluator",
    "PineResult",
    "PineRun",
    "PineRuntimeError",
    "PineSyntaxError",
    "PrecursorSignal",
    "Program",
    "TradeRecord",
    "autofix",
    "detect_precursor",
    "evaluate",
    "lint",
    "load_ohlcv",
    "parse",
    "render_interactive_html",
    "render_mplfinance",
    "resolve_symbol",
    "run_pine_script",
    "save_script",
    "scan_pairs",
    "self_check",
    "simulate_trades",
    "timeframe_minutes",
]
