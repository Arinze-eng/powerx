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
## Resolving an unfamiliar broker (do this, do not stall)

When the credentials name a broker the terminal cannot resolve, **keep going**:
`mt5_sandbox` now consults its broker table and validates candidate installer
URLs over HTTP before installing, so an unrecognised server is a discovery step,
not a dead end and not a question for the user.

Order of effort, cheapest first:

1. `action="start"` / `action="login"` as normal. If the build is wrong the tool
   resolves and installs it **and replays your action** — usually you never see it.
2. `action="install", server="<their server>"` when provisioning from scratch. An
   unregistered server is **resolved for you**: the tool discovers that broker's
   installer, validates it over HTTP, and installs it. It never quietly falls back
   to the default build — a terminal for a different broker cannot resolve their
   server at all, so that "successful" install is a dead end wearing a green tick.
3. If it still refuses: `action="list_brokers"` (what it knows), then
   `action="discover_broker"`, `server="<their server>"`.
4. If discovery fails, **web-search `<broker> download MT5`**, fetch that page, and
   pass it: `action="discover_broker", server=..., page_urls=["<the page url>"]`.
   The link on the broker's own page is authoritative; a derived guess usually is not.
   Then `install` with the `broker_installer_url` it returned. You can also hand
   `page_urls` straight to `install` and it does the fetch-and-validate itself.

Three things that keep you from going in circles:

* **`install` refusing on a named server is not a wall, and not an unsupported
  broker.** It comes back as `failure: "server_not_resolved"` with `remedy` and
  `next` spelling out the one call to make (`discover_broker`). A *bare* `install`
  with no `server` still gets the deployment's default build — that is correct, and
  it is the only case where a default is the right answer.
* **Only some table entries are confirmed.** `list_brokers` marks each brand
  `verified: true|false`. A `false` brand carries a *guessed* domain and often 404s
  — that means "our guess was wrong", **never** "this broker is unsupported". Go to
  step 3 instead of retrying guesses.
* **Never invent an installer URL.** Unverified slugs on `download.mql5.com` 404,
  and a bad guess costs a two-minute Wine install to discover it. Every URL you act
  on must come back `valid` from `discover_broker`.
* **Do not ask the user for their download link before step 3.** Asking early is
  the old behaviour and it is what this section replaces.
* **`done` means a terminal was installed, and the directory is read back off the
  disk.** A resolved broker's install directory cannot be predicted (Exness's own
  installer writes `MetaTrader 5 Terminal` for the build everyone calls
  `MetaTrader 5 EXNESS`, and a discovered broker has no verified name at all), so
  the installer records the one it actually used and `status` reports it as
  `installed_dir_name` beside `installed_url`. Read those two rather than assuming
  the name — and never call a broker "not installed" while they name its terminal.
* **`failure: "no_new_terminal"` is not a download failure.** It means the sandbox
  already carried a terminal that predates this install and the installer would not
  add a second one beside it, so *nothing was installed for this broker*. The
  payload names the pre-existing terminal in `existing_terminal` and the fix in
  `remedy`. Retrying on that sandbox repeats the same outcome — use a fresh one.

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

The internal wait is capped (`MT5_INSTALL_WAIT_SECONDS`, default **3300 s / 55
min** — above the 10–25 min worst case so a normal install finishes *inside* the
call). If it returns `poll_timeout: true`, the install is **progressing, not
broken**. Call `action='status'` again immediately, in the same turn, and keep
polling. Do not restart, do not re-run `install`, do not ask the user anything,
and **do not reply with a status update and no tool call** — that ends your turn
and freezes the install, which is the exact failure this rule exists to prevent.

### Poll it as a WATCH, not as a spin

`status` with `wait_seconds` **blocks** until the install moves and hands you the
thing that moved. Use it instead of calling `status` back to back:

```
mt5_sandbox(action="status", wait_seconds=60, lines=40)
```

It returns the moment the stage changes, the installer writes more output
(`install_log_grew`), or the installer process exits (`installer_exited`), and
the snapshot in the same payload is the state **after** that change. Read
`watched.observed_event` and `watched.note` and say what happened; a
`watched.timed_out: true` means *nothing moved in that window*, which is a fact
about the install and not a failure. Capped at 90 s per call — call again to
keep watching. A finished install answers instantly with
`install_already_done` / `install_already_failed` rather than stalling.

### Broker installer URLs are validated

`broker_installer_url` is checked **before** the sandbox is touched. A slug with a
missing TLD (e.g. `exness.technologies` instead of `exness.technologies.ltd`) is
rejected with a clear message instead of burning a two-minute Wine install and
then dying as `could not download the MT5 installer`. If you see a rejection, fix
the slug from the broker's own "Download MT5" page — do not retry the same URL.

### An unknown broker: `list_brokers`, then `discover_broker`

The registry (`BROKER_BUILDS`) only knows a handful of brokers. Handed
credentials for ANY other one, MT5 does not report a wrong build — it **skips the
connection silently**, writes zero `Network` lines, and the bridge blocks on its
IPC timeout. The login reads as a frozen terminal and the real cause is invisible.

**Step 1 — ask what is already known.** `action='list_brokers'` returns every
broker whose installer the tool already knows, and marks the install-dir names
that differ from the usual `MetaTrader 5 <BRAND>` pattern. Do this FIRST: it tells
you whether this broker is handled before you spend a single probe.

**Step 2 — search, then discover.** You do the search half:

```
1. web-search "<broker> download MT5" and fetch the broker's OWN download page.
2. mt5_sandbox(action="discover_broker", server="<their server>",
               page_urls=["<the page URL, or its fetched text>"])
     -> {"found": true, "url": "<validated installer>", "source": "page", "next": ...}
3. mt5_sandbox(action="install", server="<their server>",
               broker_installer_url="<that url>", broker_dir_name="<if it differs>")
then status -> done, start, account.
```

**Pass `page_urls` whenever you have it.** A link mined from the broker's own page
is *authoritative*; a slug invented from the brand is nearly always a 404. The
slug is a legal ENTITY domain, not the brand — seen in the wild:
`axicorp.financial.services` (AXI), `exness.technologies.ltd`, `deriv.com.limited`.
MEASURED 2026-09-24: 210 blind slug permutations across 21 well-known brokers
produced **2** live URLs. So search first, then pass what you found.

If the broker IS in `list_brokers`, discovery tries its known entity domain
first — you may not need to search at all. Either way, **never hand-build an
installer URL and never retry a login against a terminal that cannot resolve the
server.**

**Adding a broker permanently** — worth telling the user, since it helps every
later run: set `MT5_BROKER_INSTALLERS='brand|slug|name;brand2|slug2|name2'` in the
deployment. Operator entries win over the built-in table, so this also corrects a
slug that has gone stale.

Two things the answer can say that are NOT "the broker does not exist":

* `blocked: true` — the MT5 CDN is rate-limiting this box. Continuing to sweep is
  what *causes* it, and a blocked CDN then refuses the **real** download the
  install needs next. So the search stops itself at the first refusal. Wait a
  minute and retry, or pass `page_urls`.
* `derived` source — the URL was constructed from the brand tokens, not mined.
  It IS validated (right content type, real size), but verify `broker_dir_name`:
  the usual pattern is `MetaTrader 5 <BRAND>`, and Deriv is the known exception
  (`MetaTrader 5 Terminal`). A wrong directory name silently reintroduces the
  coexistence failure.

When nothing validates and the CDN is not blocked, the answer names the brand
tokens it tried. Do not then invent a URL — ask the user to paste their broker's
download link, which is one honest question instead of a failed install.

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

`order`, `split`, `close`, `close_all`, `cancel`, `modify` and `limits` move real
money and are **disabled unless the deployment sets `MT5_ALLOW_TRADING=1`**. When
disabled they return an error and never reach the sandbox. `risk` and
`limits show` only read, so they keep working either way — the caller who most
needs to see the account's exposure is the one who has just been told trading is
off.

* `order` needs `symbol` and `side` (`buy`/`sell`), plus **a size** — either
  `volume` in lots, or `risk_money` / `risk_pct` with the `sl` that defines it.
* `close` needs `ticket`, or `group` for a split; optional `volume` for a partial.
* `cancel` needs `ticket` (a resting order), or `cancel_all=true`.
* `dry_run=true` prints the exact command that *would* run, without sending —
  use it to show the user what you are about to do.

### Every position gets a stop — and that is not a formality

`order` and `split` **refuse to open without `sl`** unless you pass
`allow_no_stop=true`. This is not the tool being fussy:

* Without a stop the **broker's server holds no exit at all**. Nothing on
  MetaQuotes' side can close the position.
* The only thing left that can close it is **something looking at the price** —
  and nothing is looking between your calls. Your next turn may be minutes away,
  the box may pause, the run may end.
* "I will watch it" is a promise that expires. A stop is one the broker keeps.

If a trade genuinely should not carry a stop, say so out loud when you report it;
the result comes back with `alert=opened_without_a_stop` either way. A `modify`
that leaves a position with neither `sl` nor `tp` returns
`alert=position_left_without_a_stop`.

### A stop that moves itself — `guard_mode`

A stop set when the order goes in is a stop that **never moves again**. The two
things every trade plan asks for next — *"take the risk off at 1R"* and *"then
let it run"* — have no broker-side equivalent: MT5's server holds one fixed `sl`,
so a stop that follows the price has to be **something watching the price**. That
is exactly what does not exist between your calls.

`action='guard'` takes a `guard_mode` for this. Instead of waiting for a level to
be crossed, the rule becomes a **standing policy** that the detached watcher
re-evaluates on every tick:

```
# take the risk off once the price is 1R in front of the entry
mt5_sandbox(action="guard", guard_action="arm", symbol="XAUUSD",
            guard_mode="breakeven", ticket=4735550381, when_r=1)

# and then let it run: hold the stop $2.00 behind the best price it has seen
mt5_sandbox(action="guard", guard_action="arm", symbol="XAUUSD",
            guard_mode="trail", ticket=4735550381, trail_distance=2.0)
```

| `guard_mode` | What it does | Needs |
|---|---|---|
| `close` (default) | closes the matched positions the instant the level is touched | `trigger_price` |
| `breakeven` | once the price is `when_r` × the position's **own** risk in front of the entry, puts the stop **at the entry** | `when_r` (default `1`) |
| `trail` | keeps the stop `trail_distance` of **price** behind the best price the position has seen, following it up and never down | `trail_distance` |

Three things make it safe to leave alone:

* **A stop only ever moves towards profit.** Every candidate is compared with the
  stop that is already there, and one that is not an improvement is dropped. The
  worst a moving stop can do is nothing — it can never loosen a stop that already
  protects you.
* **It is never placed inside the broker's minimum stop distance.** A stop closer
  to the price than `trade_stops_level` comes back `10016 Invalid stops`, so the
  rule names the room the broker demands and sends nothing instead of sending it
  and being refused.
* **It never takes the `tp` off.** Moving a stop is `TRADE_ACTION_SLTP`, which
  carries **both** levels; the target is passed back unchanged, because this rule
  owns the stop and nothing else.

`R` is measured **once**, from the **stop the position was opened with** — read
from the order that opened it, because `position.sl` is the stop *now* and a
moving stop has already moved it. Measured off the current stop instead, R shrinks
every time the stop moves, the trigger walks down with it, and the stop crawls
into the price for no reason.

**Where the R came from is in every answer**, so the trigger is auditable rather
than asserted: a waiting row reads
`not yet 1R (4297.43) -- 1R is 2.17 from the stop it was opened with`.

MEASURED LIVE 2026-09-24: a trail moved a Gold stop 4293.09 → 4294.82, and a
`breakeven` rule armed *afterwards* recorded R as **0.44** instead of 2.17, so its
`1R` sat 0.44 above the entry. That is the bug this ordering exists to prevent,
and why you arm `breakeven` **when the trade opens**, not once it is running.

If the order history cannot be read, the fallback is the stop as it is now and the
answer says *that* instead — and if the fallback is in use on a position whose
stop has already reached the entry, the rule does **nothing** and says so, rather
than measuring R off a stop that has already moved.

A `guard_mode` rule needs **no `trigger_price`** — there is no level to cross — so
it is never "satisfied" and never fires itself out. It stays armed and keeps
working on whatever positions match its scope, which is what makes it usable
across a whole session instead of on one trade. `activate_at` holds it back until
the price gets somewhere ("trail it, but not before Gold is above 4300").

Two limits worth knowing before you arm one:

* **A position with no stop is reported, not fixed.** `breakeven` needs a stop to
  move; on a naked position it answers `skipped: "this position has NO stop to
  move"` and tells you to `modify` one on first. The level to invent would be a
  guess, and a guessed stop is worse than an honest report.
* **`trail_distance` is price, not pips.** On Gold `2.0` is $2.00 — the
  playbook's own 20-pip stop distance. A distance of zero would put the stop on
  the price and close the position, and is refused.

**A moving stop is usually WAITING, and waiting is what you will see.** `not yet
1R` writes no event — it would write one per tick — so `guard_action='status'`
publishes `stop_move`: one row per armed rule with `outcome` (`moved`, `refused`,
`waiting`, or `no_position` — armed and nothing matches it yet), the `mode`, the
price, and the `detail` — *"#4735550381 not yet 1R
(4302.18)"* or *"#4735550381 4284.92 -> 4286.69"*. That is the only way to tell a
rule doing its job quietly from a rule that was never armed, so read it before
telling anyone their stop is protected.

`guard_action='events'` has the history: `stop_moved` carries the mode, the price
that caused the move, and a `moved` row per ticket with `from_sl` and `to_sl`;
`stop_move_failed` carries the broker's own retcode.

### Entering at a price — `entry_type`

*"Buy the dip at 4270"* and *"buy the breakout above 4300"* are **not market
orders**. Sending a market order instead fills at a price the user never asked
for, and the difference between the price named and the price paid is the whole
trade.

```
# rests at 4270 and fills only if the market comes down to it
mt5_sandbox(action="order", symbol="XAUUSD", side="buy", volume=0.10,
            entry_type="limit", price=4270.0, sl=4268.0)

# rests at 4300 and fills only if the market breaks through it
mt5_sandbox(action="order", symbol="XAUUSD", side="buy", volume=0.10,
            entry_type="stop", price=4300.0, sl=4298.0)
```

* `limit` fills **better** than the market (buy below the ask, sell above the
  bid). `stop` fills **through** it (buy above the ask, sell below the bid).
* A pending order **holds no position** and risks nothing until it fills. It is
  not an open trade: do not watch it, do not "manage" it, and do not report it as
  a position. Read it with `orders`, remove it with `cancel`.
* A pending order carries its own `sl`/`tp`, so the exit is already on the server
  the moment it fills.
* The wrong side is caught **before** the round trip. A buy limit above the ask
  comes back with the corrected wording instead of the broker's
  `retcode 10015 "invalid price"`, which names neither the side nor the fix.
* Same for the legs: a stop on the winning side of the entry, or a target on the
  losing side, is refused here rather than surfacing as `retcode 10016 "Invalid
  stops"` — measured live: a buy limit at 4282.05 carrying `sl 4285.05` came back
  as exactly that.

### Sizing by money at risk — `risk_money` / `risk_pct`

*"Risk $100 on this"* is what a person says. The lot size is arithmetic over the
stop distance, the pip and the contract size — three places to be wrong, in the
one calculation where being wrong costs money. Let the tool do it:

```
mt5_sandbox(action="order", symbol="XAUUSD", side="buy", risk_money=100,
            sl=4283.18)
```

* The result carries `sizing`: the stop in pips, the money per pip per lot, the
  unrounded lots, the lots used, and `risk_pct_of_equity`. **Read it out loud**
  when the number matters — it is what makes the size checkable.
* It rounds **down** to the broker's lot step, so the order never risks more than
  asked. On Gold a pip is 0.10 and the contract is 100 oz, so a 20-pip stop is
  $2 a lot: $20 of risk is 0.10 lots.
* Below the broker's minimum it **refuses and names the smallest risk the symbol
  can express**, instead of quietly sending the minimum — which would risk
  several times what was asked.
* A market order fills at whatever the other side is when it lands, so the risk
  that was *sized* is not exactly the risk that was *taken*. MEASURED: the
  XAUUSD ask moved 0.18 between the quote and the fill, turning a 20.0-pip stop
  into 21.8 pips and a $20 risk into $21.80. `sizing.fill_stop_pips` and
  `sizing.actual_risk_money` report what the price paid actually implies.

A rejected order is a normal result, not an exception: the payload carries the
broker `retcode` and `comment`. Common retcodes: `10009` done, `10016` invalid
stops, `10019` no money, `10030` unsupported filling mode.

### The account circuit breaker — `limits` and `risk`

**Every stop in this skill caps ONE trade. Nothing capped the ACCOUNT.** The
account is the thing that runs out, and no single ticket's stop prevents it:
five "small" positions each risking 2% is 10% on the table, and the day ends
with no ticket having done anything wrong.

Set the limits **before** the next order, not after the loss:

```
mt5_sandbox(action="limits", limits_action="set",
            max_total_risk_money=300, max_positions=5,
            max_daily_loss_money=200)
```

| Limit | Caps |
|---|---|
| `max_total_risk_money` | what the **whole book** loses if every stop is hit at once |
| `max_total_risk_pct` | the same, as a percentage of account **equity** |
| `max_positions` | how many positions may be open — a `split` counts its full N |
| `max_daily_loss_money` | **realised** loss today (broker server midnight), after which no new position opens |

`limits_action` is `show` (default), `set` or `clear`. **`set` merges**: limits
you do not mention keep their value, so raising one does not silently drop the
others. A limit of zero or less is refused — it would refuse every order for a
reason that looks like a rule.

**They are enforced where an order is sent, not where it is remembered.** A
refused order comes back `ok=false` with `breaches` (each with the limit, the
numbers and a plain reason) and **nothing was sent**. That is the point: the rule
binds every caller, including one that never read this page.

```
mt5_sandbox(action="risk")
```

**One call, before you size anything.** It returns the open book — each position
with what it loses at its stop — plus `totals` (`risk_money`,
`risk_pct_of_equity`, `unrealised_pnl_money`), `today` (realised P&L since the
broker's midnight), `limits`, `headroom`, and `breaches` if the book already
violates something. Read `headroom`, then size the order to it.

Things it will tell you that are easy to get wrong on your own:

* **`risk_money` and unrealised P&L are different numbers.** One is what the
  position loses *if the stop is hit* (`|entry - stop| × contract × lots`); the
  other is what it is worth *right now*. Both are in the report.
* **A position with no stop has UNKNOWN risk, not zero.** It is listed in
  `positions_without_a_stop`, and `totals.positions_counted` says how many the
  total actually covers. With a total-risk limit in force that is itself a
  breach: a limit enforced against a total known to be too small reads as
  protection while being none.
* **Deposits and withdrawals are not P&L.** The daily-loss figure counts closed
  deals only, so a funding transfer can never read as a winning day or a
  withdrawal as a loss that trips the breaker.
* **An unreadable limits file is reported, not obeyed as "no limits"**
  (`alert=risk_limits_unreadable`), and an unreadable book stops the gate from
  sending anything while limits are in force. Failing open is how a protection
  quietly stops protecting.
* `alert=risk_limits_breached` on `risk` means the **standing** book already
  breaks a limit — fix it before adding to it.

The daily-loss limit **refuses new entries; it does not liquidate**. Closing is
yours to do deliberately.

## Exits at a price — never poll for them

When the user says *"close when it hits X"* — a stop, a target, "get me out at
1.1650" — the exit has to be held by something that is **not you**:

| What they want | Use | Why |
|---|---|---|
| Exit an **open** position at a level | `action='modify'` with `ticket` + `exit_at=X` | The **broker's server** holds the level. It fires in milliseconds, with no process and no model turn, and it survives the sandbox being paused or killed. |
| A condition the broker cannot hold (part of a position, a basket, a level that is not the stop) | `action='guard'` + `guard_action='arm'` | A detached tick-level watcher inside the sandbox, reading the tick stream every `interval_ms` (default 100 ms). |
| The stop should **move itself** — breakeven at 1R, then trail | the same `action='guard'`, with `guard_mode='breakeven'` or `'trail'` | A standing policy, not a level: the watcher re-evaluates it every tick and it never fires itself out. See **A stop that moves itself**. |
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
* If that first-tick fire is **refused by the broker**, `arm` answers `ok: false`
  with the retcodes and `alert: "close_retrying"`: the position is **still open**
  at a level you were asked to be out at. That is a different problem from a dead
  watcher and is named differently on purpose. The rule is NOT consumed and the
  watcher keeps retrying — see the retry loop below before reporting it as lost.
* **A refused close is retried until the broker accepts or a deadline passes.**
  MEASURED 2026-09-23, live: a rule armed with an impossible volume (1000 lots on
  a 0.01 position) was refused with retcode **10014 "Invalid volume"**, retried
  every **1.5 s** (measured gaps 1.548 / 1.548 / 1.547 s), and after **20
  attempts / 29.4 s** logged a single `close_gave_up` naming the retcodes and the
  ticket that is still open. In one sentence: a decided exit is honoured even
  after the price has moved back inside the level, because the decision was
  already made when the level was touched. The details:
  - `status` (and `positions.protection`, and `arm`) carry the retry state:
    `retrying[]` with `attempts`, `trying_for_s`, `retry_in_s`, `deadline_in_s`,
    or `gave_up[]` with `attempts`, `retcodes`, `gave_up_s_ago` and
    `next_window_in_s`. Both set `ok: false` — a caller who asked to be out is
    still in, and must not be told the level is covered.
  - `alert: "close_retrying"` means *exit in progress*: recovery is "nothing to
    do yet", and polling `guard_action='events'` for `fired` or `close_gave_up`
    is the whole follow-up. `alert: "close_gave_up"` means the watcher could not
    get out: close by hand (`action='close'`) or fix what the broker refused.
  - After giving up it is **parked for 60 s**, then a fresh window opens — a
    closed market is minutes from being closable, not never, so the rule is not
    abandoned. One loud line per window instead of a refusal storm at 10 Hz.
  - A watcher that restarts with a retry in flight resumes it and says so:
    `close_retry_resumed` in the event log (`{"rule_id", "symbol", "attempts"}`),
    continuing from the inherited attempt count — verified live across
    `guard stop` → `guard ensure` (attempts 1,2 before, `attempts: 3` inherited).
  - The rule is consumed only by a **confirmed** close, so a retry never has to
    be reconstructed from memory: it lives on the rule in `rules.json`.
* A failed `arm` tails only what **that** watcher wrote, so `log_tail` is not a
  previous run's `max 600 s` line; if the new watcher wrote nothing at all,
  `log_note` says so.
* **A guard has no time limit unless you set one.** `guard arm` with no
  `max_seconds` holds the level for as long as it takes to be touched — "close
  when it hits X" is a standing instruction, not a one-hour one. The old default
  of 3600 s is what stopped guards whose level had not arrived yet. `max_seconds`
  is honoured only when it is positive, and `ensure` inherits the budget the
  guard was armed with when the call does not restate one.
* **The guard reads every tick that was RECORDED, not one sample per loop.**
  MEASURED 2026-09-23, live: the watcher reads ONE tick per poll while the feed
  records as many as it likes — 37131 EURUSD ticks in 60 s (618.85 tick/s) in one
  reading that day, 282 rows over 60.5 s (~4.7 tick/s) in another 30 minutes
  later — so the ticks in between were never examined and a level touched inside
  a 100 ms gap was invisible. Do not quote a fixed percentage of "missed ticks":
  the rate is bursty and unexplainable from inside the box. The watcher now
  reads the recorded stream (`copy_ticks_from`) on each pass. `status` carries
  `ticks_scanned` per symbol; every event carries it too, and a fire names the
  tick that actually crossed (`crossing_msc`, `crossing_price`,
  `crossing_age_ms`). **`ticks_scanned: {}` on a symbol you know is trading means
  the scan is not reaching the stream** — investigate before claiming coverage.
* **A level touched and already back inside is REPORTED, never acted on.** The
  event is `level_touched_then_reverted` (`touch_price`, `touch_age_ms`,
  `price_now`), at most one line per rule per 5 s. Seeing the tick must not
  become acting on the tick: the price is back inside the level, so a close now
  would fill at a price the caller never asked to be out at. Closes still fire on
  the LIVE tick only. When someone asks "did it touch X", this event is the
  answer — and it is not a reason to say the position was closed.
* **`guard status` can OBSERVE instead of assert.** `wait_seconds` (capped at
  90 s, with `poll_seconds`) blocks until the event log grows, then returns the
  event with `observed`, `observed_event`, `waited_s`, `samples`; when nothing
  happens it returns `observed: false` with the same fields rather than a claim
  that all is well. ANY new event ends the wait — a `close_failed`, a
  `close_gave_up` or a `watcher_stop` matters as much as a `fired`. Use it when
  the caller asks you to watch a level, instead of polling in a loop.
* **The tick stream is stamped in the TERMINAL's clock, not the sandbox's.**
  MEASURED 2026-09-23, live: the tick clock ran **10799 s (~3 h) ahead** of
  `time.time()` inside Wine, and comparing the two made the every-tick scan
  silently empty — it asked for "ticks since now − 3 s" and was answered with
  20000 rows of history, every one older than the live tick, so `ticks_scanned`
  was `{}` on a guard that otherwise looked healthy. Anything that compares a
  tick stamp to `time.time()` is wrong; age a tick against another tick.
* The ceiling that remains: `symbol_info_tick` inside Wine costs **334.7 µs**
  (~2988 polls/s) and returns ONE tick per call, so polling faster buys nothing
  beyond that. A sub-poll spike through a level is still only catchable by an
  MQL5 EA's `OnTick`. The guard's *precision* is bounded by the poll; its
  *blindness* to recorded ticks is not.
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
  `protection.guard_live`, `protection.guard_alert`,
  `protection.guard_retrying_close` / `protection.guard_gave_up_close`, and a
  `warning` when rules are armed with nothing running. **Fixed 2026-09-23:** the
  bridge re-exec'd into Wine without passing `MT5_ROOT`, so it resolved
  `Path.home()` as Wine's `C:\users\user` and read the guard's files from a
  different, empty directory — `positions` answered `guard: {live: false,
  rules_armed: 0}` while a guard was live and retrying a refused close, i.e. the
  one call that reads open risk said nothing was watching. If a build answers
  `guard_live: false` while `guard status` says `running: true`, that is this bug
  and the CLI needs re-fetching, not re-arming.
* End a guard with `guard_action='stop'` (a stop **file**), never by killing a
  process: `pkill -f` matches the shell that launched it.

## Watching a trade while it is live — `action='watch'`

Once a position is open, the question stops being "what is the state" and becomes
"what is HAPPENING". Three separate calls answer it badly: `positions` for the
risk, `quote` for the price, `guard status` for whether anything is still
watching — three trips, three different instants, no memory between them. A
caller doing that is looking at three photographs and guessing at the film.

```
mt5_sandbox(action="watch", wait_seconds=60, poll_seconds=1, lines=20)
mt5_sandbox(action="watch", symbols="EURUSD XAUUSD", wait_seconds=60)
```

`watch` returns one frame of the film, every time:

| Field | What it answers |
|---|---|
| `positions`, `position_count` | the open risk, as you would get from `positions` |
| `prices` | live `bid`/`ask`/`mid` per watched symbol, plus `time_msc` |
| `price_path` | per symbol: `first_mid`, `last_mid`, `min_mid`, `max_mid`, `drift_pips`, `range_pips`, `samples` |
| `guard`, `guard_state` | liveness, `rules_armed`, `alert`, retrying/gave-up closes, heartbeat age |
| `ticks_scanned`, `near_miss` | proof the watcher is LOOKING, per symbol, as counts |
| `events` | the guard's own log, including `fired` with its measured `latency_ms` |
| `watched` | why this call came back: `observed_event`, `waited_s`, `samples`, `timed_out`, `capped_at_s`, `note` |

* **Omit `symbols` and it watches the symbols of the OPEN POSITIONS** — the set
  that actually carries risk. Name symbols only for a market you care about but
  have not traded yet.
* **`price_path` is the point.** A snapshot cannot tell "moved 3 pips and came
  back" from "sat still" — the same single number, two different markets. Over a
  60 s watch you get the travel, so "EURUSD drifted 2.1 pips over 60 s, range 3.4,
  no rule fired" is a sentence you can honestly write.
* **The wait ends on something that matters**, never on a price tick: a guard
  event (`fired`, `close_failed`, `close_gave_up`, `level_touched_then_reverted`,
  `watcher_stop`), a change in the **set of open positions**
  (`position_opened` / `position_closed`, with the tickets), or the watcher dying
  while you watched it. A price that merely moves does **not** end the wait — it
  is accumulated into `price_path` instead, or the wait would return instantly
  every time and observe nothing.
* **`watched.timed_out: true` is not a failure** — it means nothing happened in
  that window, and `note` says so plus how far the price travelled. Say that. Do
  not upgrade it into "the guard is protecting the position"; `watch` observes,
  it does not protect.
* **`ok: false` with an `alert` is a real alarm.** `watcher_stop` observed
  mid-watch, a `close_failed`/`close_gave_up`, a guard that is armed with nothing
  running, or a terminal that did not answer `positions_get`
  (`alert: "terminal_unavailable"` — the positions in that payload are **not**
  known to be the whole picture) all mean the position is not covered the way the
  caller thinks. Act on it or say it out loud.
* **Capped at 90 s per call.** There is no daemon and there never will be:
  "continuous" means a bounded blocking call, repeated. Call `watch` again to
  continue where the last one stopped.
* `watch` is **read-only** and works with `MT5_ALLOW_TRADING` off — observing is
  never the thing that moves money.

## The default playbook — Gold, 20-pip stop, 1:7

This is the strategy to trade unless the user names another one. It is encoded in
`nanobot/trading/gold_strategy.py`, and `action='plan'` runs it for you. **Do not
state a stop or a target from memory — compute it.**

```
mt5_sandbox(action="plan", symbol="XAUUSD", side="buy", entry=<ask for a buy,
            the bid for a sell>, volume=0.1, equity=<action='account' equity>,
            range_low=<session low>, range_high=<session high>, spread=<from quote>)
```

It returns `setup.sl` / `setup.tp` — pass those straight to `action="order"`, and
`action="guard"`/`modify` the same levels. It also returns the range sub-levels,
the risk in dollars **and** percent, and `violations`: every way the setup breaks
the playbook. **A non-empty `violations` means it is not this strategy** — say so
rather than placing it.

### The numbers

| | |
|---|---|
| Stop loss | **20 pips** — a strict, fixed stop |
| Reward-to-risk | **1:7** = **140 pips** of target |
| Entry | **at a range level**, not between levels |
| Bias filter | price beyond the MA Ribbon **and** beyond the 50% level, agreeing |

### A Gold pip is 0.10 — not the 0.01 the quote advertises

**This is the trap, and it is a factor of ten.** The broker quotes XAUUSD with
`digits=2`, so `point` is 0.01 and `10**-digits` is 0.01 — the *point*, not the
pip. MEASURED 2026-09-24 on a live Deriv-Demo terminal:
`quote XAUUSD → digits: 2, point: 0.01, spread: 18`.

Treat 0.01 as the pip and a 20-pip stop is **$0.20** — inside the spread of the
quote it was just read from, so the position is stopped out on the next tick, and
every level you ever express on Gold is 10x wrong. **A Gold pip is 0.10, so 20
pips is $2.00 and 140 pips is $14.00.**

The convention comes from the source guide's own worked example — `entry 4162.50,
SL 4160.50, TP 4176.50`, called "20 pips" and "140 pips". `4162.50 − 4160.50 =
2.00`. It is pinned in `gold_strategy.GOLD_PIP` and again in `scripts/mt5_cli.py`
(where `watch`/`guard` compute their own pips on the box), and a test holds the
two equal.

### The levels and the entry signals

Range sub-levels, from the session range: **25% / 50% / 62.5% / 75% / 87.5% /
100%**, plus the **150%** (1.5x) structure extension. The **62.5% "Golden
Retracement"** is the headline entry. The range indicator's divisor is **4.68**
for Gold; the guide states the number without its arithmetic, so `plan` returns
it in `range.divisor` rather than pretending to know the formula.

Enter only on a level, on one of three candles: **pin bar** (tail ≥ 2/3 of the
candle, rejection), **engulfing bar** (body swallows the previous body,
momentum), **inside bar** (break of the mother bar; the direction is unknown
until it breaks). Never chase a breakout — wait for the retracement to the level.

### The 20% risk parameter is a warning, not an instruction

The guide's Gold parameters also say **risk 20% of capital per trade**. `plan`
reports what that means and does not apply it: on a $10,302.92 account it is
**10.3 lots** at this stop, and the guide itself notes it "can lead to rapid
account depletion (ruin)". At **0.1 lot the same stop risks $20 — 0.19%**. Trade
the lot the user asked for, and if the arithmetic says the request is 103x
hotter than the playbook, say so in one line before placing it.

### What the strategy is not

A 1:7 target means the strategy loses most of the time by construction — seven
losses in eight still breaks even. So never present a setup as a likely win, and
never report a `plan` as a done trade: `plan` places nothing. The edge is in the
fixed stop and the level-based entry, not in the target being reached. If the
user asks for a different RR or stop, pass `rr=`/`sl_pips=`, and mention that
`violations` will then flag it as off-playbook.

## Polling a live trade in real time — not a cron

While a position is open, **watch it for real**. A scheduled task that checks the
price every five minutes is not watching, and it cannot see anything happen — it
samples a market that ticks continuously, and the model is told about it long
after the fact. Cron is for work that must happen with nobody watching. A trade
you are following is the opposite: you are there, so observe it.

```
# call this in a LOOP until the position is closed or the user says stop
mt5_sandbox(action="watch", watch_session="xau-leg-2", wait_seconds=90)
```

**Always pass `watch_session`.** Without it every call starts from zero: each one
reports its own first price, its own drift, its own range, and none of them can
answer "how has this trade gone?" — you are watching a different trade every
90 seconds. With it, the calls fold into one ledger on the box and each answer
carries:

| Field | Meaning |
|---|---|
| `session.price_path_total` | the **whole session's** first/last/min/max, drift and range in pips |
| `session.since_last_call` | `was` / `now` / `moved_pips` — the move since you last looked |
| `session.elapsed_s` | wall-clock time since the trade's first watch, across all calls |
| `session.samples_total` | samples taken across all calls |
| `session.calls` | how many times you have looked |

### You manage the trade — there is nobody else in the loop

The polling loop is not a status read-out. **You are the trade management.** No
cron, no EA, no automation runs between your calls: nothing closes a position or
takes a partial profit unless you decide it and call for it, and a `guard_mode`
rule is one you armed yourself. That is the design — a program cannot read *why*
the price is where it is, and you can.

The one exception is deliberate, and it is the one thing here that has to happen
while you are away: **the stop.** Nothing in a polling loop can move a stop on a
tick you did not poll, so when the plan says "breakeven at 1R" or "trail it",
arm it with `guard_mode='breakeven'`/`'trail'` and the sandbox's own watcher does
it on every tick — instead of you waking up at 2R to find the trade never stopped
carrying its risk. Then keep managing the rest by hand.

Every call hands you the arithmetic you need so you are deciding, not
calculating:

| Field (`trade_state`) | Meaning |
|---|---|
| `totals.total_r` / `best_r` / `worst_r` | how many R the open trade has made, per position and summed |
| `totals.risk_money`, `risk_pct_of_equity` | what is at risk right now, and as a share of the account |
| `positions[].r_multiple` | `(price − entry) / (entry − sl)` — a ratio, so it is right on Gold and FX alike |
| `positions[].pips_to_sl` / `pips_to_tp` | distance to each edge, in that symbol's own pip |
| `positions[].breakeven_price` | the entry — where the stop goes once the trade has paid for its risk |
| `positions[].breakeven_due` | set when the trade is at **≥1R and still carrying its risk** |
| `positions[].alert` | `no_stop` — a position with no stop at all |
| `session.trade_path` | `best_r` / `worst_r` / best and worst profit **across every call** |
| `notes` | the plain-language version of the above, when something needs saying |

`trade_state.notes` is not decoration. If it says a ticket is at 2R with its stop
still 40 pips away, **that is your cue to act on it** — `modify` the stop to
`breakeven_price`, or take part of the position off with `close`. If the plan is
"breakeven at 1R" then the cue to arm `guard_mode='breakeven'` is **before** the
trade reaches 1R, not `breakeven_due` appearing afterwards: a rule armed at 1R has
already missed the tick it exists for. If it says a
position has `no_stop`, stop watching and fix that first: you are one gap away
from an unbounded loss.

**Leave no position unwatched and no stop unset.** A trade you are polling has a
human's money on it; `timed_out: true` means nothing happened *yet*, not that you
are done. Watch until the position set is empty, or until you have told the user
something they need to answer.

### Don't narrate. Manage.

While the trade is being managed, **keep the loop to yourself**. "Still watching,
nothing yet" every 90 seconds is noise, and a user who asked you to run a trade
does not want forty status updates — they want the trade run. Work quietly and
speak only when one of these is true:

* **The job is done** — the position set is empty. Then report the outcome with
  the real numbers: every fill, the exit price of each part, the net P&L, and the
  R multiple. From `history`, not from your memory of the loop.
* **You need a decision only the user can make** — they said "close half at the
  first target" and the target is here but the size was never specified; or the
  setup has gone invalid and the choice is theirs.
* **Something is wrong that they must know now** — a position with no stop, an
  account that turns out to be netting, margin that will not cover the plan, the
  watcher dying, a broker rejection you cannot work around. A problem is not
  noise; silence on a problem is the failure mode.

Everything else is the loop doing its job. Acting on `trade_state` — taking a
partial off, moving a stop to breakeven — is **management, not a report**: do it
and carry on watching. Do not stop the loop to announce what you just did; the
numbers will be in the final summary, and stopping mid-trade to narrate is how a
trade ends up unwatched.

**Stay inside the turn while the trade is live.** "Quiet" means "no messages to
the user", never "end the turn and wait". If you reply with prose and no tool
call, the turn is over, the watcher has no driver, and the trade is now unwatched
until the human prompts you again — the opposite of autonomous. So each poll that
comes back still-open must be followed by another poll **in the same turn**, until
the position set is empty or one of the three report conditions above is met. If a
long wait is needed, use the blocking form (`status` with `wait_seconds`, or
`guard status wait_seconds=`) so the tool waits rather than you ending the turn to
sleep. The user asked you to run a trade, not to be told that a trade is running.

### How the loop works

1. `watch` blocks up to **90 s** and returns the moment something happens.
2. If `watched.timed_out` is `true`, **nothing happened** — that is a normal
   result, not a failure and not an error. Read the price path, think, call
   `watch` again with the same session.
3. If `watched.observed_event` is set, something happened (a fire, a refused
   close, a near miss, a dead watcher, a position opened/closed). Read it, decide,
   act.
4. Stop the loop when: the position set is empty (the trade is done), the user
   tells you to stop, or you have something to say to the user.

**Keep the thinking in the loop.** Each call gives you the cumulative path, the
delta since last time, the guard's tick counts and any near miss. That is enough
to say whether the trade is working, whether the level is being respected, and
whether the stop should move — say it, don't just keep polling in silence.

**The 90 s cap is not a limit on how long you watch.** It is the ceiling on one
sandbox command. Watching for an hour is ~40 calls, and they are one
observation because they share a session name. There is no daemon behind this and
there does not need to be one: the loop is you, and you are the part that thinks.

### Never poll with `quote`

`quote` in a loop costs a sandbox round trip per poll and returns a price with no
history, no guard state and no event log — and it is exactly the pattern that
misses a level touched between two polls. `watch` samples inside the box at 1 Hz
and hands you the path, for one round trip. Use `quote` once, when you want a
price; use `watch` when you want to know what the price is doing.

## Split trading — one idea, N positions

Instead of risking $100 as one 1.00-lot position, open **ten 0.10-lot positions
at the same price**. Same symbol, same direction, same stop, **same total risk** —
but the exits stop being all-or-nothing.

```
mt5_sandbox(action="split", symbol="XAUUSD", side="sell", volume=1.0,
            splits=10, sl=4294.18, tp=4278.18, group="xau-leg2")

# later: take three off into a run, leave seven working
mt5_sandbox(action="close", group="xau-leg2", count=3)
```

### Why it helps

* **Tiered exits.** Three off at the first target, seven left to run to the
  extension — you cannot do that with one ticket, you can only be all in or all out.
* **Free runners.** Close enough to cover the risk, then the remainder is a
  risk-free position with the original target still on it. That is where the
  extra profit actually comes from, not from the extra tickets.
* **Smaller decisions.** Each close is a small, reversible-feeling choice, which
  is easier to make by a rule than "close it all now".

### What makes it work — and the trap

* **"The same price" means *near* one price.** The tick is read once, before the
  loop, so every ticket is *requested* at the same price — but a market order
  fills at whatever the other side is when it lands, and N tickets land at N
  moments. MEASURED on a live account: ten XAUUSD tickets requested at 4284.15
  filled across **4284.06–4284.25** (1.9 pips on a 20-pip stop) from nothing but
  the market moving. `fill_price_first` / `fill_price_last` /
  `fill_dispersion_pips` report it, and `total_risk_money` is summed off each
  ticket's own fill rather than the requested price. Quote the fills; "all at one
  price" is not true and the entry rate is on the ticket list.
* **A split needs `sl` too**, for the same reason an order does — and here the
  reason is multiplied by the split count: without one, N tickets open with no
  server-side exit between them. `split` refuses without `sl` unless you pass
  `allow_no_stop=true`.
* **Every ticket must carry the same stop.** Ten 0.10 lots with a 20-pip stop risk
  exactly what one 1.00 lot with a 20-pip stop risks. The split multiplies
  *exits*, not risk. A split with stops on only some of the tickets multiplies
  the risk instead, and `split` reports the total risk it actually created.
* **It is not a grid.** `split` fires **once**, at one price, with one stop. Adding
  tickets as the price goes *against* you is the martingale the guide warns "can
  lead to rapid account depletion (ruin)". If the user asks for that, say what it
  is; do not quietly translate it into a split.
* **Cost is not always proportional.** Commission charged **per deal** is paid ten
  times where one 1.00-lot deal pays it once, and so are slippage and requotes.
  Spread cost is proportional to volume and is unaffected. On a 20-pip stop that
  difference matters — pass `check_cost=true` and read it before sending.
* **The volume must divide.** `split` rounds **down** to the broker's lot step and
  reports the leftover in `volume_left_over` rather than rounding up, because
  rounding up would risk more than the user asked for. A leftover that is not
  small is reported as a `warning`, and the split is smaller than requested on
  purpose.
* **A partial fill is not a position.** If only 6 of 10 tickets fill, the result
  is `alert=split_incomplete` with the volume actually open. Say that; never
  report a 10-ticket split that is 6 tickets.

### Reporting a split honestly

Ten tickets at $10 each is $100 of profit, exactly like one ticket at $100. If the
screenshot angle is the reason, that is the user's call — but when you report
P&L, add it up and give the real number.

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
