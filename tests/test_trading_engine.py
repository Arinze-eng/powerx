"""Tests for the trading config and strategy engine."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from nanobot.trading.config import AgentConfig, RiskConfig, SessionWindow
from nanobot.trading.risk_manager import RiskManager
from nanobot.trading.strategy_router import StrategyRouter


def _ohlcv_frame(days: int = 60, start="2024-01-01") -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame sharing a common day index."""
    idx = pd.date_range(start, periods=days, freq="D", tz="UTC")
    rng = np.random.default_rng(7)
    closes = pd.Series(100 + np.cumsum(rng.normal(0, 1, days)), index=idx)
    frame = pd.DataFrame({
        "open": closes.shift(1).fillna(closes.iloc[0]),
        "high": closes + 1.0,
        "low": closes - 1.0,
        "close": closes,
        "volume": 1_000_000.0,
    }, index=idx)
    return frame


def test_default_config():
    config = AgentConfig()
    assert "EURGBP" in config.pairs
    assert config.timeframe == "1h"
    assert config.risk.risk_fraction == 0.0025
    assert config.risk.daily_loss_cap_r == 5.0
    assert config.risk.target_r == 2.0


def test_session_window_contains():
    london = SessionWindow("London", 7, 10, True)
    assert london.contains(8) is True
    assert london.contains(10) is False
    assert london.contains(6) is False


def test_session_window_disabled():
    asia = SessionWindow("Asia", 1, 5, False)
    assert asia.contains(3) is False


def test_config_session_for():
    config = AgentConfig()
    ts = datetime(2024, 6, 15, 8, 0, tzinfo=timezone.utc)
    assert config.session_for(ts) == "London"
    ts2 = datetime(2024, 6, 15, 13, 0, tzinfo=timezone.utc)
    assert config.session_for(ts2) == "New York"
    ts3 = datetime(2024, 6, 15, 20, 0, tzinfo=timezone.utc)
    assert config.session_for(ts3) is None


def test_risk_manager_approve():
    config = RiskConfig()
    rm = RiskManager(config, starting_equity=100_000.0)
    from datetime import date

    decision = rm.approve(date(2024, 1, 1), 100_000.0, 1.1000, 1.0950)
    assert decision.allowed is True
    assert decision.risk_dollars == pytest.approx(250.0)
    assert decision.quantity > 0


def test_risk_manager_daily_loss_lock():
    config = RiskConfig()
    rm = RiskManager(config, starting_equity=100_000.0)
    from datetime import date

    rm.record_realized_r(date(2024, 1, 1), -3.0)
    decision = rm.approve(date(2024, 1, 1), 100_000.0, 1.1000, 1.0950)
    assert decision.allowed is True  # -3R, not yet at -5R

    rm.record_realized_r(date(2024, 1, 1), -3.0)
    decision = rm.approve(date(2024, 1, 1), 100_000.0, 1.1000, 1.0950)
    assert decision.allowed is False
    assert "daily loss lock" in decision.reason


def test_risk_manager_zero_stop_distance():
    config = RiskConfig()
    rm = RiskManager(config, starting_equity=100_000.0)
    from datetime import date

    decision = rm.approve(date(2024, 1, 1), 100_000.0, 1.1000, 1.1000)
    assert decision.allowed is False
    assert "stop distance is zero" in decision.reason


def test_strategy_router_awd_below_threshold():
    from nanobot.trading.analyst_agent import AnalystSignal
    from nanobot.trading.scout_agent import ScoutFeatures

    router = StrategyRouter(min_awd=0.65)
    analyst = AnalystSignal("Expansion", 0.3, 0.40, 1, "low confidence")
    scout = ScoutFeatures()
    decision = router.route(analyst, scout, None, 0.0)
    assert decision.cluster is None
    assert "below threshold" in decision.reason


def test_strategy_router_cluster_a():
    from nanobot.trading.analyst_agent import AnalystSignal
    from nanobot.trading.scout_agent import ScoutFeatures

    router = StrategyRouter(min_awd=0.65)
    analyst = AnalystSignal("Retracement", 0.8, 0.80, 1, "high confidence")
    scout = ScoutFeatures(
        fvg_direction=0,
        order_block_direction=0,
        bos_direction=0,
        choch_direction=-1,
        liquidity_sweep=True,
        displacement=True,
        killzone="London",
        sponsorship=True,
    )
    decision = router.route(analyst, scout, 0.0, 0.0)
    assert decision.cluster == "A - Institutional Reversal"
    assert decision.direction == -1


def test_strategy_router_cluster_d():
    from nanobot.trading.analyst_agent import AnalystSignal
    from nanobot.trading.scout_agent import ScoutFeatures

    router = StrategyRouter(min_awd=0.65, tma_threshold=0.2)
    analyst = AnalystSignal("Consolidation", 0.8, 0.80, 1, "consolidation")
    scout = ScoutFeatures(killzone="London")
    decision = router.route(analyst, scout, 0.5, 0.5)
    assert decision.cluster == "D - Correlation Basket"
    assert decision.direction == -1  # positive slope → short


def test_backtest_engine_run_single_pair():
    """Backtest.run() must not fail on a single instrument timeline."""
    from nanobot.trading.backtest_engine import BacktestEngine

    engine = BacktestEngine(AgentConfig(), starting_equity=100_000.0)
    data = {"EURGBP": _ohlcv_frame(days=90)}
    result = engine.run(data)
    assert result["trades"] is not None
    assert result["decisions"]
    assert result["equity"]
    assert len(result["equity"]) >= 2


def test_backtest_engine_run_multi_pair_shared_timeline():
    """Backtest.run() must intersect shared timestamps across instruments."""
    from nanobot.trading.backtest_engine import BacktestEngine

    engine = BacktestEngine(AgentConfig(), starting_equity=100_000.0)
    data = {
        "EURGBP": _ohlcv_frame(days=90),
        "EURCAD": _ohlcv_frame(days=90),
        "GBPCAD": _ohlcv_frame(days=90),
    }
    result = engine.run(data)
    assert result["trades"] is not None
    assert len(result["equity"]) >= 2
