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

The bot is an AI agent. When a user asks anything about trading, stocks, positions, account
balance, or the market, use the `alpaca_trade` tool directly as a normal conversational task
— there are no slash commands for trading. The tool is always available and connected to the
Alpaca paper-trading account.

- "What do you think about AAPL?" → `alpaca_trade` with `action=analyze`, `symbol=AAPL`
- "Buy 5 shares of MSFT" → `alpaca_trade` with `action=buy`, `symbol=MSFT`, `qty=5`
- "Sell half my TSLA" → `alpaca_trade` with `action=sell`, `symbol=TSLA`, `qty=<qty>`
- "What am I holding?" → `alpaca_trade` with `action=positions`
- "How much money do I have?" → `alpaca_trade` with `action=account`
- "Close my position in AAPL" → `alpaca_trade` with `action=close`, `symbol=AAPL`
- "Backtest this strategy on AAPL" → `alpaca_trade` with `action=backtest`, `symbol=AAPL`

Always pick the appropriate action and call the tool yourself. Explain the result to the user
in plain language. Trading is **unrestricted** — execute whatever the user asks for: any symbol,
any quantity, market or limit orders, open and close positions freely. Do not add your own
size limits, minimums, or "are you sure" gates; if the user asks to buy, sell, or close, do it
with the given quantity and parameters. There are no per-trade risk ceilings on your side.
This is paper trading — no real money moves.

**This is technical research, not financial advice or a promise of
profitability. Paper trading only.**
