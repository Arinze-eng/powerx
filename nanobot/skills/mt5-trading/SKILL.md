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

A full Wine + MT5 + bridge install needs ~10–25 minutes, but **every sandbox
command is capped at 120 s** — nothing the tool sends may run longer than that.
So the installer runs *detached* and the tool polls `status` internally, in
≤120 s steps, until it hits a terminal stage, then returns **once**.

Keep that ceiling when you run anything by hand: any command you compose (a test
suite, a probe, a `status`) must finish inside 120 s. Use `timeout 100 …`, and
split anything longer into separate calls rather than raising the timeout.

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
1. mt5_sandbox(action="install", server="<broker-server>")
                                            # THE SERVER PICKS THE BUILD -- never assume
                                            # one broker matches the deployment
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

## Always pass `server` — the server chooses the terminal build

An MT5 terminal can only resolve a server name that its own `Config/servers.dat`
carries. A build for broker A therefore **cannot** log in to broker B's server,
and MT5 does not say so: it skips the connection silently, writes **zero
`Network` lines**, and the bridge then blocks until its IPC timeout. It looks
like a frozen terminal.

So the server name is the input, and it goes to `install` as well as `start`:

* `install` with `server` resolves the matching build, installs it, and records
  which build landed. Do **not** assume the deployment's default broker — that
  hardcoding is what made a MetaQuotes-Demo account hang against an Exness
  terminal.
* `start`/`login` re-check the request against the installed build *before*
  touching MT5 and refuse in under a second with `failure:
  "server_not_in_terminal"` rather than hanging.
* When they refuse for a broker the tool can install, the tool installs that
  build and **replays the action itself** — one `start` call is enough.
* For a broker the tool does not know, pass the broker's own download link:
  `broker_installer_url` + `broker_dir_name`. Never guess a URL — unverified
  slugs on `download.mql5.com` 404.

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

## Exits at a price — never poll for them

When the user says *"close when it hits X"* — a stop, a target, "get me out at
1.1650" — the exit has to be held by something that is **not you**:

| What they want | Use | Why |
|---|---|---|
| Exit an **open** position at a level | `action='modify'` with `ticket` + `exit_at=X` | The **broker's server** holds the level. It fires in milliseconds, with no process and no model turn, and it survives the sandbox being paused or killed. |
| A condition the broker cannot hold (part of a position, a basket, a level that is not the stop) | `action='guard'` + `guard_action='arm'` | A detached tick-level watcher inside the sandbox, reading the tick stream every `interval_ms` (default 100 ms). |
| Out *now*, at whatever the market is | `action='close'` | |

**Polling `quote` and comparing is not an exit — do not do it.** MEASURED inside
a real box (2026-09-23): one `quote` round trip costs **1.0–1.5 s**, before your
own turn latency is added. A level touched and recovered inside one poll is
missed entirely, and the position stays open. A position reporting `sl 0.0
tp 0.0` has **no** server-side exit at all: nothing but an agent turn can ever
close it. When `positions` comes back with
`positions_without_a_server_side_exit`, arm a real exit before doing anything
else — that list is the exact shape of the original complaint.

* `modify --exit-at X` infers the side from the position: for a long, a level
  above the market is the target and below it is the stop; for a short, the
  reverse. It nudges the level outside the broker's minimum stop distance and
  reports `adjustments` (every leg it moved, with the level asked for) plus
  `adjusted_from` / `adjust_reason` — **read them**: they mean the level that is
  now set is not the level that was asked for.
* `modify --sl/--tp` is **checked against the side the leg has to be on**, and a
  level the market has already gone through is **refused** (`ok: false`,
  `wrong_side: ["sl"]`), not moved: nothing is sent, and the hint names the side
  that leg needs and offers `--exit-at` for a caller who meant "exit at X". A
  level that is merely **too tight** is still clamped out to the broker's
  minimum distance, staying on its own side. Only the legs you actually pass are
  touched, so `--tp` never rewrites a stop that was already there. Two different
  instructions — do not treat a refusal as a failure to retry blindly.
* **An exit AT the market closes the position now, by design.** `--exit-at` with
  a level equal to the current price (measured: `inside = bid`) routes to the leg
  on that side, is clamped just outside the minimum stop distance, and the server
  fills it immediately — `after: count=0`. So do not follow a level-at-the-market
  call with more work on that ticket: it is gone, and a second call answers
  `no open position with ticket(s) … it may have already closed`. If the caller
  wanted a level to *wait* at, give one that is not already through the market.
* A guard fires in **92–191 ms** measured (trigger tick → fill acknowledgement),
  across nine live MetaQuotes-demo samples: 92.5, 96.6, 97.3, 100.5, 102.3,
  102.4, 109.1 ms with one position, and **190.5 / 187.6 ms** closing a
  two-position basket in a single call (both retcode 10009).
  `guard_action='events'` carries `latency_ms` per fire; when a "close at X"
  instruction was late, that number is the answer to why. Nothing here is a
  *guaranteed* bound — it is what the barrier is worth on a live account with a
  fixed 100 ms poll, and a sub-100 ms spike through the level and back can still
  be missed.
* **An `arm` whose level is already satisfied is a completed exit, not a failed
  guard.** The rule fires on the first tick and the watcher is gone before the
  heartbeat wait ends, so `arm` answers `ok: true, guard: "fired_immediately"`
  with `fired[]` (level, trigger price, `latency_ms`, positions matched). Read
  `fired` and `positions` instead of arming again — the exit has already
  happened.
* If that first-tick fire is **refused by the broker**, `arm` answers `ok: false,
  guard: "close_failed"` with `close_failed[]` carrying the retcodes: the
  position is **still open** at a level you were asked to be out at. That is a
  different problem from a dead watcher and is named differently on purpose.
* A failed `arm` tails only what **that** watcher wrote, so `log_tail` is not a
  previous run's `max 600 s` line; if the new watcher wrote nothing at all,
  `log_note` says so.
* **A guard has no time limit unless you set one.** `guard arm` with no
  `max_seconds` holds the level for as long as it takes to be touched — "close
  when it hits X" is a standing instruction, not a one-hour one. The old default
  of 3600 s is what stopped guards whose level had not arrived yet. `max_seconds`
  is honoured only when it is positive, and `ensure` inherits the budget the
  guard was armed with when the call does not restate one.
* **A stopped guard with rules still armed is an ALARM, not a status.** `status`
  answers `ok: false` with `alert: "guard_not_running"`, the `exit_reason`, the
  `heartbeat_age_s`, and `recovery: "guard action='ensure'"`. Never report the
  levels as protected while `alert` is set.
* `guard_action='ensure'` restarts a watcher that stopped with rules still armed,
  and reports `unprotected_seconds` — the window in which a level could have been
  touched and nothing acted on it. Say that window out loud; the rules stayed
  armed through it and nothing fired. `ensure` answers `action: "rearmed"`,
  `"rearmed_and_fired"` (the level was reached while nothing was watching, so the
  restarted guard fired on its first tick and stopped again) or `"rearm_failed"`
  — a fired re-arm is a completed exit, not a broken guard.
* Guard-death detection is **passive**: a watcher that dies is noticed by the
  next tool call that looks (`status`, `positions`, `ensure`), never by a
  background watchdog — a Wine process cannot be supervised from outside Wine.
  So do not tell anyone a level is protected indefinitely on the strength of one
  `arm`: re-check `positions.protection` or `guard status` on the next turn, and
  treat any `alert` as unprotected until `ensure` says otherwise.
* **`arm` refuses a symbol it cannot price.** A rule on a symbol with no tick can
  never fire, and a watcher polling in silence is indistinguishable from
  protection. The refusal names the symbols (`unpriceable`) and points at
  `action='symbol'`. Pass `guard_allow_unpriceable=true` only for a symbol you
  expect to price later. A symbol that goes dark **mid-watch** is logged as
  `rule_unpriceable` and surfaced by `status` as `alert: "rule_unpriceable"`;
  if nothing was ever priced the watcher exits with
  `exit_reason: "unpriceable_symbol"`.
* `status` / `stop` / `events` / `clear` stay available with `MT5_ALLOW_TRADING`
  off, so a live guard can always be inspected or disarmed. `ensure`, like `arm`,
  **starts a process that can place orders and is gated the same way**.
* `positions` answers "is anything watching a price?" alongside the positions:
  `protection.guard_live`, `protection.guard_alert`, and a `warning` when rules
  are armed with nothing running.
* End a guard with `guard_action='stop'` (a stop **file**), never by killing a
  process: `pkill -f` matches the shell that launched it.

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
| `failure: "server_not_in_terminal"` | The terminal on the box cannot resolve that server (wrong broker's build). The payload names the build to install — or pass the broker's download link if it is not in the registry. Never retry the same login against the same terminal. |
| `failure: "no_network_activity"` | The terminal log has **zero** `Network` lines, which is how MT5 reports a name it cannot resolve — but a *successful* generic login also logged none (measured 2026-09-22), so compare `installed_broker` against the broker that owns the server before concluding. The hint carries both readings. |
| A login sits for minutes and then reports `-10005 IPC timeout` | Old signature of this bug, before the preflight refusal existed. Read `logs/<date>.log` for `Network` lines before blaming Wine or IPC — and note the log is **UTF-16**, so plain `grep` finds nothing even when the lines are there. |
| `unimplemented function ucrtbase.dll.crealf` | **numpy 2.x**, not the MT5 package. Wine's builtin `ucrtbase.dll` has no `crealf`; numpy 2.x calls it at import. The installer pins `numpy<2` (verified: 2.4.6 aborts, 1.26.4 imports). `winetricks vcrun2022` and `DllOverrides`→native do **not** fix it. |
| bridge call hangs until the 900 s command timeout, no error | Wine started `winedbg` on an unimplemented call and is waiting on a dialog nobody can answer. The installer sets `HKCU\Software\Wine\WineDbg\ShowCrashDialog=0` so it aborts fast. |
| `order` fails with `AttributeError: ... 'SYMBOL_FILLING_FOK'` | Old bug, now fixed: the package exports only `ORDER_FILLING_*`. `filling_mode` is a bitmask (1=FOK, 2=IOC, 4=RETURN); `order`/`close` retry each supported mode. |
| `retcode: 10018, "Market closed"` | **Not a code bug.** The request reached the broker and was answered correctly. FX/metals are shut on weekends — find a live symbol before concluding trading is broken. |
| `stop` reports success but the terminal is still up | Old bug, now fixed: Wine runs the terminal with comm `main`, so `pkill -x terminal64.exe` matched nothing. PIDs now come from `/proc/*/cmdline`. Never use `pkill -f terminal64` — it matches the calling shell and `wineserver`. |
| A "close when it hits X" instruction was late, or never acted on | The level was **polled** instead of held. MEASURED: a `quote` costs 1.0-1.5 s in the box, so a touch between polls is missed. Use `modify --exit-at X` (broker-held, instant) or `guard` (tick-level, 100 ms) — see "Exits at a price". |
| `modify` returns `retcode 10016` (invalid stops) | The level is inside the broker's minimum stop distance or on the wrong side of the market. Read `min_distance` from `action='symbol'`; on a live position `modify --exit-at` clamps and reports `adjusted_from` instead of failing. |
| a guard reports `running: false` when a fill was expected | It stopped: read `guard_action='events'` for the `watcher_stop` reason and `max_seconds`. A guard with no rules (`rules_armed: 0`) also exits immediately. |
| the guard log is nothing but Wine `fixme:` lines | Old bug, now fixed: Wine's stderr is kept in `watcher.err` and `log_file` holds the watcher's own lines (`[guard HH:MM:SS] ... FIRED ... N ms to fill`). |
| `bridge_imports_in_wine=false` in `doctor` | Windows python or the `MetaTrader5` wheel failed to install; check `log_tail`. |
| `mt5_cli.py must run inside Wine` | Do not invoke the CLI's bridge actions directly on Linux python — always go through `mt5_sandbox`. |

## Verified facts (measured in a Novita sandbox)
- **Exits at a price are instant with `modify`/`guard`** (Runloop devbox,
  MetaQuotes-Demo, 2026-09-23): a long with `sl 0.0 tp 0.0` was given
  `modify --exit-at <bid-2 points>` and the **server** closed it at exactly
  the level (deal `reason 4`, comment `[sl 1.14232]`) with no `close` call
  and no model turn. The tick-level guard fired twice at **97.3 ms** and
  **109.1 ms** trigger→fill, retcode `10009`, position count 0 both times.
  For comparison, one `quote` CLI round trip in the same box cost
  **1014-1480 ms** — which is why polling a price can never be an exit.

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