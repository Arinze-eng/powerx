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

- **Never `pkill -f terminal64`.** `-f` matches the whole command line, which
  includes the invoking shell — it kills your own command. Use
  `pkill -x terminal64.exe`. (`cmd_stop` was fixed for this.)
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

## Known open issue (not a blocker for login/compile)

The Windows Python bridge dies on some calls:

```
wine: Call from ... to unimplemented function ucrtbase.dll.crealf, aborting
```

This is a Wine/`ucrtbase` gap hit when loading the `MetaTrader5` bridge in
Python 3.11 under Wine 10, and it aborts the process. Login itself is unaffected
(the terminal authorizes on its own). To place orders, work around it — either
pin an older Wine, install the native `ucrtbase` via winetricks, or drive
trading from an MQL5 EA inside the terminal instead of the Python bridge.

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