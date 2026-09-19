"""Vectorised implementations of Pine ``ta.*`` / ``math.*`` built-ins.

Every function is *causal*: the value at bar ``i`` depends only on bars
``<= i``. That is what makes the whole-series evaluation in :mod:`evaluator`
equivalent to TradingView's per-bar semantics.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _s(series: Any) -> pd.Series:
    if isinstance(series, pd.Series):
        return series
    return pd.Series(series, dtype="float64")


def _n(value: Any, default: float = 0.0) -> float:
    """Best-effort scalar coercion (handles na / NaN / Series-of-one)."""
    if value is None:
        return default
    if isinstance(value, pd.Series):
        if value.empty:
            return default
        value = value.iloc[-1]
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if np.isnan(out) else out


# --------------------------------------------------------------------------
# moving averages
# --------------------------------------------------------------------------


def sma(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    return _s(source).rolling(n, min_periods=n).mean()


def ema(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    return _s(source).ewm(span=n, adjust=False, min_periods=n).mean()


def rma(source: Any, length: Any) -> pd.Series:
    """Wilder's smoothing (used by RSI and ATR)."""
    n = max(1, int(_n(length, 14)))
    return _s(source).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def wma(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    weights = np.arange(1, n + 1, dtype="float64")
    denom = weights.sum()
    return _s(source).rolling(n, min_periods=n).apply(
        lambda x: float(np.dot(x, weights) / denom), raw=True
    )


def hma(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    half = max(1, n // 2)
    root = max(1, int(np.sqrt(n)))
    raw = 2 * wma(source, half) - wma(source, n)
    return wma(raw, root)


def vwma(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    src = _s(source)
    return pd.Series(np.nan, index=src.index, dtype="float64") if n < 1 else src.rolling(n, min_periods=n).mean()


def swma(source: Any) -> pd.Series:
    src = _s(source)
    return (src.shift(3) + 2 * src.shift(2) + 2 * src.shift(1) + src) / 6


# --------------------------------------------------------------------------
# volatility / range
# --------------------------------------------------------------------------


def true_range(high: Any, low: Any, close: Any) -> pd.Series:
    h, lo, c = _s(high), _s(low), _s(close)
    prev = c.shift(1)
    return pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)


def atr(high: Any, low: Any, close: Any, length: Any = 14) -> pd.Series:
    return rma(true_range(high, low, close), length)


def stdev(source: Any, length: Any, biased: bool = True) -> pd.Series:
    n = max(1, int(_n(length, 20)))
    ddof = 0 if biased else 1
    return _s(source).rolling(n, min_periods=n).std(ddof=ddof)


def variance(source: Any, length: Any, biased: bool = True) -> pd.Series:
    n = max(1, int(_n(length, 20)))
    ddof = 0 if biased else 1
    return _s(source).rolling(n, min_periods=n).var(ddof=ddof)


def dev(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 20)))
    src = _s(source)
    return src - src.rolling(n, min_periods=n).mean()


# --------------------------------------------------------------------------
# oscillators
# --------------------------------------------------------------------------


def rsi(source: Any, length: Any = 14) -> pd.Series:
    src = _s(source)
    delta = src.diff()
    up = rma(delta.clip(lower=0), length)
    down = rma((-delta).clip(lower=0), length)
    rs = up / down.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # When there are no losses over the window RSI is 100 by definition.
    return out.where(down.notna(), np.nan).fillna(100.0).where(up.notna() | down.notna())


def macd(source: Any, fast: Any = 12, slow: Any = 26, signal: Any = 9):
    src = _s(source)
    f = ema(src, fast)
    s = ema(src, slow)
    line = f - s
    sig = ema(line, signal)
    return line, sig, line - sig


def bb(source: Any, length: Any = 20, mult: Any = 2.0):
    n = max(1, int(_n(length, 20)))
    k = _n(mult, 2.0)
    src = _s(source)
    basis = src.rolling(n, min_periods=n).mean()
    sd = src.rolling(n, min_periods=n).std(ddof=0)
    return basis, basis + k * sd, basis - k * sd


def stoch(close: Any, high: Any, low: Any, length: Any = 14) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    c, h, lo = _s(close), _s(high), _s(low)
    hh = h.rolling(n, min_periods=n).max()
    ll = lo.rolling(n, min_periods=n).min()
    span = (hh - ll).replace(0, np.nan)
    return (100 * (c - ll) / span).fillna(50.0)


def cci(high: Any, low: Any, close: Any, length: Any = 20) -> pd.Series:
    n = max(1, int(_n(length, 20)))
    tp = (_s(high) + _s(low) + _s(close)) / 3
    ma = tp.rolling(n, min_periods=n).mean()
    md = (tp - ma).abs().rolling(n, min_periods=n).mean()
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def mfi(high: Any, low: Any, close: Any, volume: Any, length: Any = 14):
    """Money Flow Index. Volume falls back to 1 when unavailable."""
    n = max(1, int(_n(length, 14)))
    tp = (_s(high) + _s(low) + _s(close)) / 3
    vol = _s(volume) if volume is not None else pd.Series(1.0, index=tp.index)
    raw = tp * vol
    delta = tp.diff()
    pos = raw.where(delta > 0, 0.0).rolling(n, min_periods=n).sum()
    neg = raw.where(delta < 0, 0.0).rolling(n, min_periods=n).sum()
    ratio = pos / neg.replace(0, np.nan)
    return 100 - (100 / (1 + ratio))


def roc(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 9)))
    src = _s(source)
    return 100 * (src - src.shift(n)) / src.shift(n).replace(0, np.nan)


def mom(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 10)))
    return _s(source) - _s(source).shift(n)


def change(source: Any, length: Any = 1) -> pd.Series:
    n = max(1, int(_n(length, 1)))
    return _s(source).diff(n)


# --------------------------------------------------------------------------
# extremes / statistics
# --------------------------------------------------------------------------


def highest(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    return _s(source).rolling(n, min_periods=n).max()


def lowest(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    return _s(source).rolling(n, min_periods=n).min()


def highestbars(source: Any, length: Any) -> pd.Series:
    win = max(1, int(_n(length, 14)))
    src = _s(source)

    def _offset(x: np.ndarray) -> float:
        return float(-(len(x) - 1 - int(np.argmax(x))))

    return src.rolling(win, min_periods=win).apply(_offset, raw=True)


def lowestbars(source: Any, length: Any) -> pd.Series:
    win = max(1, int(_n(length, 14)))
    src = _s(source)

    def _offset(x: np.ndarray) -> float:
        return float(-(len(x) - 1 - int(np.argmin(x))))

    return src.rolling(win, min_periods=win).apply(_offset, raw=True)


def sum_(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    return _s(source).rolling(n, min_periods=n).sum()


def median(source: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    return _s(source).rolling(n, min_periods=n).median()


def percentile(source: Any, length: Any, percent: Any) -> pd.Series:
    """Rolling linear-interpolated percentile (Pine's ``ta.percentile_linear_interpolation``)."""
    n = max(1, int(_n(length, 14)))
    p = _n(percent, 50.0) / 100.0
    return _s(source).rolling(n, min_periods=n).quantile(p)


def linreg(source: Any, length: Any, offset: Any = 0) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    off = int(_n(offset, 0))
    src = _s(source)
    idx = np.arange(n, dtype="float64")
    denom = ((idx - idx.mean()) ** 2).sum()

    def _fit(win: np.ndarray) -> float:
        slope = float(((idx - idx.mean()) * (win - win.mean())).sum() / denom)
        intercept = float(win.mean() - slope * idx.mean())
        return intercept + slope * (n - 1)

    fit = src.rolling(n, min_periods=n).apply(_fit, raw=True)
    return fit.shift(-off) if off else fit


def correlation(a: Any, b: Any, length: Any) -> pd.Series:
    n = max(1, int(_n(length, 14)))
    return _s(a).rolling(n, min_periods=n).corr(_s(b))


def barssince(condition: Any) -> pd.Series:
    cond = condition.fillna(False).astype(bool)
    out = np.full(len(cond), np.nan)
    last = -1
    for i, flag in enumerate(cond.to_numpy()):
        if flag:
            last = i
        if last >= 0:
            out[i] = i - last
    return pd.Series(out, index=cond.index, dtype="float64")


def valuewhen(condition: Any, source: Any, occurrence: Any = 0) -> pd.Series:
    n = max(0, int(_n(occurrence, 0)))
    cond = condition.fillna(False).astype(bool)
    src = _s(source)
    out = np.full(len(cond), np.nan)
    history: list[float] = []
    for i, flag in enumerate(cond.to_numpy()):
        if flag:
            history.insert(0, float(src.iloc[i]))
        if len(history) > n:
            out[i] = history[n]
    return pd.Series(out, index=cond.index, dtype="float64")


# --------------------------------------------------------------------------
# crossovers
# --------------------------------------------------------------------------


def crossover(a: Any, b: Any) -> pd.Series:
    sa, sb = _s(a), _s(b)
    return (sa > sb) & (sa.shift(1) <= sb.shift(1))


def crossunder(a: Any, b: Any) -> pd.Series:
    sa, sb = _s(a), _s(b)
    return (sa < sb) & (sa.shift(1) >= sb.shift(1))


def cross(a: Any, b: Any) -> pd.Series:
    return crossover(a, b) | crossunder(a, b)


def rising(source: Any, length: Any = 1) -> pd.Series:
    n = max(1, int(_n(length, 1)))
    src = _s(source)
    return src > src.shift(n)


def falling(source: Any, length: Any = 1) -> pd.Series:
    n = max(1, int(_n(length, 1)))
    src = _s(source)
    return src < src.shift(n)


# --------------------------------------------------------------------------
# session / volume
# --------------------------------------------------------------------------


def cum(source: Any) -> pd.Series:
    return _s(source).fillna(0).cumsum()


def vwap(high: Any, low: Any, close: Any, volume: Any = None) -> pd.Series:
    tp = (_s(high) + _s(low) + _s(close)) / 3
    vol = _s(volume) if volume is not None else pd.Series(1.0, index=tp.index)
    cumulative = vol.fillna(0).cumsum().replace(0, np.nan)
    return (tp * vol.fillna(0)).cumsum() / cumulative


def cum_volume(volume: Any) -> pd.Series:
    return _s(volume).fillna(0).cumsum()


# --------------------------------------------------------------------------
# math.*
# --------------------------------------------------------------------------


def math_abs(x: Any) -> Any:
    return x.abs() if isinstance(x, pd.Series) else abs(x)


def _align_operands(values: tuple[Any, ...]) -> tuple[list[Any], pd.Series | None]:
    """Align mixed scalars/series operands so they share one index.

    A bare scalar wrapped by ``pd.Series(3.0)`` would get a RangeIndex(0..0) and
    silently produce all-NaN results once concatenated with a real series, so
    every scalar is reindexed onto the series index explicitly.
    """
    series = [v for v in values if isinstance(v, pd.Series)]
    index = series[0].index if series else None
    out: list[Any] = []
    for value in values:
        if isinstance(value, pd.Series):
            out.append(value)
        elif index is not None and isinstance(value, (int, float, bool, np.number)):
            out.append(pd.Series(float(value), index=index, dtype="float64"))
        else:
            out.append(value)
    return out, index


def math_max(*values: Any) -> Any:
    aligned, index = _align_operands(values)
    if index is not None:
        return pd.concat(
            [v if isinstance(v, pd.Series) else pd.Series(v, index=index) for v in aligned],
            axis=1,
        ).max(axis=1)
    return max(aligned)


def math_min(*values: Any) -> Any:
    aligned, index = _align_operands(values)
    if index is not None:
        return pd.concat(
            [v if isinstance(v, pd.Series) else pd.Series(v, index=index) for v in aligned],
            axis=1,
        ).min(axis=1)
    return min(aligned)


def math_round(x: Any, precision: Any = 0) -> Any:
    p = int(_n(precision, 0))
    return np.round(x, p) if isinstance(x, pd.Series) else round(float(x), p)


def math_floor(x: Any) -> Any:
    return np.floor(x) if isinstance(x, pd.Series) else int(np.floor(float(x)))


def math_ceil(x: Any) -> Any:
    return np.ceil(x) if isinstance(x, pd.Series) else int(np.ceil(float(x)))


def math_sqrt(x: Any) -> Any:
    if isinstance(x, pd.Series):
        return np.sqrt(x.clip(lower=0))
    return float(np.sqrt(max(0.0, float(x))))


def math_pow(base: Any, exp: Any) -> Any:
    if isinstance(base, pd.Series) or isinstance(exp, pd.Series):
        return _s(base) ** _s(exp)
    return float(base) ** float(exp)


def math_log(x: Any) -> Any:
    if isinstance(x, pd.Series):
        return np.log(x.where(x > 0))
    return float(np.log(x)) if _n(x, 0) > 0 else float("nan")


def math_avg(*values: Any) -> Any:
    series = [v for v in values if isinstance(v, pd.Series)]
    if series:
        pool = pd.concat([_s(v) for v in values], axis=1)
        return pool.mean(axis=1)
    return float(np.mean([float(v) for v in values])) if values else float("nan")


def math_sign(x: Any) -> Any:
    return np.sign(x) if isinstance(x, pd.Series) else int(np.sign(float(x)))


def math_exp(x: Any) -> Any:
    return np.exp(x) if isinstance(x, pd.Series) else float(np.exp(float(x)))


def math_na(x: Any) -> Any:
    if isinstance(x, pd.Series):
        return x.isna()
    try:
        return bool(np.isnan(float(x)))
    except (TypeError, ValueError):
        return x is None


def math_isfinite(x: Any) -> Any:
    if isinstance(x, pd.Series):
        return np.isfinite(x)
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


def nz(value: Any, replacement: Any = 0) -> Any:
    if isinstance(value, pd.Series):
        return value.fillna(replacement)
    return replacement if math_na(value) else value
