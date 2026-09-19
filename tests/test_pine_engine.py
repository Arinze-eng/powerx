"""Tests for the Pine Script engine (parser, indicators, evaluator, precursor, charts).

These are offline: all market data is synthetic, so the suite runs in CI without
network access.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nanobot.trading.pine import indicators as ind
from nanobot.trading.pine.chart import ChartSpec, render_mplfinance, split_plot_series
from nanobot.trading.pine.evaluator import PineRuntimeError, evaluate
from nanobot.trading.pine.linter import lint
from nanobot.trading.pine.parser import PineSyntaxError, parse
from nanobot.trading.pine.precursor import detect_precursor, timeframe_minutes
from nanobot.trading.pine.runner import simulate_trades

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def make_data(n: int = 400, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0.02, 1.0, n))
    open_ = close + rng.normal(0, 0.4, n)
    high = np.maximum(close, open_) + rng.uniform(0.05, 1.2, n)
    low = np.minimum(close, open_) - rng.uniform(0.05, 1.2, n)
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1e5, 9e5, n),
        },
        index=idx,
    )


@pytest.fixture()
def data() -> pd.DataFrame:
    return make_data()


SIMPLE = """//@version=5
indicator("T", overlay=true)
f = ta.ema(close, 9)
s = ta.ema(close, 21)
plot(f, title="EMA fast")
plot(s, title="EMA slow")
plot(ta.rsi(close, 14), title="RSI")
"""


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------


class TestIndicators:
    def test_sma_matches_rolling_mean(self, data):
        out = ind.sma(data["close"], 10)
        expected = data["close"].rolling(10).mean()
        pd.testing.assert_series_equal(out.dropna(), expected.dropna())

    def test_ema_matches_pandas_ewm(self, data):
        out = ind.ema(data["close"], 12)
        expected = data["close"].ewm(span=12, adjust=False, min_periods=12).mean()
        pd.testing.assert_series_equal(out.dropna(), expected.dropna())

    def test_rsi_bounds(self, data):
        out = ind.rsi(data["close"], 14).dropna()
        assert not out.empty
        assert out.min() >= 0 and out.max() <= 100

    def test_rsi_all_up_is_100(self):
        series = pd.Series(np.arange(1, 40, dtype="float64"))
        assert float(ind.rsi(series, 14).dropna().iloc[-1]) == pytest.approx(100.0)

    def test_atr_positive(self, data):
        out = ind.atr(data["high"], data["low"], data["close"], 14).dropna()
        assert (out > 0).all()

    def test_macd_hist_is_line_minus_signal(self, data):
        line, sig, hist = ind.macd(data["close"])
        pd.testing.assert_series_equal((line - sig).dropna(), hist.dropna())

    def test_crossover_detects_cross(self):
        a = pd.Series([1.0, 1.0, 2.0, 3.0])
        b = pd.Series([2.0, 2.0, 2.0, 2.0])
        assert list(ind.crossover(a, b)) == [False, False, False, True]
        assert list(ind.crossunder(a, b)) == [False, False, False, False]

    def test_bb_ordering(self, data):
        basis, upper, lower = ind.bb(data["close"], 20, 2.0)
        valid = basis.notna()
        assert (upper[valid] >= basis[valid]).all()
        assert (lower[valid] <= basis[valid]).all()

    def test_barssince(self):
        cond = pd.Series([False, False, True, False, False, True])
        out = ind.barssince(cond)
        assert out.iloc[2] == 0
        assert out.iloc[4] == 2
        assert out.iloc[5] == 0

    def test_valuewhen(self):
        cond = pd.Series([True, False, True, False])
        src = pd.Series([10.0, 20.0, 30.0, 40.0])
        out = ind.valuewhen(cond, src, 0)
        assert out.iloc[2] == 30.0
        assert out.iloc[3] == 30.0

    def test_math_helpers(self):
        assert ind.math_max(3, 7) == 7
        assert ind.math_min(3, 7) == 3
        assert ind.math_abs(-5) == 5
        assert ind.nz(float("nan"), 1) == 1
        assert ind.math_round(3.14159, 2) == 3.14

    def test_math_helpers_series(self, data):
        a = data["close"]
        b = ind.math_max(a, 100.0)
        assert (b >= 100.0).all()


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


class TestParser:
    def test_parses_simple(self):
        program = parse(SIMPLE)
        assert len(program.statements) >= 4
        assert program.header is not None
        assert program.header.callee() == "indicator"

    def test_history_offset(self):
        program = parse("//@version=5\nx = close[3]\n")
        assert len(program.statements) == 1

    def test_tuple_destructuring(self):
        program = parse("//@version=5\n[line, sig, hist] = ta.macd(close, 12, 26, 9)\n")
        assert len(program.statements) == 1

    def test_if_else(self):
        program = parse(
            "//@version=5\n"
            "x = 0.0\n"
            "if close > open\n"
            "    x := 1.0\n"
            "else\n"
            "    x := -1.0\n"
        )
        # x = 0.0, then one If statement (with its else branch)
        assert len(program.statements) == 2
        assert type(program.statements[1]).__name__ == "If"

    def test_user_function(self):
        program = parse("//@version=5\nf(x) => x * 2\nplot(f(close), title=\"d\")\n")
        assert len(program.statements) == 2

    def test_for_loop(self):
        program = parse(
            "//@version=5\n"
            "total = 0.0\n"
            "for i = 0 to 3\n"
            "    total := total + i\n"
        )
        assert len(program.statements) == 2

    def test_rejects_empty(self):
        with pytest.raises(PineSyntaxError):
            parse("")

    def test_rejects_bad_token(self):
        with pytest.raises(PineSyntaxError):
            parse("//@version=5\nx = @\n")

    def test_ternary(self):
        parse("//@version=5\nx = close > open ? 1.0 : -1.0\n")


# --------------------------------------------------------------------------
# linter
# --------------------------------------------------------------------------


class TestLinter:
    def test_clean_script_passes(self):
        report = lint(SIMPLE)
        assert report.ok
        assert report.version == "5"

    def test_adds_missing_version(self):
        report = lint('indicator("x", overlay=true)\nplot(close)\n')
        assert report.fixed_source is not None
        assert "//@version=5" in report.fixed_source

    def test_upgrades_v4_calls(self):
        report = lint('//@version=4\nstudy("x")\nplot(sma(close, 14))\n', autofix_enabled=True)
        fixed = report.fixed_source or ""
        assert "indicator(" in fixed
        assert "ta.sma(" in fixed

    def test_normalises_tabs(self):
        report = lint('//@version=5\nindicator("x")\nif close > open\n\tplot(close)\n')
        fixed = report.fixed_source or ""
        assert "\t" not in fixed
        assert "    plot(close)" in fixed

    def test_fixes_member_casing(self):
        report = lint('//@version=5\nindicator("x")\nplot(ta.SMA(close, 10))\n', autofix_enabled=True)
        assert "ta.sma(" in (report.fixed_source or "")

    def test_returns_error_for_empty(self):
        report = lint("")
        assert not report.ok
        assert report.errors

    def test_warns_about_lookahead(self):
        src = (
            "//@version=5\n"
            'indicator("x")\n'
            'h = request.security(syminfo.tickerid, "1D", close, lookahead=barmerge.lookahead_on)\n'
            "plot(h)\n"
        )
        report = lint(src)
        assert any(d.code == "SEM002" for d in report.diagnostics)


# --------------------------------------------------------------------------
# evaluator
# --------------------------------------------------------------------------


class TestEvaluator:
    def test_plots_captured(self, data):
        result, _ = evaluate(SIMPLE, data)
        assert "EMA fast" in result.plots
        assert "EMA slow" in result.plots
        assert "RSI" in result.plots
        assert len(result.plots["EMA fast"]) == len(data)

    def test_ema_matches_reference(self, data):
        result, _ = evaluate(SIMPLE, data)
        expected = data["close"].ewm(span=9, adjust=False, min_periods=9).mean()
        diff = (result.plots["EMA fast"] - expected).abs().max()
        assert float(diff) < 1e-9

    def test_history_reference(self, data):
        src = '//@version=5\nindicator("h")\nplot(close[1], title="prev")\n'
        result, _ = evaluate(src, data)
        expected = data["close"].shift(1)
        pd.testing.assert_series_equal(
            result.plots["prev"].dropna(), expected.dropna(), check_names=False
        )

    def test_strategy_entry_only_inside_if(self, data):
        src = (
            "//@version=5\n"
            'strategy("s", overlay=true)\n'
            "c = ta.crossover(ta.ema(close, 5), ta.ema(close, 20))\n"
            "if c\n"
            '    strategy.entry("L", strategy.long)\n'
        )
        result, _ = evaluate(src, data)
        assert result.entries_long is not None
        assert result.entries_long.sum() > 0
        assert result.entries_long.sum() < len(data) // 4  # not every bar

    def test_entry_matches_crossover(self, data):
        src = (
            "//@version=5\n"
            'strategy("s", overlay=true)\n'
            "f = ta.ema(close, 5)\n"
            "s = ta.ema(close, 20)\n"
            "c = ta.crossover(f, s)\n"
            "if c\n"
            '    strategy.entry("L", strategy.long)\n'
        )
        result, _ = evaluate(src, data)
        f = data["close"].ewm(span=5, adjust=False, min_periods=5).mean()
        s = data["close"].ewm(span=20, adjust=False, min_periods=20).mean()
        expected = ind.crossover(f, s)
        assert int(result.entries_long.sum()) == int(expected.sum())

    def test_ternary_and_boolean_ops(self, data):
        src = (
            "//@version=5\n"
            'indicator("t")\n'
            "v = close > open and volume > 0 ? 1.0 : 0.0\n"
            'plot(v, title="flag")\n'
        )
        result, _ = evaluate(src, data)
        expected = (data["close"] > data["open"]).astype(float)
        pd.testing.assert_series_equal(
            result.plots["flag"].astype(float), expected, check_names=False
        )

    def test_user_function(self, data):
        src = (
            "//@version=5\n"
            'indicator("f")\n'
            "double(x) => x * 2\n"
            'plot(double(close), title="d")\n'
        )
        result, _ = evaluate(src, data)
        pd.testing.assert_series_equal(
            result.plots["d"], (data["close"] * 2), check_names=False
        )

    def test_unknown_identifier_raises(self, data):
        with pytest.raises(PineRuntimeError):
            evaluate('//@version=5\nindicator("x")\nplot(nope)\n', data)

    def test_unknown_function_raises(self, data):
        with pytest.raises(PineRuntimeError):
            evaluate('//@version=5\nindicator("x")\nplot(ta.notreal(close, 5))\n', data)

    def test_inputs_resolve_defaults(self, data):
        src = (
            "//@version=5\n"
            'indicator("i")\n'
            'len = input.int(7, "len")\n'
            'plot(ta.sma(close, len), title="s")\n'
        )
        result, _ = evaluate(src, data)
        expected = ind.sma(data["close"], 7)
        pd.testing.assert_series_equal(
            result.plots["s"].dropna(), expected.dropna(), check_names=False
        )

    def test_unsupported_calls_are_noted(self, data):
        src = (
            "//@version=5\n"
            'indicator("a")\n'
            "plot(close)\n"
            'alert("hi", alert.freq_once_per_bar)\n'
        )
        result, _ = evaluate(src, data)
        assert any("alert" in note for note in result.notes)

    def test_plotshape_captured(self, data):
        src = (
            "//@version=5\n"
            'indicator("p")\n'
            "c = close > open\n"
            'plotshape(c, title="up", style=shape.triangleup, location=location.belowbar)\n'
        )
        result, _ = evaluate(src, data)
        assert "up" in result.plots

    def test_no_lookahead_bias(self, data):
        """Truncating the series must not change earlier values."""
        result_full, _ = evaluate(SIMPLE, data)
        truncated = data.iloc[:200]
        result_trunc, _ = evaluate(SIMPLE, truncated)
        a = result_full.plots["EMA fast"].iloc[:200].dropna()
        b = result_trunc.plots["EMA fast"].iloc[:200].dropna()
        pd.testing.assert_series_equal(a, b)


# --------------------------------------------------------------------------
# precursor
# --------------------------------------------------------------------------


def _converging_series(n: int = 320, freq: str = "5min"):
    """Build a series whose fast EMA is about to cross above the slow EMA.

    The final bars are truncated to *just before* the crossover so the test
    exercises the projection rather than an already-completed cross.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    # down-trend then a sharp turn up: the fast EMA turns first, so the cross
    # happens a few bars after the turn begins.
    close = np.concatenate(
        [np.linspace(100, 90, n - 8), np.linspace(90, 94.0, 8)]
    )
    data = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )
    fast = ind.ema(data["close"], 9)
    slow = ind.ema(data["close"], 21)
    cross = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    positions = np.flatnonzero(cross.to_numpy())
    cross_bar = int(positions[0]) if len(positions) else n - 1
    # keep bars strictly before the cross
    cut = max(30, cross_bar - 1)
    return data.iloc[:cut], fast.iloc[:cut], slow.iloc[:cut], cross_bar


class TestPrecursor:
    def test_timeframe_minutes(self):
        assert timeframe_minutes("1m") == 1.0
        assert timeframe_minutes("5m") == 5.0
        assert timeframe_minutes("1h") == 60.0
        assert timeframe_minutes("4h") == 240.0
        assert timeframe_minutes("1d") == 1440.0
        assert timeframe_minutes("15m") == 15.0

    def test_detects_converging_cross(self):
        data, fast, slow, cross_bar = _converging_series()
        assert cross_bar > 30, "fixture must contain a real crossover"
        sig = detect_precursor(fast, slow, data, horizon_bars=5, timeframe="1m")
        assert sig.direction == 1, sig.reasons
        assert sig.eta_bars >= 1
        assert 0.0 <= sig.confidence <= 1.0
        assert sig.is_actionable

    def test_warns_within_horizon_before_the_cross_bar(self):
        """The whole point: at least one in-horizon warning precedes the cross.

        Scanning the 5 bars before the crossover must yield at least one
        actionable projection that lands inside the alert horizon. Early bars
        legitimately report a large ETA (the gap is still wide), so the property
        under test is "a warning is possible before the cross", not "every bar
        warns".
        """
        data, fast, slow, cross_bar = _converging_series()
        warnings = []
        for cut in range(cross_bar - 5, cross_bar + 1):
            if cut < 5:
                continue
            sig = detect_precursor(
                fast.iloc[:cut], slow.iloc[:cut], data.iloc[:cut],
                horizon_bars=5, timeframe="1m",
            )
            if sig.direction:
                warnings.append((cut, sig.eta_bars, sig.in_horizon))

        assert warnings, "engine should warn at least once before the cross"
        assert any(in_horizon for _, _, in_horizon in warnings), (
            f"expected an in-horizon warning, got {warnings}"
        )

    def test_wide_gap_reports_large_eta(self):
        """Sanity: when the lines are far apart the ETA must be large, not 1."""
        data, fast, slow, _ = _converging_series()
        early = 60
        sig = detect_precursor(
            fast.iloc[:early], slow.iloc[:early], data.iloc[:early],
            horizon_bars=5, timeframe="1m",
        )
        if sig.direction:
            assert sig.eta_bars > 5
            assert not sig.in_horizon

    def test_diverging_returns_none(self, data):
        fast = ind.ema(data["close"], 9)
        slow = ind.ema(data["close"], 21)
        sig = detect_precursor(fast, slow, data, horizon_bars=5, timeframe="1d")
        assert sig.direction == 0
        assert sig.reasons

    def test_horizon_flag(self):
        n = 200
        idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
        close = np.concatenate([np.linspace(50, 45, n - 4), np.linspace(45, 47, 4)])
        data = pd.DataFrame(
            {"open": close, "high": close + 0.02, "low": close - 0.02, "close": close,
             "volume": np.full(n, 100.0)},
            index=idx,
        )
        fast = ind.ema(data["close"], 5)
        slow = ind.ema(data["close"], 15)
        sig = detect_precursor(fast, slow, data, horizon_bars=5, timeframe="1m")
        assert isinstance(sig.in_horizon, bool)
        if sig.direction:
            expected = sig.eta_bars <= 5
            assert sig.in_horizon == expected

    def test_eta_minutes_scales_with_timeframe(self):
        data, fast, slow, _ = _converging_series(freq="1min")
        sig = detect_precursor(fast, slow, data, horizon_bars=5, timeframe="1m")
        if sig.direction:
            assert sig.eta_minutes == pytest.approx(sig.eta_bars * 1.0)

    def test_handles_short_series(self, data):
        sig = detect_precursor(
            data["close"].iloc[:2], data["close"].iloc[:2], data.iloc[:2],
            horizon_bars=5, timeframe="1d",
        )
        assert sig.direction == 0


# --------------------------------------------------------------------------
# backtest
# --------------------------------------------------------------------------


class TestBacktest:
    def test_produces_trades(self, data):
        src = (
            "//@version=5\n"
            'strategy("s", overlay=true)\n'
            "f = ta.ema(close, 5)\n"
            "s = ta.ema(close, 20)\n"
            "if ta.crossover(f, s)\n"
            '    strategy.entry("L", strategy.long)\n'
            "if ta.crossunder(f, s)\n"
            '    strategy.exit("X", when=ta.crossunder(f, s))\n'
        )
        result, _ = evaluate(src, data)
        stats = simulate_trades(data, result)
        assert stats.count >= 1
        assert 0 <= stats.win_rate <= 100

    def test_stats_serialisable(self, data):
        src = (
            "//@version=5\n"
            'strategy("s")\n'
            "if ta.crossover(ta.ema(close, 5), ta.ema(close, 20))\n"
            '    strategy.entry("L", strategy.long)\n'
        )
        result, _ = evaluate(src, data)
        payload = simulate_trades(data, result).to_dict()
        assert set(payload) >= {"trades", "win_rate_pct", "total_pnl_pct"}

    def test_no_signals_no_trades(self, data):
        result, _ = evaluate(SIMPLE, data)
        stats = simulate_trades(data, result)
        assert stats.count == 0
        assert stats.win_rate == 0.0


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------


class TestCharts:
    def test_split_plot_series(self):
        idx = pd.date_range("2024-01-01", periods=5)
        plots = {
            "EMA fast": pd.Series([1.0, 2, 3, 4, 5], index=idx),
            "RSI": pd.Series([50.0, 60, 70, 80, 90], index=idx),
        }
        overlay, panel, conditions = split_plot_series(plots)
        assert "EMA fast" in overlay
        assert "RSI" in panel
        assert conditions == {}

    def test_render_png(self, data, tmp_path):
        result, _ = evaluate(SIMPLE, data)
        overlay, panel, conditions = split_plot_series(result.plots)
        spec = ChartSpec(
            symbol="TEST",
            timeframe="1d",
            last_bars=120,
            overlay_indicators=overlay,
            panel_indicators=panel,
            condition_plots=conditions,
            signals_long=result.entries_long,
            signals_short=result.entries_short,
        )
        out = tmp_path / "chart.png"
        render_mplfinance(data, spec, out)
        assert out.exists()
        assert out.stat().st_size > 10_000

    def test_render_png_without_indicators(self, data, tmp_path):
        spec = ChartSpec(symbol="TEST", timeframe="1d", last_bars=50)
        out = tmp_path / "bare.png"
        render_mplfinance(data, spec, out)
        assert out.exists()

    def test_render_html_is_self_contained(self, data, tmp_path):
        from nanobot.trading.pine.chart import render_interactive_html

        spec = ChartSpec(symbol="TEST", timeframe="1d", last_bars=60)
        out = tmp_path / "chart.html"
        render_interactive_html(data, spec, out)
        html = out.read_text()
        assert "lightweight-charts" in html
        # No TradingView widget/account involved.
        assert "tradingview.com" not in html.lower()
        assert "tv.js" not in html.lower()


# --------------------------------------------------------------------------
# end-to-end tool behaviour
# --------------------------------------------------------------------------


def _tool_instance(tool_cls):
    """Build a Tool instance without running __init__ (schemas need no state)."""
    return tool_cls.__new__(tool_cls)


class TestToolSurface:
    def test_tool_schema_is_valid_json_schema(self):
        from nanobot.agent.tools.pine_script import PineScriptTool

        schema = _tool_instance(PineScriptTool).parameters
        assert schema["type"] == "object"
        assert "action" in schema["properties"]
        assert schema["required"] == ["action"]

    def test_chart_tool_schema(self):
        from nanobot.agent.tools.pine_chart import PineChartTool

        schema = _tool_instance(PineChartTool).parameters
        assert "symbol" in schema["properties"]
        assert schema["required"] == ["symbol"]

    def test_tool_identity(self):
        from nanobot.agent.tools.pine_chart import PineChartTool
        from nanobot.agent.tools.pine_script import PineScriptTool

        assert _tool_instance(PineScriptTool).name == "pine_script"
        assert _tool_instance(PineChartTool).name == "pine_chart"

    def test_registry_reports_surface(self):
        from nanobot.trading.pine.registry import capability_summary, describe_surface

        surface = describe_surface()
        assert "ta" in surface and "math" in surface
        assert "ema" in surface["ta"]
        assert capability_summary()["ta"] > 20
