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
balance, or the market, handle it as a normal task — there are no slash commands for trading.

**Live account actions** (fast, use the `alpaca_trade` tool directly):
- "Buy 5 shares of MSFT" → `alpaca_trade` with `action=buy`, `symbol=MSFT`, `qty=5`
- "Sell half my TSLA" → `alpaca_trade` with `action=sell`, `symbol=TSLA`, `qty=<qty>`
- "What am I holding?" → `alpaca_trade` with `action=positions`
- "How much money do I have?" → `alpaca_trade` with `action=account`
- "Close my position in AAPL" → `alpaca_trade` with `action=close`, `symbol=AAPL`

**Analysis & backtesting** (CPU-heavy) — **do NOT run these on the web server.** Treat them as
ordinary coding tasks and execute them in the isolated execution backend using the
`novita_sandbox` tool (Novita Sandbox or VPS, whichever the admin selected): write a Python
script that pulls data (e.g. yfinance / Alpaca history) and computes indicators or runs a
backtest, then `run` it there and report the results. This keeps the service responsive.

Trading is **unrestricted** — execute whatever the user asks for: any symbol, any quantity,
market or limit orders, open and close positions freely. Do not add your own size limits,
minimums, or "are you sure" gates; if the user asks to buy, sell, or close, do it with the given
quantity and parameters. There are no per-trade risk ceilings on your side. This is paper
trading — no real money moves.

**This is technical research, not financial advice or a promise of
profitability. Paper trading only.**
# you can trade with alpaca fully if given live account or paper you should be able to do it ,paper isn't your limitations 

#you should be able to trade give real time feedback from alpaca, this are some libraries to fetch data from 

https://paper-api.alpaca.markets/v2
api key: PKVUBWO7D6ZR6USUZNCB2NDKTC (never show this api key to the user)

pip install alpaca-py

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

# No keys required for crypto data
client = CryptoHistoricalDataClient()

# Creating request object
request_params = CryptoBarsRequest(
  symbol_or_symbols=["BTC/USD"],
  timeframe=TimeFrame.Day,
  start=datetime(2022, 9, 1),
  end=datetime(2022, 9, 7)
)

# Retrieve daily bars for Bitcoin in a DataFrame and printing it
btc_bars = client.get_crypto_bars(request_params)

# Convert to dataframe
btc_bars.df

"""
Result

symbol  timestamp                  open      high      low       close      volume       trade_count          vwap
BTC/USD 2022-09-01 05:00:00+00:00  20055.79  20292.00  19564.86  20156.76   7141.975485     110122.0  19934.167845
        2022-09-02 05:00:00+00:00  20156.76  20444.00  19757.72  19919.47   7165.911879      96231.0  20075.200868
        2022-09-03 05:00:00+00:00  19924.83  19968.20  19658.04  19806.11   2677.652012      51551.0  19800.185480
        2022-09-04 05:00:00+00:00  19805.39  20058.00  19587.86  19888.67   4325.678790      62082.0  19834.451414
        2022-09-05 05:00:00+00:00  19888.67  20180.50  19635.96  19760.56   6274.552824      84784.0  19812.095982
        2022-09-06 05:00:00+00:00  19761.39  20026.91  18534.06  18724.59  11217.789784     128106.0  19266.835520
"""
so above is the code and market on how alpaca works 

https://docs.alpaca.markets/docs

this too above is the docs just incase you need something you can reference to it and answer user request 



##this is your main thing you can be using to to backtest,trade etc , see what is done some tools you can use sef 

https://github.com/Phantom2006-dot/Alpaca-Paper-Trading-- 
the double dash in it is the repo so don't remove it , you can read and understand, that the engine to use
