"""Introspection of the Pine surface the engine supports.

Kept separate from the evaluator so tools can describe capabilities without
instantiating an evaluator (which needs market data).
"""

from __future__ import annotations

from typing import Any

# Mirrors of the tables installed by PineEvaluator._install_builtins.
TA_FUNCTIONS = (
    "sma", "ema", "rma", "wma", "hma", "vwma", "swma", "tr", "atr", "rsi",
    "macd", "bb", "bbw", "stoch", "cci", "mfi", "roc", "mom", "change",
    "highest", "lowest", "highestbars", "lowestbars", "sum", "median",
    "percentile_linear_interpolation", "linreg", "correlation", "barssince",
    "valuewhen", "crossover", "crossunder", "cross", "rising", "falling",
    "cum", "vwap", "stdev", "dev", "variance", "cmo",
)

MATH_FUNCTIONS = (
    "abs", "max", "min", "round", "floor", "ceil", "sqrt", "pow", "log",
    "log10", "avg", "sign", "exp", "isna", "na", "isfinite", "todegrees",
    "toradians", "pi", "e",
)

STRATEGY_MEMBERS = (
    "entry", "order", "close", "close_all", "exit", "long", "short",
    "equity", "position_size",
)

BARSTATE_MEMBERS = (
    "isfirst", "islast", "isnew", "isconfirmed", "isrealtime", "ishistory",
)

INPUT_MEMBERS = (
    "input", "int", "float", "bool", "string", "timeframe", "session",
    "source", "color",
)

SUPPORTED_SERIES = (
    "open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4", "hlcc4",
    "time", "time_close", "bar_index", "last_bar_index",
)

PLOT_CALLS = (
    "plot", "plotshape", "plotchar", "plotarrow", "plotcandle", "plotbar",
    "hline", "fill", "bgcolor", "barcolor", "alert", "alertcondition",
)

NOTES = {
    "request.security": "Only the current symbol/timeframe is loaded; higher-timeframe requests are not fetched.",
    "array": "array.*/matrix.*/map.* containers parse but are not evaluated.",
    "strategy.risk": "Risk rules (commission/slippage) are not modelled in the backtest.",
    "label": "label.*/line.*/box.*/table.* calls are accepted but not rendered.",
    "alert": "alert()/alertcondition() are recorded, not dispatched.",
    "varip": "varip behaves like var (closed-bar semantics).",
}


def describe_surface() -> dict[str, list[str]]:
    """Return the supported Pine names grouped by namespace."""
    return {
        "ta": sorted(TA_FUNCTIONS),
        "math": sorted(MATH_FUNCTIONS),
        "strategy": sorted(STRATEGY_MEMBERS),
        "barstate": sorted(BARSTATE_MEMBERS),
        "input": sorted(INPUT_MEMBERS),
        "series": sorted(SUPPORTED_SERIES),
        "plot": sorted(PLOT_CALLS),
        "sort": sorted(NOTES),
    }


def describe_notes() -> dict[str, str]:
    return dict(NOTES)


def capability_summary() -> dict[str, Any]:
    """Compact capability manifest for tool/agent consumption."""
    surface = describe_surface()
    return {
        "ta": len(surface["ta"]),
        "math": len(surface["math"]),
        "series": surface["series"],
        "plot_calls": surface["plot"],
        "unsupported": describe_notes(),
    }
