# MT5 broker login — the servers.dat trap, and the wine64 launcher trap

Two independent failures kept a **broker** account from authorizing inside a
sandbox. Both were reproduced and fixed on 2026-09-22 (Runloop devbox, Debian 12,
WineHQ 10.0, MT5 build 6207, Exness trial account).

If you are debugging "MT5 will not log in", read §1 first — it is the one that
costs hours, because **nothing in the system reports an error**.

---

## 1. The generic MetaQuotes terminal cannot log in to a broker

### Symptom

Everything looks healthy, yet no login ever happens:

| Probe | Result |
|---|---|
| Terminal starts | ✅ |
| Config loaded | ✅ log: `successfully initialized from start config "C:\mt5cfg\powerx.ini"` |
| Log line confirming launch | ✅ log: `launched with C:\mt5cfg\powerx.ini` |
| **`Network` log lines** | ❌ **zero** |
| `mt5_cli.py account` | ❌ `{"ok": false, "error": "mt5.initialize() failed: (-10005, 'IPC timeout')"}` |

A working login writes **three** `Network` lines. Their *absence* — not an error
message — is the only tell:

```
Network  '477199408': authorized on Exness-MT5Trial9 through Access Point 1
Network  '477199408': terminal synchronized with Exness Technologies Ltd: 0 positions, 0 orders, 356 symbols
Network  '477199408': trading has been enabled, demo account - hedging mode
```

### Root cause

The MT5 terminal resolves a **broker server name** (`Exness-MT5Trial9`) to a host
through `Config/servers.dat` and `dnsperf.dat`.

MetaQuotes' **generic** installer ships a server database that contains no
brokers at all. Verified by parsing the file: the only strings it holds are

```
Copyright 2000-2026, MetaQuotes Ltd.
Servers
```

So the name cannot be resolved. MT5 does **not** error — it silently skips the
connection. The terminal never authorizes, therefore never exposes an account
over IPC, and the Python bridge reports `-10005 IPC timeout`. That error points
at Wine/IPC and sends you debugging an entirely innocent layer.

Measured table:

| Installer | `servers.dat` | Broker login |
|---|---|---|
| `metaquotes.software.corp/mt5/mt5setup.exe` (generic) | 50 544 B, 0 broker entries | ❌ never even attempts |
| `exness.technologies.ltd/mt5/exness5setup.exe` (branded) | 234 324 B, broker servers embedded | ✅ authorized first try |

### Fix

Install the **broker's own** MT5 build. Set these on the installer:

```bash
MT5_BROKER_INSTALLER_URL="https://download.mql5.com/cdn/web/exness.technologies.ltd/mt5/exness5setup.exe"
MT5_BROKER_DIR_NAME="MetaTrader 5 EXNESS"
```

Broker CDN slugs follow `download.mql5.com/cdn/web/<broker-slug>/mt5/<name>setup.exe`.
Exness is `exness.technologies.ltd`. Find yours from the broker's own
"Download MT5" page — the slug is in the link.

Through the agent tool:

```jsonc
{"action": "install",
 "broker_installer_url": "https://download.mql5.com/cdn/web/exness.technologies.ltd/mt5/exness5setup.exe",
 "broker_dir_name": "MetaTrader 5 EXNESS"}
```

Then `start` with login/password/server as before. No other step changes.

### How to see this coming next time

`mt5_cli.py doctor` now probes for it and sets `terminal_has_broker_servers`:

```json
{"terminal_has_broker_servers": false,
 "ready_for_trading": true,
 "warning": "This terminal is the GENERIC MetaQuotes build: its Config/servers.dat
             carries no broker server list, so a broker server name cannot be
             resolved and logins will silently never even be attempted ..."}
```

`ready_for_trading: true` with `terminal_has_broker_servers: false` means the
chain is installed but **broker login is impossible**. Trust the second field.

### Two traps this creates

* **Both terminals coexist.** The branded installer refuses to overwrite a
  generic install; it lands in `Program Files/MetaTrader 5 EXNESS` alongside
  `Program Files/MetaTrader 5`. `find_terminal()` therefore resolves the branded
  build **explicitly and first** — an unqualified `rglob("terminal64.exe")`
  returns whichever the filesystem lists first, and picking the generic one
  silently reintroduces this whole failure.
* **The installer's success check must be directory-scoped.** A bare
  `find <prefix> -iname terminal64.exe` matches the *other* build's binary,
  concludes success, and hands the bridge a terminal that cannot reach the
  broker. It now waits on the specific `Program Files/<MT5_BROKER_DIR_NAME>` path.

---

## 2. `/usr/bin/wine` is 32-bit and dies on a no-IA32 kernel

### Symptom

The installer aborts almost immediately:

```
[mt5-install] initialising wine prefix at /home/user/.wine-mt5 (this takes minutes)
[mt5-install] installer exited with code 2
```

The prefix contains **no `drive_c`**. It reads like an OOM kill or a wedged
`wineboot`. It is neither.

### Root cause

WineHQ's `/usr/bin/wine` is a **32-bit** ELF. Some modern kernels ship with no
IA32 emulation at all — on the Runloop devbox `/proc/sys/abi/ldt16` does not
exist and `ia32` is absent from `/proc/cpuinfo`, so even the 32-bit loader fails:

```
$ /usr/bin/wine --version
bash: /usr/bin/wine: cannot execute binary file: Exec format error
$ /lib/ld-linux.so.2 --version
bash: /lib/ld-linux.so.2: cannot execute binary file: Exec format error
```

`wine64` (64-bit) runs fine.

The script selected the launcher with `command -v wine`, which **returns 0 for a
binary that cannot execute**. So it picked the dead launcher, `wineboot` failed
instantly, and `set -e` + the `ERR` trap reported `code 2` while the log's last
line still read "initialising wine prefix" — a misdirection, not a diagnosis.

### Fix

**Probe** the launcher instead of trusting `which`:

```bash
_wine_works() {
  local bin="$1"
  command -v "$bin" >/dev/null 2>&1 || return 1
  env -u WINEDEBUG timeout 60 "$bin" --version >/dev/null 2>&1
}
WINE_BIN=""
for _cand in wine wine64; do
  _wine_works "$_cand" && { WINE_BIN="$_cand"; break; }
done
```

A working `wine` passes on the first try and is used unchanged, so normal hosts
are unaffected. Version detection uses the same probe (`wine_major`), otherwise
the script reads `0` and "helpfully" reinstalls a Wine that is already present.
The MQL5-library launch also uses `"$WINE_BIN"` rather than a bare `wine`.

64-bit-only is sufficient: `mt5setup.exe`, the embeddable Windows Python and the
`MetaTrader5` `win_amd64` wheels are all 64-bit (the installer's PE header
reports machine `0x8664`).

---

## 3. `winbindd` must be *running*, not merely installed

Wine's named-pipe support for the MetaTrader terminal needs winbind. Installing
the `winbind` package only puts binaries on disk — nothing starts the daemon, and
no `/etc/samba/smb.conf` exists. The terminal still boots and logs in, but the
bridge cannot attach (`-10005 IPC timeout`).

The installer now starts `winbindd` (writing a minimal `smb.conf` if absent).
Best-effort: it never fails an otherwise-good install.

---

## Verified result

```
$ python3 ~/.mt5/bin/mt5_cli.py doctor
{"wine": "wine-10.0", "prefix_ready": true,
 "terminal_path": ".../MetaTrader 5 EXNESS/terminal64.exe",
 "terminal_has_broker_servers": true,
 "bridge_imports_in_wine": true, "ready_for_trading": true}

$ python3 ~/.mt5/bin/mt5_cli.py account
{"ok": true, "account": {"login": 477199408, "balance": 500.0, "equity": 500.0,
 "currency": "USD", "server": "Exness-MT5Trial9",
 "company": "Exness Technologies Ltd", "trade_allowed": true}}
```

---

## Checklist for a new broker

1. Find the broker's branded MT5 download URL from their site.
2. `install` with `broker_installer_url` + `broker_dir_name`.
3. `doctor` → confirm `terminal_has_broker_servers: true`.
4. `start` with login/password/server.
5. `account` → expect `balance`; if you get `-10005`, read the terminal log for
   `Network` lines before touching anything else.
6. Confirm the broker server name is exact — e.g. `Exness-MT5Trial9`, not
   `Exness-MT5Trial`. A near-miss fails like a wrong password.

## Environment notes

* Runloop trial accounts cap devboxes at `MEDIUM` / 3600 s keep-alive. MT5 needs
  ≥ 1800 MB; the `MEDIUM` box has 3939 MB.
* `keep_alive` did **not** extend a trial devbox in testing — the box shut down at
  exactly 3600 s (`shutdown_reason: ttl_expired`) despite successful keep-alive
  calls. Do not rely on it to hold a trial sandbox open; re-create instead.
* Never `pkill -f terminal64.exe`: it matches the invoking shell and wineserver,
  killing your own command. Use `mt5_cli.py stop`.