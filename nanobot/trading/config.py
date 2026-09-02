"""Central configuration for the trading research agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_PAIRS = ("EURGBP", "EURCAD", "NZDCHF", "CADCHF", "GBPCAD", "GBPCHF")


@dataclass(frozen=True)
class SessionWindow:
    name: str
    start_hour: int
    end_hour: int
    enabled: bool = True

    def contains(self, hour: int) -> bool:
        return self.enabled and self.start_hour <= hour < self.end_hour


@dataclass(frozen=True)
class RiskConfig:
    risk_fraction: float = 0.0025
    daily_loss_cap_r: float = 5.0
    breakeven_trigger_r: float = 0.75
    stop_atr_multiple: float = 1.0
    target_r: float = 2.0
    max_spread_pips: float = 3.0


@dataclass(frozen=True)
class AlpacaConfig:
    """Alpaca paper-trading configuration.

    Credentials are resolved from environment variables at startup.
    Per-user keys stored in Supabase override these defaults.
    """

    api_key: str = ""
    secret_key: str = ""
    base_url: str = "https://paper-api.alpaca.markets"
    enabled: bool = True


@dataclass(frozen=True)
class AgentConfig:
    pairs: tuple[str, ...] = DEFAULT_PAIRS
    timeframe: str = "1h"
    start: str | None = None
    end: str | None = None
    data_dir: Path | None = None
    results_dir: Path = Path("results")
    tma_period: int = 20
    atr_period: int = 100
    tma_threshold: float = 0.2
    tma_atr_shift: int = 11
    hmm_states: int = 3
    hmm_min_samples: int = 120
    hmm_lookback: int = 500
    hmm_refit_interval: int = 12
    swing_length: int = 10
    scout_refit_interval: int = 4
    min_awd: float = 0.65
    fvg_min_atr_fraction: float = 0.10
    displacement_atr_multiple: float = 1.5
    session_observation_minutes: int = 60
    sessions: tuple[SessionWindow, ...] = field(
        default_factory=lambda: (
            SessionWindow("London", 7, 10, True),
            SessionWindow("New York", 12, 15, True),
            SessionWindow("Asia", 1, 5, False),
        )
    )
    risk: RiskConfig = field(default_factory=RiskConfig)
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentConfig":
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
        sessions = tuple(
            SessionWindow(
                name=item["name"],
                start_hour=int(item["start_hour"]),
                end_hour=int(item["end_hour"]),
                enabled=bool(item.get("enabled", True)),
            )
            for item in raw.pop("sessions", [])
        )
        risk_raw = raw.pop("risk", {})
        risk = RiskConfig(**risk_raw)
        alpaca_raw = raw.pop("alpaca", {})
        alpaca = AlpacaConfig(**alpaca_raw)
        if sessions:
            raw["sessions"] = sessions
        if "pairs" in raw:
            raw["pairs"] = tuple(raw["pairs"])
        if "data_dir" in raw and raw["data_dir"]:
            raw["data_dir"] = Path(raw["data_dir"])
        if "results_dir" in raw:
            raw["results_dir"] = Path(raw["results_dir"])
        raw["risk"] = risk
        raw["alpaca"] = alpaca
        return cls(**raw)

    def session_for(self, timestamp) -> str | None:
        hour = timestamp.hour
        for session in self.sessions:
            if session.contains(hour):
                return session.name
        return None
