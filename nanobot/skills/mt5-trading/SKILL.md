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

**`install` waits for you now — never hand the wait back to the user.**

A full Wine + MT5 + bridge install needs ~10–25 minutes, but every sandbox command
is capped (900 s on Novita). So the installer runs *detached* and the tool polls
`status` internally until it hits a terminal stage, then returns **once**.

That means:

* **Never** say "it's installing, tell me when to check again."
* **Never** ask "should I check the status now?" — that is the stall this
  playbook exists to prevent.
* **Never** stop at `stage="installing"` and wait for the user to prompt you.
  You own the wait. Poll `status` yourself, in a loop, until `stage="done"`.
* A `status` that still says an in-progress stage has **not** failed. Keep going.

Do **not** retry `install` in a loop either — one call is enough, and re-running it
while an install is live is wasted work.

### When the wait budget runs out

The internal wait is capped (`MT5_INSTALL_WAIT_SECONDS`, default 1500 s). If it
returns `poll_timeout: true`, the install is **progressing, not broken**. Call
`action='status'` again immediately and keep polling. Do not restart, do not
re-run `install`, do not ask the user anything.

### Broker installer URLs are validated

`broker_installer_url` is checked **before** the sandbox is touched. A slug with a
missing TLD (e.g. `exness.technologies` instead of `exness.technologies.ltd`) is
rejected with a clear message instead of burning a two-minute Wine install and
then dying as `could not download the MT5 installer`. If you see a rejection, fix
the slug from the broker's own "Download MT5" page — do not retry the same URL.

## The installation rule (this is what you were getting wrong)

Being handed an `.mq5` is **not** permission to compile it casually. An `.mq5`
has exactly one compiler — MetaEditor, inside the Wine + MT5 chain — so **every**
MQL5 run begins with `install`, then `status` until `stage="done"`, and only
then `compile`. Skipping that is the bug this playbook exists to prevent.

`compile` enforces the rule itself: with no (or a partial) chain it refuses with

```json
{"ok": false, "stage": "not_installed", "missing": ["wine", "metaeditor64.exe"],
 "next": "mt5_sandbox(action='install') then poll action='status' until stage='done'"}
```

Read that literally. It means **provisioning is missing**, so:

* **Never** treat it as a code error — do not edit the source to "make it compile".
* **Never** conclude MQL5 cannot be compiled in the sandbox.
* **Never** hand the uncompiled `.mq5` back to the user asking them to compile it.
* Run `install`, poll `status` to `stage="done"`, then retry `compile`.

A `not_installed` refusal is the *only* result that requires zero edits. Every
other `ok: false` (real `errors[...]` from MetaEditor) is a genuine source error —
fix that, and compile again.

## Canonical workflow

```
1. mt5_sandbox(action="install")            # installs AND waits to a terminal stage
     -> stage="done"  (installed=true)      # proceed immediately
     -> stage="failed"                      # read message + log_tail, fix, retry install
     -> poll_timeout=true                   # still running: poll status again, keep waiting
2. mt5_sandbox(action="start", login=<acct>, password=<pw>, server="<broker-server>")
     -> launches terminal64.exe under Xvfb in portable mode with the login seeded
     -> returns ipc_ready=true once the account is live
3. mt5_sandbox(action="account")            # confirm balance/equity/currency
4. mt5_sandbox(action="quote", symbols="EURUSD XAUUSD")
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

MetaEditor resolves `#include <Trade/Trade.mqh>` relative to the **MQL5 data
tree that owns the source file** — not relative to the terminal binary, and not
relative to `--include`. Put sources under the data tree below and the include
resolves implicitly:

```text
# terminal data dir (where Experts/ and Include/ belong)
~/.wine-mt5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/MQL5/

# install dir (holds the binaries + a stub MQL5/Experts)
~/.wine-mt5/drive_c/Program Files/MetaTrader 5/
```

The installer now mirrors the full standard library into that data tree's
`Include/`, and the CLI re-mirrors it before every compile, so a plain
`#include <Trade/…>` compiles with **no extra flags**.

Two traps, both verified and both now handled automatically:

* Passing an explicit `include=`/`--include` pointing at the install dir makes
  MetaEditor concatenate the flag with the source-relative path and fail on a
  doubled path — `error 106: file '…\Include\Include\Trade\Trade.mqh' not
  found`. Prefer letting the implicit data-tree lookup work.
* A broker may push newer headers on first sync. The mirror uses copy-if-absent,
  so broker-supplied headers are never clobbered.

Either way an `error 106` is a *path* problem, not a code problem — never edit
the user's `.mq5` to work around it.

Verified end to end in a sandbox: a self-contained `Plain.mq5` compiled to a
6186-byte `Plain.ex5` with `Result: 0 errors, 0 warnings, 335 ms elapsed,
cpu='X64 Regular'`.

The `.log` MetaEditor writes next to the source is UTF-16LE with a BOM; the CLI
auto-detects UTF-16/UTF-8/Latin-1, so read `errors`/`log` from the JSON rather
than fetching the file yourself.

After compiling an EA, `action="experts"` tails the Experts journal and
`action="logs"` tails the terminal log — together they are the full read/fix
loop for runtime errors.

## A clean compile does NOT mean the EA is right

MetaEditor only checks syntax and symbols. A real submitted EA compiled with
`0 errors, 0 warnings` and still had **eight** defects, none of which the
compiler can see. When you are asked to "fix" an `.mq5`, run this checklist in
addition to the compile loop — silently-wrong trading logic is far more
expensive than a compile error.

| Anti-pattern (seen in the wild) | Why it is wrong | Fix |
|---|---|---|
| `ArraySetAsSeries(a,true)` **after** `CopyBuffer(...,a)` | `CopyBuffer` fills oldest→first, so re-flagging afterwards leaves `a[0]` as the **oldest** bar. Every signal is inverted and it still compiles. | Set `ArraySetAsSeries` on the destination **before** `CopyBuffer`. |
| `CopyBuffer(h,0,0,3,a)` with no return check | On a cold start it returns `-4`/fewer bars and leaves `a` empty, so `a[0]` is an out-of-range read at runtime. | `if(CopyBuffer(...) != n) return;` |
| `x <= y == false` | Parses as `(x <= y) == false`. Reads as "not crossed", is actually "not-above" — the opposite of the intended cross test. | Write `!(x <= y)` or the explicit negation. |
| `iMA(...)` result never checked | `OnInit` returns `INIT_SUCCEEDED` with `INVALID_HANDLE`s; the first `CopyBuffer` then fails forever. | Validate every handle, `return INIT_FAILED` otherwise. |
| `if(PositionsTotal() > 0) return;` | `PositionsTotal()` is the **whole account**. Any other EA or a manual trade blocks this one permanently. | Count only positions matching your magic number + symbol. |
| No `trade.SetExpertMagicNumber(...)` | Orders are unattributable, so the filter above and `close_all` cannot identify them. | Set a magic number and filter on it. |
| SL/TP computed from `ASK` for a **sell** | A short is filled at `BID`; measuring stops from the other side misprices risk by the spread (and can produce `10016 invalid stops`). | Use `ASK` for buys, `BID` for sells. |
| `trade.Buy(...)` return value ignored | A rejected order is invisible; the EA looks idle. | Check the bool and log `trade.ResultRetcode()`/`ResultRetcodeDescription()`. |
| Lot size not normalised | Brokers reject volumes off `SYMBOL_VOLUME_STEP` / below `SYMBOL_VOLUME_MIN` with `10014`. | Clamp+round to min/max/step. |
| No `IndicatorRelease()` in `OnDeinit` | Handle leak across recompiles. | Release every handle. |

Note `#include <Trade\Trade.mqh>` with a **backslash** is accepted by MetaEditor
(verified), so it is a style issue, not a bug — do not "fix" it into a
`error 106` by changing paths you have not tested.

When you rewrite an EA for these reasons, **compile the result before reporting
it**: the fixed version above builds with `0 errors, 0 warnings, 478 ms`.

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
| `unimplemented function ucrtbase.dll.crealf` | **numpy 2.x**, not the MT5 package. Wine's builtin `ucrtbase.dll` has no `crealf`; numpy 2.x calls it at import. The installer pins `numpy<2` (verified: 2.4.6 aborts, 1.26.4 imports). `winetricks vcrun2022` and `DllOverrides`→native do **not** fix it. |
| bridge call hangs until the 900 s command timeout, no error | Wine started `winedbg` on an unimplemented call and is waiting on a dialog nobody can answer. The installer sets `HKCU\Software\Wine\WineDbg\ShowCrashDialog=0` so it aborts fast. |
| `order` fails with `AttributeError: ... 'SYMBOL_FILLING_FOK'` | Old bug, now fixed: the package exports only `ORDER_FILLING_*`. `filling_mode` is a bitmask (1=FOK, 2=IOC, 4=RETURN); `order`/`close` retry each supported mode. |
| `retcode: 10018, "Market closed"` | **Not a code bug.** The request reached the broker and was answered correctly. FX/metals are shut on weekends — find a live symbol before concluding trading is broken. |
| `stop` reports success but the terminal is still up | Old bug, now fixed: Wine runs the terminal with comm `main`, so `pkill -x terminal64.exe` matched nothing. PIDs now come from `/proc/*/cmdline`. Never use `pkill -f terminal64` — it matches the calling shell and `wineserver`. |
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
- A **library-using** EA compiles cleanly once the data tree holds the standard
  library. Re-verified 2026-09-20 on Debian 12 / 4 GB / wineserver 10.0:
  `TradeEA.mq5` (**`#include <Trade/Trade.mqh>` + `CSymbolInfo`**) →
  `Result: 0 errors, 0 warnings, 402 ms elapsed` → `TradeEA.ex5` 14 662 bytes,
  with **no** `--include` flag. Generic compile errors still surface normally
  (`error 256: undeclared identifier`, `error 157: ')' - expression expected`),
  so the read → edit → recompile loop is intact.