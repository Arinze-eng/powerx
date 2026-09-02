"""Causal ICT/SMC feature extraction through the public SMC package."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    from smartmoneyconcepts import smc
except ImportError:  # pragma: no cover
    smc = None


@dataclass(frozen=True)
class ScoutFeatures:
    fvg_direction: int = 0
    order_block_direction: int = 0
    bos_direction: int = 0
    choch_direction: int = 0
    liquidity_sweep: bool = False
    displacement: bool = False
    displacement_ratio: float = 0.0
    premium_discount: float = 0.5
    killzone: str | None = None
    sponsorship: bool = False
    inducement: bool = False
    reason: str = "no confirmed SMC event on the closed bar"


def _direction(value) -> int:
    if value is None or not np.isfinite(value):
        return 0
    return 1 if value > 0 else -1 if value < 0 else 0


def _latest_direction(frame: pd.DataFrame, column: str) -> int:
    values = frame[column].dropna() if column in frame else pd.Series(dtype=float)
    return _direction(values.iloc[-1]) if not values.empty else 0


class ScoutAgent:
    def __init__(
        self,
        swing_length: int = 10,
        displacement_atr_multiple: float = 1.5,
        fvg_min_atr_fraction: float = 0.10,
        sessions: tuple = (),
        refit_interval: int = 4,
    ):
        self.swing_length = swing_length
        self.displacement_atr_multiple = displacement_atr_multiple
        self.fvg_min_atr_fraction = fvg_min_atr_fraction
        self.sessions = sessions
        self.refit_interval = max(1, refit_interval)
        self._cache: dict[str, tuple[int, ScoutFeatures]] = {}

    def extract(self, frame: pd.DataFrame, cache_key: str = "default") -> ScoutFeatures:
        if len(frame) < max(self.swing_length * 2 + 5, 30):
            return ScoutFeatures(reason="insufficient closed bars for SMC confirmation")
        cached = self._cache.get(cache_key)
        if cached and len(frame) - cached[0] < self.refit_interval:
            return cached[1]

        if smc is None:
            return ScoutFeatures(reason="smartmoneyconcepts package not installed")

        history = frame.tail(300).copy()
        swings = smc.swing_highs_lows(history, swing_length=self.swing_length)
        fvg = smc.fvg(history)
        order_blocks = smc.ob(history, swings)
        structure = smc.bos_choch(history, swings)
        liquidity = smc.liquidity(history, swings)

        atr = (
            pd.concat(
                [
                    history["high"] - history["low"],
                    (history["high"] - history["close"].shift()).abs(),
                    (history["low"] - history["close"].shift()).abs(),
                ],
                axis=1,
            )
            .max(axis=1)
            .rolling(14, min_periods=14)
            .mean()
        )
        last = history.iloc[-1]
        body = abs(float(last["close"] - last["open"]))
        candle_range = float(last["high"] - last["low"])
        displacement_ratio = float(body / atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) and atr.iloc[-1] > 0 else 0.0
        displacement = displacement_ratio >= self.displacement_atr_multiple and (
            body / candle_range >= 0.60 if candle_range > 0 else False
        )
        current_atr = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else 0.0
        current_range_high = float(history["high"].tail(50).max())
        current_range_low = float(history["low"].tail(50).min())
        dealing_range = current_range_high - current_range_low
        premium_discount = (
            float((last["close"] - current_range_low) / dealing_range) if dealing_range > 0 else 0.5
        )

        fvg_direction = _latest_direction(fvg, "FVG") if not fvg.empty else 0
        ob_direction = _latest_direction(order_blocks, "OB") if not order_blocks.empty else 0
        bos_direction = _latest_direction(structure, "BOS") if not structure.empty else 0
        choch_direction = _latest_direction(structure, "CHoCH") if not structure.empty else 0
        liquidity_event = liquidity["Liquidity"].dropna() if "Liquidity" in liquidity else pd.Series(dtype=float)
        liquidity_sweep = bool(
            not liquidity_event.empty and _direction(liquidity_event.iloc[-1]) != 0
        )
        fvg_size = 0.0
        if not fvg.empty and np.isfinite(fvg.iloc[-1].get("Top", np.nan)):
            fvg_size = abs(float(fvg.iloc[-1]["Top"] - fvg.iloc[-1]["Bottom"]))
        fvg_valid = fvg_direction != 0 and (current_atr <= 0 or fvg_size >= current_atr * self.fvg_min_atr_fraction)

        timestamp = history.index[-1]
        killzone = next(
            (
                session.name
                for session in self.sessions
                if session.contains(timestamp.hour)
            ),
            None,
        )
        sponsorship = displacement and abs(float(last["close"] - last["open"])) >= max(current_atr, 0.0)
        inducement = bool(fvg_valid and (bos_direction != 0 or choch_direction != 0))
        event_names = [
            name
            for name, active in (
                ("FVG", fvg_valid),
                ("OB", ob_direction != 0),
                ("BoS", bos_direction != 0),
                ("CHoCH", choch_direction != 0),
                ("sweep", liquidity_sweep),
            )
            if active
        ]
        features = ScoutFeatures(
            fvg_direction=fvg_direction if fvg_valid else 0,
            order_block_direction=ob_direction,
            bos_direction=bos_direction,
            choch_direction=choch_direction,
            liquidity_sweep=liquidity_sweep,
            displacement=displacement,
            displacement_ratio=displacement_ratio,
            premium_discount=float(np.clip(premium_discount, 0.0, 1.0)),
            killzone=killzone,
            sponsorship=sponsorship,
            inducement=inducement,
            reason="confirmed closed-bar events: " + (", ".join(event_names) or "none"),
        )
        self._cache[cache_key] = (len(frame), features)
        return features
