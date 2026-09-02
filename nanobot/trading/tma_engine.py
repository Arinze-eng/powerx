"""Faithful Python port of the supplied legacy NB-TMA slope logic."""

from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    return pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def wilder_atr(frame: pd.DataFrame, period: int) -> pd.Series:
    if period < 1:
        raise ValueError("ATR period must be positive.")
    return true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def nb_tma(
    closes: pd.Series | np.ndarray,
    center_index: int,
    current_index: int,
    period: int,
) -> float | None:
    """Port NB_TMA() while never reading beyond current_index."""

    values = np.asarray(closes, dtype=float)
    if period < 1 or center_index < 0 or current_index >= len(values):
        return None
    if center_index > current_index or center_index + period >= len(values):
        return None
    center_price = values[center_index]
    if not np.isfinite(center_price):
        return None

    total = center_price * (period + 1)
    weight_sum = float(period + 1)
    center_shift = current_index - center_index
    for j in range(1, period + 1):
        weight = period + 1 - j
        older_index = center_index - j
        if older_index < 0 or not np.isfinite(values[older_index]):
            return None
        total += weight * values[older_index]
        weight_sum += weight
        newer_index = center_index + j
        if j <= center_shift and newer_index <= current_index:
            if not np.isfinite(values[newer_index]):
                return None
            total += weight * values[newer_index]
            weight_sum += weight
    return float(total / weight_sum)


def legacy_slope(
    frame: pd.DataFrame,
    current_index: int,
    tma_period: int = 20,
    atr_period: int = 100,
    atr_shift: int = 11,
) -> float | None:
    """Calculate (t0 - t1) / (ATR[shift] / 10), as in GetLegacySlope()."""

    if current_index < 0 or current_index >= len(frame):
        return None
    t0 = nb_tma(frame["close"], current_index - 1, current_index, tma_period)
    t1 = nb_tma(frame["close"], current_index - 2, current_index, tma_period)
    atr = wilder_atr(frame, atr_period)
    atr_index = current_index - atr_shift
    if t0 is None or t1 is None or atr_index < 0 or not np.isfinite(atr.iloc[atr_index]):
        return None
    atr_value = float(atr.iloc[atr_index])
    if atr_value <= 0:
        return None
    return float((t0 - t1) / (atr_value / 10.0))


def double_smoothed_sma(prices: pd.Series, period: int) -> pd.Series:
    return prices.rolling(period, min_periods=period).mean().rolling(period, min_periods=period).mean()


def basket_correlation(frames: dict[str, pd.DataFrame], lookback: int = 30) -> float:
    returns = pd.concat(
        {pair: frame["close"].pct_change() for pair, frame in frames.items()},
        axis=1,
    ).tail(lookback)
    corr = returns.corr().to_numpy(dtype=float)
    upper = corr[np.triu_indices_from(corr, k=1)]
    upper = upper[np.isfinite(upper)]
    return float(upper.mean()) if upper.size else 0.0
