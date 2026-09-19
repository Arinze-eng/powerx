---
name: mt5-trading
description: Install, compile MQL5, and trade MetaTrader 5 from the command line inside the user's execution sandbox (Wine + MT5, never on the app host). Use when the user mentions MetaTrader, MT5, MQL5, .mq5/.mqh, EA/Expert Advisor, forex/CFD trading, or a broker account.
metadata: {"nanobot":{"emoji":"📈","requires":{"tools":["mt5_sandbox"],"env":["NOVITA_API_KEY"]}}}
---

# MetaTrader 5 (Wine, sandbox-only)

The `mt5_sandbox` tool drives a headless MetaTrader 5 terminal **inside the
user's execution sandbox**. Wine, the MT5 terminal, and the Python bridge never
touch the application host — that is deliberate, because the Wine prefix alone
is ~800 MB and the terminal is an amd64 GUI app.

## The one rule that matters

**`install` returns immediately.** A full Wine + MT5 + bridge install needs
~10–25 minutes, but every sandbox command is capped (900 s on Novita). So the
installer is launched *detached* and you poll `status` until it finishes.

Do **not** retry `install` in a loop, and do not assume a `status` call that
still says `in_progress` has failed. Poll patiently.

## Canonical workflow

```
1. mt5_sandbox(action="install")            # starts the detached install
2. mt5_sandbox(action="status")             # poll every few minutes
     -> stage="bootstrap"|"wine"|"wineprefix"|"download"|"mt5"|"winpython"|"bridge"
     -> stage="done"  (installed=true)      # proceed
     -> stage="failed"                      # read message + log_tail, fix, retry install
3. mt5_sandbox(action="start", login=<acct>, password=<pw>, server="<broker-server>")
     -> launches terminal64.exe under Xvfb in portable mode with the login seeded
     -> returns ipc_ready=true once the account is live
4. mt5_sandbox(action="account")            # confirm balance/equity/currency
5. mt5_sandbox(action="quote", symbols="EURUSD XAUUSD")
   mt5_sandbox(action="candles", symbol="EURUSD", timeframe="M15", count=200)
```

Quotes and orders **require an account**. A terminal with no login exposes no
symbols over IPC and `start` reports `ipc_ready=false` with a hint.

## Compile loop (MQL5)

```
mt5_sandbox(action="compile", file="/home/user/.mt5/MQL5/Experts/MyEA.mq5")
  -> {"ok": false, "errors": ["MyEA.mq5(42,7) : error 256: ..."], "log": "..."}
```

Read `errors`, edit the source, compile again. `ex5` is non-null only on
success.

### Where sources must live

MetaEditor resolves `#include <Trade/Trade.mqh>` relative to the terminal it is
invoked from. Two consequences, both verified:

* Put sources under the terminal's **MQL5 data tree** (below), and
* If you compile a file from anywhere else, pass `include=` pointing at the
  MQL5 `Include` directory (`--include` on the CLI). Without it you get
  `error 106: file 'Include\Trade\Trade.mqh' not found`, which is a *path*
  problem, not a code problem.

```text
# terminal data dir (where Experts/ and Include/ belong)
~/.wine-mt5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/MQL5/

# install dir (holds the binaries + a stub MQL5/Experts)
~/.wine-mt5/drive_c/Program Files/MetaTrader 5/
```

The installer ships only a stub `MQL5/Experts` — MetaQuotes pushes the full
standard library on first broker sync. A self-contained EA compiles with no
setup; anything using `<Trade/…>`, `<Arrays/…>` etc. needs either that sync or
an `include=` path to a library you provide.

Verified end to end in a sandbox: a self-contained `Plain.mq5` compiled to a
6186-byte `Plain.ex5` with `Result: 0 errors, 0 warnings, 335 ms elapsed,
cpu='X64 Regular'`.

The `.log` MetaEditor writes next to the source is UTF-16LE with a BOM; the CLI
auto-detects UTF-16/UTF-8/Latin-1, so read `errors`/`log` from the JSON rather
than fetching the file yourself.

After compiling an EA, `action="experts"` tails the Experts journal and
`action="logs"` tails the terminal log — together they are the full read/fix
loop for runtime errors.

## Trading

`order`, `close`, and `close_all` move real money and are **disabled unless the
deployment sets `MT5_ALLOW_TRADING=1`**. When disabled they return an error and
never reach the sandbox.

* `order` needs `symbol`, `side` (`buy`/`sell`), and `volume`; optional `sl`,
  `tp`, `deviation`, `comment`.
* `close` needs `ticket`; optional `volume` for a partial close.
* `dry_run=true` prints the exact command that *would* run, without sending —
  use it to show the user what you are about to do.

A rejected order is a normal result, not an exception: the payload carries the
broker `retcode` and `comment`. Common retcodes: `10009` done, `10016` invalid
stops, `10019` no money, `10030` unsupported filling mode.

## Sizing

Wine + MT5 does not fit in the stock ~486 MB sandbox and gets OOM-killed
mid-install. The tool auto-selects a sized template (2 GB by default; override
with `NOVITA_SANDBOX_MEMORY_MB=4096`) and the installer refuses to start below
1800 MB with an explicit fix message rather than dying mysteriously.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **`A debugger has been found running in your system`** | MetaTrader's anti-debug check. Two causes, both handled automatically: **Wine 11 is unusable** (Wine 10 installs in ~30 s), and **any `WINEDEBUG` value** — even `-all` — makes Wine set PEB heap-debug flags MT5 detects. The installer pins Wine 10 across all four wine packages and strips `WINEDEBUG`. Never pass `WINEDEBUG` to an MT5 binary. |
| `status` stuck on `wineprefix` | Wine's first boot does one-time registry work; it is slow but bounded. Keep polling. |
| `stage="failed"`, `insufficient memory` | Sandbox too small. Raise `NOVITA_SANDBOX_MEMORY_MB` and recreate. |
| `start` returns `ipc_ready=false` | No broker login. Pass `login`/`password`/`server` to `start`. |
| `unimplemented function ucrtbase.dll.crealf` | Wine < 9. The installer pins Wine 10; re-run `install`. |
| `bridge_imports_in_wine=false` in `doctor` | Windows python or the `MetaTrader5` wheel failed to install; check `log_tail`. |
| `mt5_cli.py must run inside Wine` | Do not invoke the CLI's bridge actions directly on Linux python — always go through `mt5_sandbox`. |

## Verified facts (measured in a Novita sandbox)

- Wine **10.0** is required. Wine 11.0 fails MetaTrader's anti-debug check.
- `WINEDEBUG` must be **absent** from the environment of every MT5 process.
- `WINEDLLOVERRIDES=mscoree,mshtml=` makes `wineboot` finish in **~6 s** instead
  of wedging for 10+ minutes in setupapi.
- Total install time with the stack above: **~2 minutes** (terminal + MetaEditor
  + Windows Python bridge), producing
  `Program Files/MetaTrader 5/terminal64.exe` and `MetaEditor64.exe`.