"""Risk controls used by both the backtest and the execution layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from nanobot.trading.config import RiskConfig


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    risk_dollars: float = 0.0
    quantity: float = 0.0


class RiskManager:
    def __init__(self, config: RiskConfig, starting_equity: float):
        if starting_equity <= 0:
            raise ValueError("Starting equity must be positive.")
        self.config = config
        self.starting_equity = float(starting_equity)
        self._daily_r: dict[date, float] = {}

    def daily_r(self, day: date) -> float:
        return self._daily_r.get(day, 0.0)

    def record_realized_r(self, day: date, r_multiple: float) -> None:
        self._daily_r[day] = self.daily_r(day) + float(r_multiple)

    def approve(
        self,
        day: date,
        equity: float,
        entry: float,
        stop: float,
        spread_pips: float | None = None,
    ) -> RiskDecision:
        if self.daily_r(day) <= -self.config.daily_loss_cap_r:
            return RiskDecision(False, f"daily loss lock active at {self.daily_r(day):.2f}R")
        if spread_pips is not None and spread_pips > self.config.max_spread_pips:
            return RiskDecision(False, f"spread {spread_pips:.2f} pips exceeds configured filter")
        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            return RiskDecision(False, "stop distance is zero")
        risk_dollars = float(equity * self.config.risk_fraction)
        quantity = risk_dollars / stop_distance
        return RiskDecision(True, "risk gates passed", risk_dollars, quantity)
