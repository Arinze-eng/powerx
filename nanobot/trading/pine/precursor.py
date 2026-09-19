"""Precursor (early) signal detection.

The user requirement: *"the signal should pop ~1-5 minutes/bars before it plays"*.

A crossover signal fires on the bar where two lines actually cross. Waiting for
that bar means the move has already started. This module answers a different
question:

    "Given the current slopes and distance between the two lines, on which
     future bar would they cross — and is that within the alert horizon?"

Method
------
For two series ``fast`` and ``slow``:

1. Compute the gap ``g[i] = fast[i] - slow[i]``.
2. Estimate the per-bar velocity of the gap with a short EMA-smoothed slope
   (a robust, low-lag slope estimate that does not over-react to one bar).
3. Project ``gap`` forward: ``g_hat[i + k] = g[i] + k * slope[i]``.
4. The crossing bar is the smallest ``k >= 1`` where ``sign(g_hat)`` flips.
5. The distance is normalised by ATR so the resulting confidence is comparable
   across instruments and timeframes.
6. Confidence combines: normalised distance (closer = better), slope agreement
   (both lines converging), and confirmation filters (RSI / MACD direction).

The output is deliberately conservative: ``confidence`` is only reported as
high when the projection is stable across the last few bars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from nanobot.trading.pine import indicators as ind


@dataclass
class PrecursorSignal:
    """A signal projected to fire within the next ``eta_bars`` bars."""

    direction: int  # +1 long, -1 short, 0 none
    eta_bars: int  # projected bars until the cross
    eta_minutes: float  # projected wall-clock minutes
    confidence: float  # 0..1
    gap: float  # current fast - slow
    slope: float  # per-bar change of the gap
    atr: float  # atr at the current bar
    normalized_gap: float  # |gap| / atr
    fast_name: str = "fast"
    slow_name: str = "slow"
    price: float = 0.0
    projected_price: float = 0.0
    bar_index: int = 0
    horizon_bars: int = 5
    reasons: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return self.direction != 0 and self.eta_bars >= 1

    @property
    def in_horizon(self) -> bool:
        """True when the projected cross lands inside the alert window."""
        return self.is_actionable and self.eta_bars <= self.horizon_bars

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "side": "long" if self.direction > 0 else ("short" if self.direction < 0 else "none"),
            "eta_bars": self.eta_bars,
            "eta_minutes": round(self.eta_minutes, 2),
            "confidence": round(self.confidence, 4),
            "gap": round(self.gap, 6),
            "slope": round(self.slope, 6),
            "atr": round(self.atr, 6),
            "normalized_gap": round(self.normalized_gap, 4),
            "price": round(self.price, 6),
            "projected_price": round(self.projected_price, 6),
            "bar_index": self.bar_index,
            "horizon_bars": self.horizon_bars,
            "in_horizon": self.in_horizon,
            "reasons": list(self.reasons),
        }


# minutes per bar for the timeframes we support
TIMEFRAME_MINUTES = {
    "1m": 1.0, "2m": 2.0, "3m": 3.0, "5m": 5.0, "10m": 10.0, "15m": 15.0,
    "30m": 30.0, "45m": 45.0, "1h": 60.0, "2h": 120.0, "4h": 240.0,
    "6h": 360.0, "12h": 720.0, "1d": 1440.0, "1w": 10080.0, "1M": 43200.0,
}


def timeframe_minutes(timeframe: str) -> float:
    """Best-effort conversion of a timeframe token to minutes."""
    token = (timeframe or "1d").strip()
    if token in TIMEFRAME_MINUTES:
        return TIMEFRAME_MINUTES[token]
    lower = token.lower()
    if lower in TIMEFRAME_MINUTES:
        return TIMEFRAME_MINUTES[lower]
    try:
        if lower.endswith("m"):
            return float(lower[:-1])
        if lower.endswith("h"):
            return float(lower[:-1]) * 60
        if lower.endswith("d"):
            return float(lower[:-1]) * 1440
        if lower.endswith("w"):
            return float(lower[:-1]) * 10080
    except ValueError:
        pass
    return TIMEFRAME_MINUTES["1d"]


def _slope(series: pd.Series, lookback: int = 5) -> pd.Series:
    """Robust per-bar slope: average of successive diffs, EMA-smoothed."""
    diffs = series.diff()
    smoothed = diffs.ewm(span=max(2, lookback), adjust=False, min_periods=1).mean()
    return smoothed


def _projection_slope(gap_series: pd.Series, lookback: int) -> tuple[float, bool]:
    """Estimate the per-bar closing speed of the gap.

    Returns ``(slope, used_accelerated_estimate)``.

    A smoothed slope (EMA of diffs) is the stable default, but right before a
    crossover the gap is *accelerating*, so the smoothed value lags and
    over-estimates the ETA. In that regime the current 2-bar speed is the more
    accurate predictor, so the larger closing-magnitude estimate is used —
    being a bar early is much more useful than being three bars late.
    """
    smoothed_series = _slope(gap_series, lookback)
    smoothed = float(smoothed_series.iloc[-1])

    diffs = gap_series.diff().dropna()
    fast = float(diffs.tail(2).mean()) if len(diffs) >= 2 else float(diffs.iloc[-1])

    gap = float(gap_series.iloc[-1])
    gap_sign = _sign(gap)

    def _closing(value: float) -> bool:
        return np.isfinite(value) and value != 0.0 and _sign(value) == -gap_sign

    if _closing(fast) and (not _closing(smoothed) or abs(fast) > abs(smoothed)):
        return fast, True
    return smoothed, False


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def detect_precursor(
    fast: pd.Series,
    slow: pd.Series,
    data: pd.DataFrame,
    *,
    horizon_bars: int = 5,
    timeframe: str = "1d",
    atr_period: int = 14,
    slope_lookback: int = 5,
    fast_name: str = "fast",
    slow_name: str = "slow",
    confirm_rsi: pd.Series | None = None,
    atr_floor_ratio: float = 0.05,
) -> PrecursorSignal:
    """Project whether *fast* will cross *slow* within *horizon_bars*.

    Set ``horizon_bars`` to the user's alert window (e.g. 5 bars on a 1-minute
    chart = "5 minutes before it plays").
    """
    empty = PrecursorSignal(
        direction=0, eta_bars=0, eta_minutes=0.0, confidence=0.0, gap=0.0,
        slope=0.0, atr=0.0, normalized_gap=0.0, fast_name=fast_name,
        slow_name=slow_name,
    )
    if fast is None or slow is None or len(fast) < 3:
        return empty

    fast = pd.Series(fast).astype("float64")
    slow = pd.Series(slow).astype("float64")
    if fast.isna().all() or slow.isna().all():
        return empty

    gap_series = (fast - slow).dropna()
    if len(gap_series) < 3:
        return empty

    atr_series = ind.atr(data["high"], data["low"], data["close"], atr_period) if {
        "high", "low", "close"
    }.issubset(data.columns) else pd.Series(np.nan, index=data.index)
    atr_value = float(atr_series.dropna().iloc[-1]) if atr_series.notna().any() else 0.0
    price = float(data["close"].iloc[-1]) if len(data) else 0.0
    bar_index = int(len(data) - 1)

    slope_series = _slope(gap_series, slope_lookback)
    slope, accelerated = _projection_slope(gap_series, slope_lookback)
    gap = float(gap_series.iloc[-1])

    scale = atr_value if atr_value > 0 else max(abs(price) * 0.001, 1e-9)
    normalized_gap = abs(gap) / scale

    reasons: list[str] = []

    if abs(gap) < 1e-12:
        # Lines are touching right now: the cross is happening.
        return PrecursorSignal(
            direction=0, eta_bars=0, eta_minutes=0.0, confidence=0.0, gap=gap,
            slope=slope, atr=atr_value, normalized_gap=normalized_gap,
            fast_name=fast_name, slow_name=slow_name, price=price, bar_index=bar_index,
            reasons=["lines are touching; cross is current"],
        )

    # No projection is possible when the gap is not closing.
    if _sign(slope) == 0 or _sign(slope) == _sign(gap):
        return PrecursorSignal(
            direction=0, eta_bars=0, eta_minutes=0.0, confidence=0.0, gap=gap,
            slope=slope, atr=atr_value, normalized_gap=normalized_gap,
            fast_name=fast_name, slow_name=slow_name, price=price, bar_index=bar_index,
            reasons=["lines are diverging" if _sign(slope) == _sign(gap) else "gap is flat"],
        )

    eta = abs(gap) / abs(slope)
    eta_bars = int(np.ceil(eta))
    # Direction: gap is closing from + to - => bearish cross coming.
    direction = -_sign(gap)

    if eta_bars < 1:
        eta_bars = 1

    reasons.append(f"gap {gap:+.6g} closing at {slope:+.6g}/bar")
    if accelerated:
        reasons.append("closing speed is accelerating (using current speed)")
    reasons.append(f"projected cross in {eta_bars} bar(s)")

    # ---- confidence ----------------------------------------------------
    # 1) proximity: closer gap => more time for it to be invalidated but also
    #    less price risk. Reward small normalised gaps.
    proximity = float(np.clip(1.0 - (normalized_gap / 3.0), 0.0, 1.0))

    # 2) horizon fit
    horizon_fit = 1.0 if eta_bars <= horizon_bars else float(
        np.clip(1.0 - (eta_bars - horizon_bars) / max(horizon_bars * 3, 1), 0.0, 1.0)
    )
    if eta_bars > horizon_bars:
        reasons.append(f"outside {horizon_bars}-bar alert horizon")

    # 3) slope stability: the projection must be consistent over recent bars
    recent = slope_series.dropna().tail(max(2, slope_lookback))
    if len(recent) >= 2:
        same_direction = float(np.mean([_sign(v) == _sign(slope) for v in recent.to_numpy()]))
    else:
        same_direction = 0.5
    if same_direction >= 0.8:
        reasons.append("slope stable across recent bars")

    # 4) momentum confirmation
    momentum_score = 0.5
    if confirm_rsi is not None and len(confirm_rsi.dropna()):
        rsi_last = float(confirm_rsi.dropna().iloc[-1])
        if direction > 0:
            momentum_score = float(np.clip((rsi_last - 40.0) / 30.0, 0.0, 1.0))
            if rsi_last >= 50:
                reasons.append(f"RSI {rsi_last:.1f} supports long")
        else:
            momentum_score = float(np.clip((60.0 - rsi_last) / 30.0, 0.0, 1.0))
            if rsi_last <= 50:
                reasons.append(f"RSI {rsi_last:.1f} supports short")

    confidence = float(
        np.clip(
            0.40 * proximity + 0.25 * horizon_fit + 0.20 * same_direction + 0.15 * momentum_score,
            0.0,
            1.0,
        )
    )

    projected_price = price
    if "close" in data.columns and len(data) >= 3:
        close_series = data["close"].astype("float64")
        price_slope = float(_slope(close_series, slope_lookback).iloc[-1])
        if np.isfinite(price_slope):
            projected_price = price + price_slope * eta_bars
    minutes = timeframe_minutes(timeframe) * eta_bars
    reasons.append(f"ETA ~{minutes:g} min on {timeframe}")

    return PrecursorSignal(
        direction=direction,
        eta_bars=eta_bars,
        eta_minutes=minutes,
        confidence=confidence,
        gap=gap,
        slope=slope,
        atr=atr_value,
        normalized_gap=normalized_gap,
        fast_name=fast_name,
        slow_name=slow_name,
        price=price,
        projected_price=float(projected_price),
        bar_index=bar_index,
        horizon_bars=horizon_bars,
        reasons=reasons,
    )


def scan_pairs(
    data: pd.DataFrame,
    pairs: list[tuple[str, pd.Series, pd.Series]],
    *,
    horizon_bars: int = 5,
    timeframe: str = "1d",
    confirm_rsi: pd.Series | None = None,
) -> list[PrecursorSignal]:
    """Run :func:`detect_precursor` over several (name, fast, slow) pairs.

    Returns only actionable projections, best confidence first.
    """
    out: list[PrecursorSignal] = []
    for name, fast, slow in pairs:
        signal = detect_precursor(
            fast,
            slow,
            data,
            horizon_bars=horizon_bars,
            timeframe=timeframe,
            fast_name=f"{name} fast",
            slow_name=f"{name} slow",
            confirm_rsi=confirm_rsi,
        )
        if signal.direction != 0:
            out.append(signal)
    return sorted(out, key=lambda s: (-s.confidence, s.eta_bars))
