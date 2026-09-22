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

Install the **broker's own** MT5 build. The input is the **server the account
lives on** — never a broker baked into the deployment:

```bash
python3 ~/.mt5/bin/mt5_cli.py install --server Exness-MT5Trial9
```

`--server` is matched against a registry (`BROKER_BUILDS` in `scripts/mt5_cli.py`)
that maps a server-name prefix to the build that can resolve it:

| Server prefix | Build | Install dir |
|---|---|---|
| `metaquotes` | generic (`MT5_INSTALLER_URL`) | `MetaTrader 5` |
| `exness` | `exness.technologies.ltd/mt5/exness5setup.exe` | `MetaTrader 5 EXNESS` |

Broker CDN slugs follow `download.mql5.com/cdn/web/<broker-slug>/mt5/<name>setup.exe`.
Find yours on the broker's own "Download MT5" page — the slug is in the link.

**Adding a broker needs no code change.** Export the registry from the deployment:

```bash
MT5_BROKER_BUILDS='icmarkets,ic.markets|https://download.mql5.com/cdn/web/<slug>/mt5/ic.exe|MetaTrader 5 IC Markets'
```

Records are `prefix1,prefix2|<installer-url>|<install-dir>`, separated by `;`.
Malformed records are ignored rather than failing the command — a bad hint must
not disable every MT5 action. Only URLs that have actually been fetched belong
here: guessed slugs on `download.mql5.com` 404 (`icmarkets.ltd`, `xm.global`,
`trading.point.ltd`, `fbs.markets`, `pepperstone.group` were all tried).

Through the agent tool the same thing is one call — it resolves the build from
`server`, installs it, and (when the terminal on the box is a different broker's)
replays the login automatically:

```jsonc
{"action": "start", "login": 10012768157, "password": "…", "server": "MetaQuotes-Demo"}
{"action": "install", "server": "Exness-MT5Trial9"}                      // explicit
{"action": "install", "server": "MyBroker-Demo",
 "broker_installer_url": "https://…/mybroker5setup.exe",                  // not in the registry
 "broker_dir_name": "MetaTrader 5 MyBroker"}
```

`broker_installer_url` / `broker_dir_name` remain for a broker the registry does
not know; give the tool the broker's own download link and dir name for that.

### Why the server, not the deployment

Branded builds **coexist** in one prefix (the branded installer refuses to
overwrite another broker's), so "which terminal is this" has to be recorded and
re-checked, not assumed:

* the installer writes `.installed.url` and `.broker_key` into `$MT5_ROOT`
* the install is skipped only when the done marker **and** the URL match, so a
  broker *switch* re-installs instead of short-circuiting on the old marker
* `.terminal_path` (the resolved-terminal cache) is invalidated when the
  requested broker differs from the cached one — it was never cleared before, so
  after a switch `start` booted the **old** broker, silently

### Fail fast on a build that cannot resolve the server

`start` / `login` run `preflight_server()` before touching MT5. When the server
belongs to a broker whose build is not the one installed — or the installed
terminal is branded and the server is unknown — the command returns in **under a
second** instead of blocking:

```json
{"ok": false, "failure": "server_not_in_terminal",
 "installed_broker": "exness", "requested_server": "MetaQuotes-Demo",
 "remedy": {"action": "install", "installer": "…", "dir_name": "MetaTrader 5"}}
```

Without it, MT5 does not error: it skips the connection and the bridge sits in
`initialize()` until its IPC timeout, which is exactly the hang this whole
document is about. An unknown server is never blocked preflight (`None`), because
the CLI must not refuse what it cannot reason about.

A second tell, when the login still produces no account: **zero `Network` lines**
in `logs/<date>.log`. An unresolvable name writes none — MT5 skips the connection
— while a wrong password writes one (`authorization failed`). `start` reports
`failure: "no_network_activity"` for the empty case.

Caveat, measured 2026-09-22: **zero lines is a signal, not proof.** A generic
MetaQuotes terminal that logged in *successfully* (balance read back over IPC)
also wrote zero `Network` lines, so an empty log cannot on its own convict the
server name. What can is the build: `start` compares `installed_broker_key()`
against `broker_for_server(server)` and reports
`failure: "server_not_in_terminal"` only for a real mismatch; otherwise the
`no_network_activity` hint gives both readings and both next steps.

Note the log is **UTF-16LE** — plain `grep Network logs/<date>.log` matches
nothing even when the lines exist. The CLI's own reader auto-detects the
encoding; if you inspect by hand, `iconv -f UTF-16LE` first.

### How to see this coming next time

`mt5_cli.py doctor` now probes for it and sets `terminal_has_broker_servers`:

```json
{"terminal_has_broker_servers": false,
 "installed_broker": "metaquotes",
 "supported_server_prefixes": ["exness", "metaquotes"],
 "ready_for_trading": true,
 "warning": "This terminal is the GENERIC MetaQuotes build: its Config/servers.dat
             carries no broker server list, so a BROKER server name cannot be
             resolved and a broker login will silently never even be attempted ...
             For a real broker, install the build that carries it:
             'install --server <broker server name>' ..."}
```

`terminal_has_broker_servers: false` with `installed_broker: "metaquotes"` means
a **real broker cannot log in** on this terminal; MetaQuotes' own demo servers
can (measured). The fix is `install --server <the broker's server>`, not a
hand-set URL.

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

1. `doctor` → read `installed_broker` and `supported_server_prefixes`. If the
   broker is already listed, nothing below the `install` step changes.
2. Not listed? Find the broker's branded MT5 download URL on their site and
   either export `MT5_BROKER_BUILDS`, or pass `broker_installer_url` +
   `broker_dir_name` once.
3. `install --server <broker server name>` (through the tool: `server` on the
   `install` action). It resolves the build and records which one landed.
4. `doctor` → confirm `installed_broker` matches and `terminal_has_broker_servers`
   is `true`.
5. `start` with login/password/server.
6. `account` → expect `balance`. On `-10005`, read `logs/<date>.log` (it is
   UTF-16, so `iconv` it or use `action='logs'`). A `Network` line without an
   authorization means credentials; no line at all is ambiguous — trust
   `installed_broker` against the server's broker instead of the line count.
7. Confirm the broker server name is exact — e.g. `Exness-MT5Trial9`, not
   `Exness-MT5Trial`. A near-miss fails like a wrong password.

MetaQuotes' own demo servers (`MetaQuotes-Demo`) are the one case that resolves on
the **generic** build, which is why the registry maps them to `url: ""` (the
generic installer) rather than to a broker build.

## Environment notes

* Runloop trial accounts cap devboxes at `MEDIUM` / 3600 s keep-alive. MT5 needs
  ≥ 1800 MB; the `MEDIUM` box has 3939 MB.
* `keep_alive` did **not** extend a trial devbox in testing — the box shut down at
  exactly 3600 s (`shutdown_reason: ttl_expired`) despite successful keep-alive
  calls. Do not rely on it to hold a trial sandbox open; re-create instead.
* Never `pkill -f terminal64.exe`: it matches the invoking shell and wineserver,
  killing your own command. Use `mt5_cli.py stop`.