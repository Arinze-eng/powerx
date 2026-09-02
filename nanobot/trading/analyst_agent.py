"""Regime and confidence analysis using a causal HMM window."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    from hmmlearn.hmm import GaussianHMM
except ImportError:  # pragma: no cover
    GaussianHMM = None


@dataclass(frozen=True)
class AnalystSignal:
    regime: str
    regime_confidence: float
    awd: float
    directional_bias: int
    reason: str


class AnalystAgent:
    def __init__(
        self,
        n_states: int = 3,
        min_samples: int = 120,
        lookback: int = 500,
        refit_interval: int = 12,
    ):
        self.n_states = n_states
        self.min_samples = min_samples
        self.lookback = lookback
        self.refit_interval = max(1, refit_interval)
        self._cache: dict[str, tuple[int, GaussianHMM | None, dict[int, str]]] = {}

    def analyze(
        self,
        frame: pd.DataFrame,
        tma_slope: float | None,
        threshold: float,
        cache_key: str = "default",
    ) -> AnalystSignal:
        returns = np.log(frame["close"]).diff().replace([np.inf, -np.inf], np.nan).dropna()
        if len(returns) < self.min_samples:
            return AnalystSignal("Unclassified", 0.0, 0.0, 0, "insufficient closed bars for HMM")

        sample = returns.tail(self.lookback).to_numpy().reshape(-1, 1)
        try:
            cached = self._cache.get(cache_key)
            if cached and len(returns) - cached[0] < self.refit_interval:
                model, labels = cached[1], cached[2]
            else:
                if GaussianHMM is None:
                    return AnalystSignal("Unclassified", 0.0, 0.0, 0, "hmmlearn not installed")
                model = GaussianHMM(
                    n_components=self.n_states,
                    covariance_type="full",
                    n_iter=100,
                    random_state=7,
                )
                model.fit(sample)
                means = model.means_.ravel()
                order = np.argsort(means)
                labels = {int(order[0]): "Retracement", int(order[-1]): "Expansion"}
                labels.update(
                    {state: "Consolidation" for state in range(self.n_states) if state not in labels}
                )
                self._cache[cache_key] = (len(returns), model, labels)
            states = model.predict(sample)
            state = int(states[-1])
            label = labels[state]
            posterior = model.predict_proba(sample)[-1]
            confidence = float(np.clip(posterior[state], 0.0, 1.0))
        except (ValueError, np.linalg.LinAlgError) as exc:
            return AnalystSignal("Unclassified", 0.0, 0.0, 0, f"HMM unavailable for this window: {exc}")

        slope_strength = min(abs(tma_slope or 0.0) / max(threshold, 1e-9), 1.0)
        recent_volatility = float(returns.tail(20).std() or 0.0)
        long_run_volatility = float(returns.tail(min(len(returns), 100)).std() or 0.0)
        volatility_score = (
            min(recent_volatility / long_run_volatility, 2.0) / 2.0
            if long_run_volatility > 0
            else 0.0
        )
        awd = float(np.clip(0.45 * confidence + 0.35 * slope_strength + 0.20 * volatility_score, 0.0, 1.0))

        if tma_slope is not None and abs(tma_slope) >= threshold:
            directional_bias = -1 if tma_slope > 0 else 1
        else:
            directional_bias = 1 if float(returns.tail(5).sum()) > 0 else -1
        return AnalystSignal(
            label,
            confidence,
            awd,
            directional_bias,
            f"HMM={label}, confidence={confidence:.2f}, causal AWD={awd:.2f}",
        )
