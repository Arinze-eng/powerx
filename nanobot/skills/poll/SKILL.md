---
name: poll
description: Real-time polling & vigilance tool — watch anything over time and react, including live trading and any repeated non-trading task
version: 1.0.0
---

# Poll Tool

The `poll` tool lets the agent **watch/poll/monitor something over time and
react in real-time**, using natural-language intent. It is **task-agnostic** —
it is not only for trading. Use it for ANY repeated real-time request.

## When to use `poll` (natural-language intent)

The LLM should trigger `poll` whenever the user asks to:

- **Watch / poll / monitor / keep an eye on / track** something over time.
- **Do something in real-time**.
- Wait for a condition and then **act** ("when it hits X, do Y").
- Check repeatedly until a goal is reached.

### Trading / market examples
- "Buy 1 TSLA whenever it drops to $280" → `action=poll, symbol=TSLA, target_price=280, direction=drop, when_met=buy, qty=1`
- "Sell my BTC at $60000" → `action=poll, symbol=BTCUSD, target_price=60000, direction=above, when_met=sell`
- "Close my AAPL position if it falls below $180" → `action=poll, symbol=AAPL, target_price=180, direction=drop, when_met=close`
- "Tell me when X moves more than 5%" → `action=poll, symbol=X..., move_percent=5`

### Non-trading / generic examples (do these WITHOUT a symbol)
- "Poll every minute until our staging server returns HTTP 200" → `action=poll, description=..., check_goal='staging server returns HTTP 200', interval_seconds=60`
- "Monitor the API and notify me if error rate exceeds 5%" → `action=poll, description=..., check_goal='...', interval_seconds=30`
- "Watch for the report file to appear, then let me know" → `action=poll, description=..., check_goal='report file appears', interval_seconds=15`

For generic (non-trading) tasks, **omit `symbol`** and provide a clear
natural-language `description` + `check_goal`. No broker credentials are needed.

## Actions

- **`poll`** — start a background watch. Returns a Watch ID.
- **`status`** — list active watches and recent run history (progress ticks).
- **`stop`** — cancel an active watch by `watch_id`.

## Parameters

| param | meaning |
|-------|---------|
| `action` | poll / status / stop |
| `symbol` | market symbol (only for price/trading watches) |
| `target_price` | price to cross before acting |
| `direction` | drop/below / above/up / breakout |
| `move_percent` | trigger on a %-move from reference_price |
| `when_met` | notify / buy / sell / close |
| `qty` | quantity for buy/sell |
| `interval_seconds` | poll cadence (default 5s) |
| `max_polls` | auto-stop after N ticks (0 = until done) |
| `description` | natural-language what-to-do (for any task) |
| `check_goal` | generic-task objective to poll toward |
| `label` | human name for the watch |

## Notes

- Polls run in the background as asyncio tasks in the process. Watch state and
  run history are persisted to Supabase (`polling_watches` / `polling_watch_runs`).
- Trading actions resolve the user's **own** credentials from Supabase
  (mirrors `alpaca_trade`); if not connected, use `/alpaca connect`.
- This is **paper trading** by default. Paper trading does not limit you —
  if the user supplies live credentials, trade live with them.