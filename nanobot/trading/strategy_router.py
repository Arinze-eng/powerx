"""Maps causal analyst and scout features to one of five strategy clusters."""

from __future__ import annotations

from dataclasses import dataclass

from nanobot.trading.analyst_agent import AnalystSignal
from nanobot.trading.scout_agent import ScoutFeatures


@dataclass(frozen=True)
class RouteDecision:
    cluster: str | None
    direction: int
    reason: str


class StrategyRouter:
    def __init__(self, min_awd: float = 0.65, tma_threshold: float = 0.2):
        self.min_awd = min_awd
        self.tma_threshold = tma_threshold

    def route(
        self,
        analyst: AnalystSignal,
        scout: ScoutFeatures,
        tma_slope: float | None,
        basket_correlation: float,
    ) -> RouteDecision:
        if analyst.awd < self.min_awd:
            return RouteDecision(None, 0, f"AWD {analyst.awd:.2f} below threshold {self.min_awd:.2f}")
        if scout.liquidity_sweep and scout.choch_direction:
            direction = scout.choch_direction
            return RouteDecision("A - Institutional Reversal", direction, "sweep + CHoCH + sponsorship context")
        if scout.bos_direction and analyst.regime == "Expansion" and scout.displacement:
            return RouteDecision("B - Trend Expansion", scout.bos_direction, "BoS + Expansion + displacement")
        if scout.fvg_direction and scout.killzone and scout.inducement:
            direction = scout.fvg_direction
            return RouteDecision("C - Value Retracement", direction, "FVG + inducement + killzone")
        if (
            tma_slope is not None
            and abs(tma_slope) >= self.tma_threshold
            and analyst.regime == "Consolidation"
            and basket_correlation >= 0.25
        ):
            direction = -1 if tma_slope > 0 else 1
            return RouteDecision("D - Correlation Basket", direction, "TMA extreme + consolidation + basket correlation")
        if scout.killzone and analyst.regime == "Consolidation":
            return RouteDecision("E - Range Liquidity", analyst.directional_bias, "killzone + consolidation")
        return RouteDecision(None, 0, "no five-cluster confluence on the closed bar")
