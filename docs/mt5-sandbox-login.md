# MT5 in a Novita sandbox — verified bring-up & login

Everything here was verified end-to-end on 2026-09-20 in a real sandbox
(Wine 10.0, MT5 build 6204, `powerx-base-4g-c2` template, 4 GB RAM).

## TL;DR — the login that works

```bash
python3 ~/.mt5/bin/mt5_cli.py start \
  --login <ACCOUNT> --password '<PASSWORD>' --server <SERVER> --wait 300
```

`start` now writes a `/config:` ini and launches:

```
wine terminal64.exe /portable /config:C:\mt5cfg\powerx.ini
```

Successful authorization looks like this in
`.../MetaTrader 5/logs/YYYYMMDD.log` (UTF-16LE):

```
Network  '<login>': previous successful authorization performed from <ip>
Network  '<login>': terminal synchronized with MetaQuotes Ltd.: 0 positions, 0 orders, 12375 symbols
Network  '<login>': trading has been enabled, demo account - hedging mode
```

## What does NOT work (all tested)

| Approach | Result |
|---|---|
| `terminal64.exe /login:X /password:Y /server:Z` | **Silently ignored.** Only `/login:`, `/config:`, `/profile:`, `/portable` are real switches; anything else falls back to defaults, so the terminal boots with no account and emits **zero** Network log lines. |
| Seeding only `$MT5_ROOT/config/common.ini` | MT5 reads the file (it appends its own `Environment=` key to the loaded file) but does not authorize from it on a fresh prefix. |
| `mt5.initialize(login=..., server="<unknown name>")` | **Hangs forever**, it does not time out. Do not build a readiness probe on it without a hard timeout. |
| `mt5.initialize()` with `MetaQuotes-Demo` before auth lands | Fails cleanly; the terminal needs time before IPC exposes an account. |

## Timing (the part that bites)

Authorization is **not** instant. Measured on a MetaQuotes demo account:

- ~0–60 s: boot, IP discovery, first TCP connect (drops, then retries)
- ~130 s: `previous successful authorization` → `terminal synchronized`
- with `Server=MetaQuotes-Demo` the platform resolves the real host itself
  (`demo.metaquotes.net`) and even accepts `demo.metaquotes.net:443` directly.

So **wait ≥180 s** before concluding login failed. `--wait` now defaults to 300.

## Sandbox gotchas

- **`pkill` cannot target the terminal at all — by either flag.**
  `pkill -x terminal64.exe` never matches (Wine's comm is `main`), and
  `pkill -f terminal64` matches the invoking shell *and* `wineserver`, so it
  kills your own command or the whole Wine session. Resolve PIDs from
  `/proc/<pid>/cmdline` instead (`mt5_cli.py stop` now does). (`cmd_stop` fixed.)
- Launch long installs/terminal detached, exactly like the tool does:
  ```bash
  setsid bash $HOME/.mt5/bin/install_mt5_sandbox.sh > $HOME/.mt5/install.log 2>&1 &
  ```
  Never inline, and never with a long explicit `timeout:` on the SDK call — the
  sync command stream tears down and you lose the output.
- Read the terminal log with `iconv -f UTF-16LE -t UTF-8 <log> | sed 's/\r//'`.
- `mt5_cli.py` **re-execs itself under Wine**; the Linux python cannot import
  `MetaTrader5`. Use `run --code "<python>"` as the escape hatch.
- Sandbox **egress works**: TCP to `demo.metaquotes.net:443/444/80` connects and
  holds, and real TLS handshakes succeed. Network is not the blocker.

## The `ucrtbase.dll.crealf` bridge crash — SOLVED

Re-verified 2026-09-20 (Novita `powerx-base-4g-c2`, 3 939 MB, Debian 12, Wine
10.0, MT5 build 6204, MetaTrader5 5.0.6180). The bridge used to die on import:

```
wine: Call from 00006FFFFF40CF77 to unimplemented function ucrtbase.dll.crealf, aborting
```

**Root cause is numpy, not the MT5 package.** `import numpy` alone aborts at the
same address; the C99 complex-math entry points (`crealf`/`cimagf`/…) are simply
not exported by Wine 10's *builtin* `ucrtbase.dll`, and numpy 2.x calls them from
its compiled `_multiarray_umath` at import time.

The installer now pins `numpy<2`, which fixes it outright — verified:

| step | result |
|---|---|
| `import numpy` (2.4.6) | **aborts** on `ucrtbase.dll.crealf` |
| `pip install "numpy<2"` | installs numpy 1.26.4 |
| `import numpy, MetaTrader5` | `BRIDGE_IMPORT_OK 5.0.6180 1.26.4` |
| `mt5.initialize()` | `True` |
| `mt5.account_info()` | login `10012768157`, `100000.0 USD`, `trade_allowed: true` |
| `mt5.order_send(...)` | reaches the broker and returns a real retcode |

**Do not re-try the two dead ends** (both measured, neither works):

* `winetricks -q --force vcrun2022` → exits 0, but `ucrtbase.dll` is
  **byte-identical** before and after and the abort still fires.
* `HKCU\Software\Wine\DllOverrides` → `ucrtbase = native,builtin` → same abort
  (there is no native `ucrtbase.dll` in the prefix to switch to).

### Why it hung instead of erroring

Wine answers an unimplemented-function call by **starting `winedbg`**, which on a
headless box waits forever on a dialog nobody can answer. Every bridge call
therefore burned the full sandbox command timeout (900 s) and came back as a bare
`TimeoutException` with no diagnostic — indistinguishable from "the tool is not
responding". The installer now sets

```
HKCU\Software\Wine\WineDbg /v ShowCrashDialog REG_DWORD 0
```

so such a call aborts immediately with the real error text.

## Order filling mode (the bug that blocked every trade)

`mt5_cli.py` used to do:

```python
if filling_name == mt5.SYMBOL_FILLING_FOK:   # AttributeError
```

**`SYMBOL_FILLING_FOK` / `SYMBOL_FILLING_IOC` do not exist** in the Python
package. Verified against 5.0.6180:

```python
[n for n in dir(mt5) if "FILLING" in n]
# ['ORDER_FILLING_BOC', 'ORDER_FILLING_FOK', 'ORDER_FILLING_IOC', 'ORDER_FILLING_RETURN']
```

So **every** `order` call died with
`AttributeError: module 'MetaTrader5' has no attribute 'SYMBOL_FILLING_FOK'`,
no matter the symbol or credentials — trading looked impossible while login,
quotes and account info all worked fine.

Two further corrections the fix relies on:

* `SymbolInfo.filling_mode` is a **bitmask**, not a scalar to compare against:
  `1 = FOK`, `2 = IOC`, `4 = RETURN`. (EURUSD reported `1`.)
* Retcode **10030** (`unsupported filling mode`) is a mode mismatch, not a trade
  decision, so `order`/`close`/`close_all` now retry each supported mode in turn
  instead of guessing once.

## `stop` / process detection: match on argv, never on `comm`

Wine starts the terminal through its loader, so the kernel command name is
`main`/`start.exe`, not `terminal64.exe`:

```
15662  start.exe  start.exe /exec /home/user/.wine-mt5/drive_c/Program Files/MetaTrader 5/terminal64.exe /portable
15666  main       C:\Program Files\MetaTrader 5\terminal64.exe /portable /config:C:\mt5cfg\powerx.ini
```

measured while the terminal was healthy:

```
pgrep -x terminal64.exe | wc -l   ->  0     # so `pkill -x` was a NO-OP: stop never stopped anything
pgrep -f terminal64.exe | wc -l   ->  4     # but this also matches the calling `bash -lc`
```

`terminal64.exe` also sits **inside** `wineserver`'s argv
(`wineserver -p /home/user/.wine-mt5 …`), so a `-f` pattern match hits
wineserver too — killing it tears down every Wine process. Both
`terminal_running()` and `cmd_stop()` now read `/proc/<pid>/cmdline` and act only
on the real terminal PIDs, excluding the current process. `stop` now reports
`{"stopped": [15662, 15666], "still_running": []}`.

## Markets close — 10018 is not a bug

On a Sunday, EURUSD/XAUUSD are closed. A correctly built request still reaches
the broker and comes back as `{"retcode": 10018, "comment": "Market closed"}` —
that is **success for the plumbing**. To see a fill when FX is shut, use a 24/7
crypto symbol on MetaQuotes-Demo (BTCUSD/ETHUSD), or check
`SYMBOL_TRADE_MODE`/last-tick age first. Do not read 10018 as a code defect.

## Compile (verified working)

```bash
python3 ~/.mt5/bin/mt5_cli.py compile --file "<MQL5 data tree>/Experts/X.mq5"
```

Source must live in the MQL5 data tree for `<angle>` includes to resolve.
`ComplexEA.mq5` (CTrade / CSymbolInfo / 4 indicator handles / OnTradeTransaction)
built with `0 errors, 26 warnings, 525 ms` → 44 KB `.ex5`.
A deliberately broken file returns `ok:false`, `ex5:null`, exit code `4`, and
per-error `file(line,col) : error NNN: message` — good enough for an LLM fix loop.

Data tree used by the installer (portable mode):
`$WINE_PREFIX/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/MQL5`