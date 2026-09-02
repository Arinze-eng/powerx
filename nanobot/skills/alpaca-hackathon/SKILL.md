---
name: alpaca-hackathon
description: ICT/SMC + TMA + HMM five-cluster trading strategy playbook
version: 1.0.0
---

# Alpaca Hackathon Trading Strategy

This skill provides the strategy knowledge for the alpaca_trade tool.
Use this when a user asks about trading analysis, strategy decisions,
or wants to understand what the five-cluster engine is doing.

## Five Strategy Clusters

Each closed bar is routed into the best-fitting cluster:

### A — Institutional Reversal
Looks for a liquidity sweep followed by a Change of Character (CHoCH),
with sponsorship context. Trades the reversal direction.
**Inputs:** ICT liquidity, SMC CHoCH, displacement/sponsorship.

### B — Trend Expansion
Follows a confirmed Break of Structure (BoS) when the HMM identifies
an Expansion regime and the candle shows strong displacement.
**Inputs:** SMC BoS, Markov regime, displacement.

### C — Value Retracement
Looks for a Fair Value Gap (FVG) / inducement setup during a London
or New York killzone, using the dealing range to distinguish premium
and discount.
**Inputs:** ICT FVG, SMC inducement, premium/discount, session.

### D — Correlation Basket
Uses the TMA slope as a mean-reversion signal when the market is in
Consolidation and the configured pair basket is correlated.
Positive slope → short. Negative slope → long.
**Inputs:** MQL4 TMA port, ATR, HMM regime, basket correlation.

### E — Range Liquidity
Trades only in an enabled institutional killzone during Consolidation,
using the analyst's directional bias rather than forcing a trend trade.
**Inputs:** ICT session, HMM regime.

## Supporting Pillars

- **ICT** is the timer and liquidity context: London/New York killzones,
  sweeps, and fair-value-gap context.
- **SMC** is the market-structure map: BoS, CHoCH, order blocks, and
  inducement are read from the smartmoneyconcepts package.
- **Markov/HMM** is the environment classifier: it labels the current
  market Expansion, Retracement, or Consolidation.
- **Institutional Sponsorship (IS)** is a force filter: uses a
  displacement/volume-compatible proxy.
- **TMA** is the basket mean-reversion pillar: the only direct signal
  in Cluster D; its normalized slope also contributes to AWD confidence.

## Risk Parameters

- **Risk fraction:** 0.25% of equity per trade
- **Daily loss lock:** 5R (stops trading for the day)
- **Breakeven trigger:** 0.75R (move stop to entry)
- **Target:** 2R
- **Stop:** 1.0 × ATR
- **Max spread:** 3.0 pips
- **AWD confidence threshold:** 0.65 minimum

## Killzone Hours (UTC)

- **London:** 07:00 – 10:00
- **New York:** 12:00 – 15:00
- **Asia:** 01:00 – 05:00 (disabled by default)

## TMA Slope Interpretation

- **Positive slope** → price is above the TMA center → mean reversion
  signals SHORT in Cluster D
- **Negative slope** → price is below the TMA center → mean reversion
  signals LONG in Cluster D
- **|slope| ≥ 0.2** → TMA extreme, eligible for Cluster D

## HMM Regime Definitions

- **Expansion:** High volatility, strong directional moves
- **Retracement:** Counter-trend pullback within a trend
- **Consolidation:** Low volatility, range-bound price action

## Usage

When a user asks to analyze a pair, the tool runs the full five-cluster
engine on historical bars and reports the latest regime, AWD confidence,
TMA slope, basket correlation, and the routed cluster with direction.

When a user asks to backtest, the tool runs the event-driven backtest
over the specified date range and reports trades, win rate, total R,
max drawdown, and in-sample/out-of-sample split.

**This is technical research, not financial advice or a promise of
profitability. Paper trading only.**
