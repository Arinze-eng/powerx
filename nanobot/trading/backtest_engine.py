"""Event-driven, closed-bar-causal backtest engine."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from nanobot.trading.analyst_agent import AnalystAgent
from nanobot.trading.config import AgentConfig
from nanobot.trading.risk_manager import RiskManager
from nanobot.trading.scout_agent import ScoutAgent
from nanobot.trading.strategy_router import StrategyRouter
from nanobot.trading.tma_engine import basket_correlation, legacy_slope, wilder_atr


@dataclass
class OpenPosition:
    pair: str
    direction: int
    cluster: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry: float
    stop: float
    initial_stop: float
    target: float
    risk_dollars: float
    quantity: float
    breakeven_moved: bool = False


@dataclass
class Trade:
    pair: str
    direction: int
    cluster: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    initial_stop: float
    target: float
    r_multiple: float
    pnl: float
    exit_reason: str


class BacktestEngine:
    def __init__(self, config: AgentConfig, starting_equity: float = 100_000.0):
        self.config = config
        self.starting_equity = float(starting_equity)
        self.analyst = AnalystAgent(
            config.hmm_states,
            config.hmm_min_samples,
            config.hmm_lookback,
            config.hmm_refit_interval,
        )
        self.scout = ScoutAgent(
            config.swing_length,
            config.displacement_atr_multiple,
            config.fvg_min_atr_fraction,
            config.sessions,
            config.scout_refit_interval,
        )
        self.router = StrategyRouter(config.min_awd, config.tma_threshold)
        self.risk = RiskManager(config.risk, starting_equity)

    def run(self, market_data: dict[str, pd.DataFrame]) -> dict:
        if len(market_data) < 1:
            raise ValueError("Backtest requires at least one instrument.")
        working = list(set(frame.index) for frame in market_data.values())
        timeline = sorted(set.intersection(*working))
        if len(timeline) < 2:
            raise ValueError("Instruments do not share at least two timestamps.")

        positions: dict[str, OpenPosition] = {}
        pending: dict[str, dict] = {}
        decisions: list[dict] = []
        trades: list[Trade] = []
        equity = self.starting_equity
        equity_rows = [{"timestamp": timeline[0].isoformat(), "equity": equity}]

        for time_index, timestamp in enumerate(timeline):
            for pair, signal in list(pending.items()):
                if pair in positions:
                    continue
                row = market_data[pair].loc[timestamp]
                entry = float(row["open"])
                frame = market_data[pair].loc[:timestamp]
                atr = wilder_atr(frame, self.config.atr_period).iloc[-1]
                if not np.isfinite(atr) or atr <= 0:
                    decisions.append(
                        {"timestamp": timestamp.isoformat(), "pair": pair, "action": "skip", "reason": "ATR unavailable at fill"}
                    )
                    continue
                stop_distance = float(atr * self.config.risk.stop_atr_multiple)
                stop = entry - signal["direction"] * stop_distance
                approval = self.risk.approve(timestamp.date(), equity, entry, stop, spread_pips=None)
                if not approval.allowed:
                    decisions.append(
                        {"timestamp": timestamp.isoformat(), "pair": pair, "action": "skip", "reason": approval.reason}
                    )
                    continue
                target = entry + signal["direction"] * stop_distance * self.config.risk.target_r
                positions[pair] = OpenPosition(
                    pair=pair,
                    direction=signal["direction"],
                    cluster=signal["cluster"],
                    signal_time=signal["signal_time"],
                    entry_time=timestamp,
                    entry=entry,
                    stop=stop,
                    initial_stop=stop,
                    target=target,
                    risk_dollars=approval.risk_dollars,
                    quantity=approval.quantity,
                )
                decisions.append(
                    {
                        "timestamp": timestamp.isoformat(),
                        "pair": pair,
                        "action": "enter",
                        "cluster": signal["cluster"],
                        "reason": signal["reason"],
                    }
                )
            pending = {}

            for pair, position in list(positions.items()):
                row = market_data[pair].loc[timestamp]
                favourable = (
                    float(row["high"] - position.entry)
                    if position.direction == 1
                    else float(position.entry - row["low"])
                )
                stop_distance = abs(position.entry - position.initial_stop)
                if not position.breakeven_moved and favourable >= stop_distance * self.config.risk.breakeven_trigger_r:
                    position.stop = position.entry
                    position.breakeven_moved = True
                stop_hit = float(row["low"]) <= position.stop if position.direction == 1 else float(row["high"]) >= position.stop
                target_hit = (
                    float(row["high"]) >= position.target
                    if position.direction == 1
                    else float(row["low"]) <= position.target
                )
                if stop_hit or target_hit:
                    exit_price = position.stop if stop_hit else position.target
                    reason = "stop_or_breakeven" if stop_hit else "target"
                    r_multiple = position.direction * (exit_price - position.entry) / stop_distance
                    pnl = position.risk_dollars * r_multiple
                    trades.append(
                        Trade(
                            pair,
                            position.direction,
                            position.cluster,
                            position.signal_time.isoformat(),
                            position.entry_time.isoformat(),
                            timestamp.isoformat(),
                            position.entry,
                            exit_price,
                            position.initial_stop,
                            position.target,
                            float(r_multiple),
                            float(pnl),
                            reason,
                        )
                    )
                    equity += pnl
                    self.risk.record_realized_r(timestamp.date(), r_multiple)
                    del positions[pair]

            for pair, frame in market_data.items():
                visible = frame.loc[:timestamp]
                current_index = len(visible) - 1
                slope = legacy_slope(
                    visible,
                    current_index,
                    self.config.tma_period,
                    self.config.atr_period,
                    self.config.tma_atr_shift,
                )
                analyst = self.analyst.analyze(visible, slope, self.config.tma_threshold, cache_key=pair)
                scout = self.scout.extract(visible, cache_key=pair)
                pair_frames = {
                    name: data.loc[:timestamp]
                    for name, data in market_data.items()
                    if timestamp in data.index
                }
                correlation = basket_correlation(pair_frames)
                route = self.router.route(analyst, scout, slope, correlation)
                reason = f"{analyst.reason}; {scout.reason}; route={route.reason}"
                decisions.append(
                    {
                        "timestamp": timestamp.isoformat(),
                        "pair": pair,
                        "action": "signal" if route.cluster else "no_trade",
                        "cluster": route.cluster or "",
                        "direction": route.direction,
                        "awd": analyst.awd,
                        "regime": analyst.regime,
                        "tma_slope": slope,
                        "basket_correlation": correlation,
                        "awd_passed": analyst.awd >= self.config.min_awd,
                        "regime_classified": analyst.regime in {"Expansion", "Retracement", "Consolidation"},
                        "scout_ready": "insufficient" not in scout.reason,
                        "fvg_detected": bool(scout.fvg_direction),
                        "order_block_detected": bool(scout.order_block_direction),
                        "bos_detected": bool(scout.bos_direction),
                        "choch_detected": bool(scout.choch_direction),
                        "liquidity_sweep_detected": bool(scout.liquidity_sweep),
                        "displacement_detected": bool(scout.displacement),
                        "killzone_active": bool(scout.killzone),
                        "sponsorship_detected": bool(scout.sponsorship),
                        "inducement_detected": bool(scout.inducement),
                        "tma_extreme": slope is not None and abs(slope) >= self.config.tma_threshold,
                        "basket_correlation_passed": correlation >= 0.25,
                        "reason": reason,
                    }
                )
                if route.cluster and route.direction and pair not in positions and time_index < len(timeline) - 1:
                    pending[pair] = {
                        "direction": route.direction,
                        "cluster": route.cluster,
                        "signal_time": timestamp,
                        "reason": route.reason,
                    }
                equity_rows.append({"timestamp": timestamp.isoformat(), "equity": equity})

        final_time = timeline[-1]
        for pair, position in positions.items():
            exit_price = float(market_data[pair].loc[final_time, "close"])
            stop_distance = abs(position.entry - position.initial_stop)
            r_multiple = position.direction * (exit_price - position.entry) / stop_distance
            pnl = position.risk_dollars * r_multiple
            trades.append(
                Trade(
                    pair,
                    position.direction,
                    position.cluster,
                    position.signal_time.isoformat(),
                    position.entry_time.isoformat(),
                    final_time.isoformat(),
                    position.entry,
                    exit_price,
                    position.initial_stop,
                    position.target,
                    float(r_multiple),
                    float(pnl),
                    "end_of_data",
                )
            )
            equity += pnl
            self.risk.record_realized_r(final_time.date(), r_multiple)
        equity_rows[-1]["equity"] = equity
        return {"trades": trades, "decisions": decisions, "equity": equity_rows}

    def write_results(self, result: dict, output_dir: str | Path | None = None) -> dict:
        output = Path(output_dir or self.config.results_dir)
        output.mkdir(parents=True, exist_ok=True)
        trades = pd.DataFrame([asdict(t) for t in result["trades"]])
        decisions = pd.DataFrame(result["decisions"])
        equity = pd.DataFrame(result["equity"])
        trades.to_csv(output / "trades.csv", index=False)
        decisions.to_csv(output / "decisions.csv", index=False)
        equity.to_csv(output / "equity_curve.csv", index=False)
        equity["timestamp"] = pd.to_datetime(equity["timestamp"])
        plt.figure(figsize=(11, 5))
        plt.plot(equity["timestamp"], equity["equity"], color="#1f77b4", linewidth=1.5)
        plt.title("Alpaca Agent Research Backtest Equity Curve")
        plt.xlabel("UTC time")
        plt.ylabel("Account equity ($)")
        plt.grid(alpha=0.25)
        plt.tight_layout()
        plt.savefig(output / "equity_curve.png", dpi=140)
        plt.close()
        summary = self._summaries(trades, equity)
        diagnostics = self._diagnostics(decisions)
        (output / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2))
        pd.DataFrame(
            [{"metric": m, "count": c} for m, c in diagnostics.items()]
        ).to_csv(output / "diagnostics.csv", index=False)
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        pd.DataFrame(summary).T.to_csv(output / "summary.csv")
        return summary

    def _diagnostics(self, decisions: pd.DataFrame) -> dict[str, int]:
        if decisions.empty or "awd" not in decisions:
            return {"evaluation_rows": 0}
        evaluations = decisions.loc[decisions["awd"].notna()].copy()
        diagnostics: dict[str, int] = {
            "evaluation_rows": int(len(evaluations)),
            "awd_passed": int(evaluations["awd_passed"].fillna(False).sum()),
            "regime_classified": int(evaluations["regime_classified"].fillna(False).sum()),
            "scout_ready": int(evaluations["scout_ready"].fillna(False).sum()),
        }
        for column in (
            "fvg_detected",
            "order_block_detected",
            "bos_detected",
            "choch_detected",
            "liquidity_sweep_detected",
            "displacement_detected",
            "killzone_active",
            "sponsorship_detected",
            "inducement_detected",
            "tma_extreme",
            "basket_correlation_passed",
        ):
            if column in evaluations:
                diagnostics[column] = int(evaluations[column].fillna(False).sum())
        diagnostics["routed_signals"] = int((evaluations["action"] == "signal").sum()) if "action" in evaluations else 0
        diagnostics["no_trade_routes"] = int((evaluations["action"] == "no_trade").sum()) if "action" in evaluations else 0
        for cluster in (
            "A - Institutional Reversal",
            "B - Trend Expansion",
            "C - Value Retracement",
            "D - Correlation Basket",
            "E - Range Liquidity",
        ):
            key = cluster.split(" - ")[0].lower() + "_signals"
            if "cluster" in evaluations:
                diagnostics[key] = int((evaluations["cluster"] == cluster).sum())
        return diagnostics

    def _summaries(self, trades: pd.DataFrame, equity: pd.DataFrame) -> dict:
        if trades.empty:
            return {"combined": {"trades": 0}}
        timestamps = pd.to_datetime(equity["timestamp"])
        split = timestamps.iloc[0] + (timestamps.iloc[-1] - timestamps.iloc[0]) * 0.8
        segments = {
            "in_sample": (None, split),
            "out_of_sample": (split, None),
            "combined": (None, None),
        }
        result = {}
        for name, (start, end) in segments.items():
            part = trades.copy()
            if not part.empty:
                entry_times = pd.to_datetime(part["entry_time"])
                if start is not None:
                    part = part.loc[entry_times >= start]
                if end is not None:
                    part = part.loc[entry_times < end]
            if part.empty:
                result[name] = {"trades": 0}
                continue
            r = part["r_multiple"]
            curve = equity["equity"]
            mask = pd.Series(True, index=equity.index)
            ts = timestamps
            if start is not None:
                mask &= ts >= start
            if end is not None:
                mask &= ts < end
            curve = equity.loc[mask, "equity"]
            peak = curve.cummax() if not curve.empty else pd.Series(dtype=float)
            drawdown = ((curve - peak) / peak).min() if not curve.empty else 0.0
            sharpe = (r.mean() / r.std() * np.sqrt(len(r))) if len(r) > 1 and r.std() else 0.0
            result[name] = {
                "trades": int(len(r)),
                "wins": int((r > 0).sum()),
                "losses": int((r <= 0).sum()),
                "win_rate": float((r > 0).mean()) if len(r) else 0.0,
                "total_r": float(r.sum()) if len(r) else 0.0,
                "average_r_per_trade": float(r.mean()) if len(r) else 0.0,
                "max_drawdown_pct": float(drawdown * 100) if not curve.empty else 0.0,
                "sharpe_trade_r": float(sharpe),
            }
        return result
