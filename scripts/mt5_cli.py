#!/usr/bin/env python3
"""Headless MetaTrader 5 command-line bridge (runs INSIDE the sandbox).

This is the single entry point the agent uses to drive MT5 over the command
line. It deliberately lives in the sandbox, never on the application host, so
that Wine, the MT5 terminal and the ``MetaTrader5`` python package cannot
consume the gateway's CPU/RAM.

Subcommands
-----------
  doctor            environment report: wine, xvfb, terminal, python bridge
  install           run the Wine + MT5 + bridge installer (scripts/install_mt5_sandbox.sh)
  start             launch terminal64.exe headless under Xvfb and wait for IPC
  stop              terminate the terminal
  login             connect to a broker account (--login --password --server)
  account           print account info (balance, equity, margin, currency)
  quote             current tick for one or more symbols
  candles           OHLCV bars (--symbol --timeframe --count)
  positions         open positions
  orders            pending orders
  history           closed deals (--days N)
  order             send an order (--symbol --side --volume [--sl --tp])
  close             close a position (--ticket N [--volume V])
  close_all         flatten every open position
  symbol            symbol metadata (digits, spread, min/max lot, trade mode)
  compile           compile an .mq5/.mqh file with MetaEditor's CLI
  logs              read the MT5 terminal log tail (--lines N)
  experts           read the MT5 Experts journal log tail (--lines N)
  run               raw python via MetaTrader5 if you need something exotic

Exit codes
----------
  0 success, 1 usage error, 2 not logged in / terminal unavailable,
  3 trade rejected by broker, 4 compile error.

Notes
-----
* Every subcommand prints a single JSON object on stdout so the agent can parse
  the result deterministically; human-readable text goes to stderr.
* ``--json`` is accepted for symmetry and is the default; ``--text`` switches to
  a compact human rendering.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MT5_ROOT = Path(os.environ.get("MT5_ROOT") or (Path.home() / ".mt5"))
WINE_PREFIX = Path(os.environ.get("WINE_PREFIX") or (Path.home() / ".wine-mt5"))
DISPLAY_NUM = os.environ.get("MT5_DISPLAY_NUM", "99")
METAEDITOR_MARKER = MT5_ROOT / ".metaeditor_path"
# Where find_terminal() caches the resolved terminal path, so repeated calls do
# not re-walk the Wine prefix.
TERMINAL_MARKER = MT5_ROOT / ".terminal_path"
LOGIN_STATE = MT5_ROOT / ".login.json"

TIMEFRAMES = {
    "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1", "MN1": "TIMEFRAME_MN1",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def emit(payload: dict[str, Any], *, text: str | None = None, code: int = 0) -> int:
    """Print one JSON object on stdout and optionally human text on stderr."""
    if text:
        print(text, file=sys.stderr)
    print(json.dumps(payload, default=str))
    return code


def fail(message: str, *, code: int = 1, **extra: Any) -> int:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return emit(payload, text=f"error: {message}", code=code)


def wine_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("WINEPREFIX", str(WINE_PREFIX))
    env.setdefault("DISPLAY", f":{DISPLAY_NUM}")
    # WINEDEBUG is deliberately NOT set here.
    #
    # Wine raises PEB heap-debug flags for any process while WINEDEBUG is present
    # in the environment — *including* WINEDEBUG=-all. MetaTrader treats those
    # flags as "a debugger is attached" and aborts with
    #   "A debugger has been found running in your system."
    # Any leftover value is therefore stripped, and one must never be added.
    env.pop("WINEDEBUG", None)
    # Wine's Mono/.NET and Gecko/HTML add-on prompts cannot be answered in a
    # headless container, and ``wineboot`` then wedges in setupapi for 10+ minutes.
    # Disabling both is what keeps prefix creation fast and non-interactive.
    env.setdefault("WINEDLLOVERRIDES", "mscoree,mshtml=")
    return env


def wine_bin() -> str:
    for candidate in ("wine", "wine64"):
        if subprocess.run(["which", candidate], capture_output=True).returncode == 0:
            return candidate
    return "wine"


def win_python() -> Path | None:
    """Locate the WINDOWS python inside the Wine prefix, if installed.

    The ``MetaTrader5`` PyPI package ships **only** win_amd64 wheels, so the
    bridge cannot be imported by the sandbox's Linux python — it has to run on a
    Windows interpreter under Wine (and talk to the terminal over Wine's IPC).
    ``mt5_cli.py`` therefore re-executes itself through ``wine python.exe``
    whenever this interpreter is present.
    """
    override = os.environ.get("MT5_WIN_PYTHON")
    candidates = [Path(override)] if override else []
    candidates.append(WINE_PREFIX / "drive_c" / "Python311" / "python.exe")
    for cand in candidates:
        if cand and cand.exists():
            return cand
    return None


def find_terminal() -> Path | None:
    if TERMINAL_MARKER.exists():
        cached = Path(TERMINAL_MARKER.read_text(encoding="utf-8").strip())
        if cached.exists():
            return cached
    drive_c = WINE_PREFIX / "drive_c"
    if not drive_c.exists():
        return None
    for exe in drive_c.rglob("terminal64.exe"):
        try:
            TERMINAL_MARKER.write_text(str(exe), encoding="utf-8")
        except OSError:
            pass
        return exe
    return None


def _find_exe(directory: Path, name: str) -> Path | None:
    """Find a Windows .exe *directly inside* directory, case-insensitively.

    Linux paths are case-sensitive and MT5 ships ``MetaEditor64.exe`` with
    capital letters while everything here is lowercase, so a literal match never
    found it on a good install.

    This is deliberately a SHALLOW scan. The tree-walking version used
    ``rglob("*")``, and a Wine ``drive_c`` holds tens of thousands of files
    (MQL5/ alone has thousands), so every call burned seconds of stat() traffic
    and pushed ``compile`` past the sandbox timeout — which surfaced as
    "the tool was not responding" and made the agent hand the .mq5 back to the
    user. MetaEditor always sits next to terminal64.exe, so shallow is also
    correct.
    """
    wanted = name.lower()
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.lower() == wanted:
                return entry
    except OSError:  # pragma: no cover - defensive
        return None
    return None


def _find_exe_deep(directory: Path, name: str) -> Path | None:
    """Bounded last-resort search, restricted to ``*.exe``."""
    wanted = name.lower()
    try:
        for candidate in directory.rglob("*.exe"):
            if candidate.name.lower() == wanted:
                return candidate
    except OSError:  # pragma: no cover - defensive
        return None
    return None


def find_metaeditor(terminal: Path | None = None) -> Path | None:
    """Locate MetaEditor64.exe (the ONLY MQL5 compiler). Cached + case-folded.

    Resolution order is cheap-first, and the result is memoised in a marker file
    like the terminal path so repeated compiles never re-walk the prefix.
    """
    if METAEDITOR_MARKER.exists():
        cached = Path(METAEDITOR_MARKER.read_text(encoding="utf-8").strip())
        if cached.is_file():
            return cached
        try:
            METAEDITOR_MARKER.unlink()
        except OSError:
            pass

    if terminal is None:
        terminal = find_terminal()
    if terminal is None:
        return None

    drive_c = WINE_PREFIX / "drive_c"
    found = _find_exe(terminal.parent, "metaeditor64.exe")
    if found is None:
        for rel in (
            "Program Files/MetaTrader 5",
            "Program Files/MetaQuotes Terminal 5",
        ):
            found = _find_exe(drive_c / rel, "metaeditor64.exe")
            if found is not None:
                break
    if found is None:
        found = _find_exe_deep(drive_c, "metaeditor64.exe")

    if found is not None:
        try:
            METAEDITOR_MARKER.parent.mkdir(parents=True, exist_ok=True)
            METAEDITOR_MARKER.write_text(str(found), encoding="utf-8")
        except OSError:
            pass
        return found
    return None


def terminal_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "terminal64.exe"], capture_output=True, text=True
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def mt5_module():  # noqa: ANN201 - returns module or None
    try:
        import MetaTrader5 as mt5  # noqa: N813
        return mt5
    except Exception:
        return None


def under_wine() -> bool:
    """True when this process is the Windows python running inside Wine."""
    return sys.platform.startswith("win") or bool(os.environ.get("MT5_UNDER_WINE"))


def require_bridge():  # noqa: ANN201
    mt5 = mt5_module()
    if mt5 is None:
        if not under_wine() and win_python() is not None:
            return None, fail(
                "mt5_cli.py must run inside Wine to reach the MT5 bridge. It should "
                "re-exec automatically; run it via mt5_cli.py (not directly) or set "
                "MT5_UNDER_WINE=1.",
                code=2,
            )
        return None, fail(
            "MetaTrader5 bridge is not installed. Run: mt5_cli.py install (the "
            "bridge requires the Windows python inside the Wine prefix because "
            "MetaTrader5 only publishes win_amd64 wheels).",
            code=2,
        )
    if not mt5.initialize():
        return None, fail(
            f"mt5.initialize() failed: {mt5.last_error()}. Run start first.", code=2
        )
    return mt5, None


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def _bridge_imports_under_wine() -> bool:
    """Verify the bridge really imports by running a child through Wine."""
    winpy = win_python()
    if winpy is None:
        return False
    try:
        proc = subprocess.run(
            [wine_bin(), str(winpy), "-c", "import MetaTrader5; print('ok')"],
            capture_output=True,
            text=True,
            env=wine_env(),
            timeout=180,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return "ok" in (proc.stdout or "")


def cmd_doctor(_: argparse.Namespace) -> int:
    terminal = find_terminal()

    def _which_version(binary: str) -> str | None:
        """Return ``<binary> --version`` output, or None when it is absent.

        ``doctor`` is also the first thing the tool runs before anything is
        installed, so every probe here must degrade to None instead of raising.
        """
        try:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                env=wine_env(),
                timeout=30,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return None
        return (proc.stdout or proc.stderr or "").strip() or None

    def _pgrep(pattern: str) -> bool:
        try:
            return subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, timeout=15
            ).returncode == 0
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return False

    wine_version = _which_version(wine_bin())
    winpy = win_python()
    info = {
        "ok": True,
        "wine": wine_version,
        "wine_installed": wine_version is not None,
        "wine_prefix": str(WINE_PREFIX),
        "prefix_ready": (WINE_PREFIX / "drive_c").exists(),
        "xvfb": _pgrep(f"Xvfb :{DISPLAY_NUM}"),
        "display": f":{DISPLAY_NUM}",
        "terminal_path": str(terminal) if terminal else None,
        "terminal_running": terminal_running(),
        "windows_python": str(winpy) if winpy else None,
        "python_bridge": winpy is not None,
        "bridge_imports_in_wine": _bridge_imports_under_wine() if winpy else False,
        "running_under_wine": under_wine(),
        "mt5_root": str(MT5_ROOT),
    }
    ready = bool(
        info["wine_installed"] and info["prefix_ready"] and terminal and winpy is not None
    )
    info["ready_for_trading"] = ready
    if ready:
        text = "MT5 stack ready"
    elif not info["wine_installed"]:
        text = "wine not installed — run: mt5_cli.py install"
    else:
        text = "MT5 stack incomplete — run: mt5_cli.py install"
    return emit(info, text=text)


def cmd_install(args: argparse.Namespace) -> int:
    script = Path(args.script).expanduser()
    if not script.exists():
        return fail(f"installer script not found: {script}")
    if not os.access(script, os.O_RDONLY):
        return fail(f"installer script is not readable: {script}")

    log_path = MT5_ROOT / "install.log"
    status_path = MT5_ROOT / "install.status"
    MT5_ROOT.mkdir(parents=True, exist_ok=True)
    # A stale success/failure marker from a previous attempt must not be
    # mistaken for this run's outcome.
    for stale in (status_path, log_path):
        if stale.exists():
            stale.unlink()

    if args.detach:
        # WHY DETACHED: the execution sandbox clamps every single command to a
        # fixed ceiling (900 s on Novita) while a full Wine + MT5 + bridge
        # install legitimately runs longer. Holding one command open would be
        # killed mid-install and leave a half-built prefix. So the installer is
        # launched with nohup/setsid and the caller polls ``status`` instead.
        inner = f"bash {shlex.quote(str(script))} > {shlex.quote(str(log_path))} 2>&1"
        quoted = shlex.quote(inner)
        proc = subprocess.run(
            ["sh", "-c", f"nohup setsid sh -c {quoted} >/dev/null 2>&1 & echo $!"],
            env=wine_env(),
            capture_output=True,
            text=True,
        )
        pid = (proc.stdout or "").strip()
        return emit(
            {
                "ok": True,
                "detached": True,
                "pid": pid,
                "status_file": str(status_path),
                "log_file": str(log_path),
                "hint": "Poll action='status' (or mt5_cli.py status) until stage is "
                "'done' or 'failed'. A full install takes ~10-25 minutes.",
            },
            text=f"install started detached (pid {pid}); poll status until done",
        )

    # Foreground mode: only usable when the caller's command ceiling exceeds the
    # install duration (e.g. a local run or a self-hosted box).
    env = wine_env()
    proc = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=int(args.timeout),
    )
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-40:])
    return emit(
        {"ok": proc.returncode == 0, "exit_code": proc.returncode, "log_tail": tail},
        text=tail,
        code=0 if proc.returncode == 0 else 2,
    )


def cmd_status(args: argparse.Namespace) -> int:
    """Report install progress and overall stack readiness (pollable)."""
    status_path = MT5_ROOT / "install.status"
    log_path = MT5_ROOT / "install.log"
    stage, message = "unknown", ""
    if status_path.exists():
        raw = status_path.read_text(encoding="utf-8", errors="replace").strip()
        stage, _, message = raw.partition("|")

    terminal = find_terminal()
    winpy = win_python()
    running = terminal_running()
    installed = bool(terminal and winpy is not None)
    failed = stage == "failed"
    done = installed or stage == "done"

    if status_path.exists():
        # An install log that is still growing means the detached installer is
        # alive; that is the only reliable "in progress" signal.
        pass
    in_progress = (not done) and (not failed) and _installer_alive()

    if not status_path.exists() and not done:
        stage, message = "not_started", "no install has been run in this sandbox"

    payload = {
        "ok": not failed,
        "stage": "done" if done else stage,
        "message": message,
        "in_progress": in_progress,
        "installed": installed,
        "terminal_path": str(terminal) if terminal else None,
        "terminal_running": running,
        "windows_python": str(winpy) if winpy else None,
        "windows_python_bytes": _wine_python_bytes(),
        "log_tail": _tail(log_path, int(args.lines)),
        "next": (
            "Stack installed — run action='start' with login/password/server."
            if done
            else "Keep polling action='status' until stage='done'."
            if in_progress
            else "Run action='install'."
        ),
    }
    return emit(payload, text=f"stage={payload['stage']}: {message}"[:2000],
                code=0 if not failed else 6)


def _installer_alive() -> bool:
    """True while a detached ``install_mt5_sandbox.sh`` is still running."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "install_mt5_sandbox.sh"], capture_output=True, timeout=15
        )
        return proc.returncode == 0
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def _wine_python_bytes() -> int:
    """Total size of the Windows python tree (0 when it does not exist yet)."""
    winpy = win_python()
    if winpy is None:
        return 0
    total = 0
    for path in winpy.parent.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _bridge_probe() -> dict[str, Any] | None:
    """Ask the Wine-side bridge for account info and return the parsed JSON.

    ``start`` runs on the Linux python, which can NEVER import MetaTrader5 (the
    package is Windows-only). Probing the module locally would therefore always
    look "not ready" and the terminal would be reported as unconnected even when
    it is fine. So readiness is delegated to a child invocation of this same
    script, which the re-exec layer automatically routes through Wine.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "account"],
            capture_output=True,
            text=True,
            timeout=240,
            env=wine_env(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return _extract_json(proc.stdout)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Parse the last balanced ``{...}`` object out of mixed process output."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def cmd_start(args: argparse.Namespace) -> int:
    terminal = find_terminal()
    if terminal is None:
        return fail("terminal64.exe not found. Run install first.", code=2)

    # Pre-seed credentials so the terminal can auto-connect on boot. MT5 will not
    # expose symbols/quotes over IPC until it has an account, and there is no GUI
    # to type one into — the CLI is the only way in.
    if getattr(args, "login", None) and args.password and args.server:
        cfg_dir = MT5_ROOT / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "common.ini").write_text(
            "[Common]\n"
            f"Login={int(args.login)}\n"
            f"Password={args.password}\n"
            f"Server={args.server}\n"
            "KeepPrivate=1\n",
            encoding="utf-8",
        )
        # Portable mode makes the terminal read config/ from MT5_ROOT instead of
        # the prefix's roaming profile, which is what lets the seeded login win.
        portable = True
    else:
        portable = bool(getattr(args, "portable", False))

    if not terminal_running():
        # Make sure a display exists even if the installer did not leave Xvfb up.
        subprocess.run(
            ["sh", "-c", f"pgrep -f 'Xvfb :{DISPLAY_NUM}' >/dev/null || "
                         f"(nohup Xvfb :{DISPLAY_NUM} -screen 0 1280x1024x24 "
                         f">/dev/null 2>&1 &)"],
            capture_output=True,
        )
        time.sleep(2)
        launch = [wine_bin(), str(terminal)]
        if portable:
            launch.append("/portable")
        subprocess.Popen(
            launch,
            env=wine_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    deadline = time.time() + int(args.wait)
    ready = False
    while time.time() < deadline:
        probe = _bridge_probe()
        # A terminal with no account still returns account=null; require an
        # actual account before declaring the stack ready for quotes/orders.
        if probe and probe.get("ok") and probe.get("account"):
            ready = True
            break
        time.sleep(5)

    return emit(
        {
            "ok": ready,
            "terminal_path": str(terminal),
            "running": terminal_running(),
            "ipc_ready": ready,
            "portable": portable,
            "hint": None
            if ready
            else "Terminal is up but has no account. Pass login/password/server to "
            "action='start' (or use action='login') so MT5 can connect; quotes and "
            "orders need a broker account.",
        },
        text="terminal ready" if ready else "terminal started but not connected to an account",
        code=0 if ready else 2,
    )


def cmd_stop(_: argparse.Namespace) -> int:
    subprocess.run(["pkill", "-f", "terminal64.exe"], capture_output=True)
    return emit({"ok": True, "stopped": True}, text="terminal stopped")


def cmd_login(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err

    # initialize() attaches to the running terminal; an explicit login is then
    # applied on top. Keeping these as two distinct steps means a credential
    # error is reported as a login failure rather than a generic init failure.
    if not mt5.initialize():
        return fail(f"terminal attach failed: {mt5.last_error()}", code=2)

    if args.login:
        if not args.password or not args.server:
            return fail("login requires --password and --server", code=1)
        if not mt5.login(int(args.login), password=args.password, server=args.server):
            return fail(f"login failed: {mt5.last_error()}", code=2)

    acct = mt5.account_info()
    payload = {"ok": True, "account": acct._asdict() if acct else None}
    if acct:
        LOGIN_STATE.write_text(
            json.dumps({"login": acct.login, "server": acct.server}), encoding="utf-8"
        )
    return emit(payload, text=f"logged in as {getattr(acct, 'login', 'unknown')}")


def cmd_account(_: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    acct = mt5.account_info()
    if acct is None:
        return fail(f"account_info() returned None: {mt5.last_error()}", code=2)
    return emit({"ok": True, "account": acct._asdict()})


def cmd_quote(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    out: dict[str, Any] = {}
    for symbol in args.symbol:
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        out[symbol] = {
            "tick": tick._asdict() if tick else None,
            "digits": getattr(info, "digits", None),
            "spread": getattr(info, "spread", None),
            "point": getattr(info, "point", None),
        }
    return emit({"ok": True, "quotes": out})


def cmd_candles(args: argparse.Namespace) -> int:
    # Validate the timeframe BEFORE touching the bridge so bad input produces a
    # usage error rather than a misleading "bridge not installed".
    tf_name = TIMEFRAMES.get(args.timeframe.upper())
    if tf_name is None:
        return fail(f"unknown timeframe {args.timeframe}; use {', '.join(TIMEFRAMES)}")
    mt5, err = require_bridge()
    if err is not None:
        return err
    mt5.symbol_select(args.symbol, True)
    rates = mt5.copy_rates_from_pos(args.symbol, getattr(mt5, tf_name), 0, int(args.count))
    if rates is None:
        return fail(f"copy_rates_from_pos failed: {mt5.last_error()}", code=2)
    rows = [
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "tick_volume": int(r["tick_volume"]),
        }
        for r in rates
    ]
    return emit({"ok": True, "symbol": args.symbol, "timeframe": args.timeframe.upper(), "bars": rows})


def cmd_positions(_: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    positions = mt5.positions_get()
    if positions is None:
        return fail(f"positions_get failed: {mt5.last_error()}", code=2)
    return emit({"ok": True, "count": len(positions), "positions": [p._asdict() for p in positions]})


def cmd_orders(_: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    orders = mt5.orders_get()
    if orders is None:
        return fail(f"orders_get failed: {mt5.last_error()}", code=2)
    return emit({"ok": True, "count": len(orders), "orders": [o._asdict() for o in orders]})


def cmd_history(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    import datetime as _dt

    end = _dt.datetime.now()
    start = end - _dt.timedelta(days=int(args.days))
    deals = mt5.history_deals_get(start, end)
    if deals is None:
        return fail(f"history_deals_get failed: {mt5.last_error()}", code=2)
    return emit({"ok": True, "count": len(deals), "deals": [d._asdict() for d in deals]})


def cmd_symbol(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    mt5.symbol_select(args.symbol, True)
    info = mt5.symbol_info(args.symbol)
    if info is None:
        return fail(f"symbol {args.symbol} not found: {mt5.last_error()}", code=2)
    return emit({"ok": True, "symbol": info._asdict()})


def _order_send(mt5, request: dict[str, Any]) -> dict[str, Any]:
    result = mt5.order_send(request)
    if result is None:
        return {"ok": False, "error": str(mt5.last_error()), "request": request}
    payload = {"ok": result.retcode == mt5.TRADE_RETCODE_DONE, "retcode": result.retcode,
               "comment": result.comment, "order": result.order, "deal": result.deal}
    if not payload["ok"]:
        payload["request"] = request
        payload["last_error"] = str(mt5.last_error())
    return payload


def cmd_order(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    symbol = args.symbol
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return fail(f"symbol {symbol} unavailable", code=2)

    side = args.side.lower()
    if side in ("buy", "long"):
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif side in ("sell", "short"):
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        return fail("side must be buy or sell", code=1)

    # Filling mode has to match what the broker's symbol actually supports.
    filling_name = getattr(info, "filling_mode", 0)
    filling = mt5.ORDER_FILLING_RETURN
    if filling_name == mt5.SYMBOL_FILLING_FOK:
        filling = mt5.ORDER_FILLING_FOK
    elif filling_name == mt5.SYMBOL_FILLING_IOC:
        filling = mt5.ORDER_FILLING_IOC

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(args.volume),
        "type": order_type,
        "price": float(price),
        "deviation": int(args.deviation),
        "magic": int(args.magic),
        "comment": args.comment or "powerx-mt5",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    if args.sl is not None:
        request["sl"] = float(args.sl)
    if args.tp is not None:
        request["tp"] = float(args.tp)

    payload = _order_send(mt5, request)
    code = 0 if payload["ok"] else 3
    note = "order filled" if payload["ok"] else f"order rejected: {payload.get('comment')}"
    return emit(payload, text=note, code=code)


def cmd_close(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    positions = mt5.positions_get(ticket=int(args.ticket))
    if not positions:
        return fail(f"no position with ticket {args.ticket}", code=2)
    pos = positions[0]
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return fail(f"no tick for {pos.symbol}", code=2)
    closing_long = pos.type == mt5.POSITION_TYPE_BUY
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": float(args.volume or pos.volume),
        "type": mt5.ORDER_TYPE_SELL if closing_long else mt5.ORDER_TYPE_BUY,
        "position": pos.ticket,
        "price": float(tick.bid if closing_long else tick.ask),
        "deviation": int(args.deviation),
        "magic": int(args.magic),
        "comment": "powerx-close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }
    payload = _order_send(mt5, request)
    return emit(payload, text="position closed" if payload["ok"] else "close failed",
                code=0 if payload["ok"] else 3)


def cmd_close_all(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    positions = mt5.positions_get() or []
    results = []
    for pos in positions:
        ns = argparse.Namespace(
            ticket=pos.ticket, volume=None, deviation=args.deviation, magic=args.magic
        )
        # Reuse the single-close path so filling/order-type logic stays identical.
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            results.append({"ticket": pos.ticket, "ok": False, "error": "no tick"})
            continue
        closing_long = pos.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": float(pos.volume),
            "type": mt5.ORDER_TYPE_SELL if closing_long else mt5.ORDER_TYPE_BUY,
            "position": pos.ticket,
            "price": float(tick.bid if closing_long else tick.ask),
            "deviation": int(args.deviation),
            "magic": int(args.magic),
            "comment": "powerx-close-all",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        results.append({"ticket": pos.ticket, **_order_send(mt5, request)})
    ok = all(r.get("ok") for r in results) if results else True
    return emit({"ok": ok, "closed": len(results), "results": results},
                text=f"closed {len(results)} position(s)", code=0 if ok else 3)


def installed_chain() -> dict[str, Any]:
    """Report whether the FULL Wine + MT5 chain is installed.

    An ``.mq5`` cannot be compiled by anything except MetaEditor running inside
    the installed Wine prefix, so every chain-dependent action must prove the
    chain exists before it does any work. A partial prefix (missing MetaEditor,
    missing Windows python, or a bridge that will not import) is NOT usable: the
    old code only checked ``terminal64.exe`` and then let a compile attempt fail
    in a way that looked like a code error.
    """
    terminal = find_terminal()
    winpy = win_python()

    # MetaEditor is the actual compiler and is named ``MetaEditor64.exe`` on
    # disk; resolve it case-insensitively so a good install is not reported broken.
    metaeditor = find_metaeditor(terminal)

    try:
        wine_present = (
            subprocess.run(["which", wine_bin()], capture_output=True).returncode == 0
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        wine_present = False

    missing: list[str] = []
    if not wine_present:
        missing.append("wine")
    if not (WINE_PREFIX / "drive_c").exists():
        missing.append("wine_prefix")
    if terminal is None:
        missing.append("terminal64.exe")
    if metaeditor is None:
        missing.append("metaeditor64.exe")
    if winpy is None:
        missing.append("windows_python")

    return {
        "installed": not missing,
        "missing": missing,
        "wine": wine_present,
        "wine_prefix": str(WINE_PREFIX),
        "terminal_path": str(terminal) if terminal else None,
        "metaeditor_path": str(metaeditor) if metaeditor else None,
        "windows_python": str(winpy) if winpy else None,
    }


def require_installed_chain(action: str) -> int | None:
    """Hard gate: refuse a chain-dependent action unless the chain is installed.

    THIS IS THE INSTALLATION RULE. Handing the agent an ``.mq5`` must never lead
    to a casual "compile" that skips provisioning: the only supported path is
    ``install`` (detached) -> poll ``status`` until ``stage="done"`` -> then
    ``compile``. Without this gate the tool cheerfully attempted a compile against
    a missing/partial prefix and then looked like an ordinary compile failure, so
    the agent treated MQL5 like any other source file instead of provisioning the
    Wine + MT5 chain first.
    """
    info = installed_chain()
    if info["installed"]:
        return None
    return fail(
        f"action='{action}' requires the installed MT5 chain (Wine + MetaTrader 5 "
        "+ MetaEditor + Windows python bridge), but it is missing: "
        f"{', '.join(info['missing']) or 'unknown'}. Do NOT try to compile or fix "
        "the MQL5 source some other way — follow the installation rules: run "
        "action='install' (it returns immediately and installs detached), then poll "
        "action='status' until stage='done', then retry.",
        code=5,
        stage="not_installed",
        missing=info["missing"],
        next="mt5_sandbox(action='install') then poll action='status' until stage='done'",
        chain=info,
    )


def cmd_compile(args: argparse.Namespace) -> int:
    """Compile an MQL5 source file via MetaEditor's command-line interface.

    MetaEditor ships alongside the terminal inside the Wine prefix. Its
    ``/compile`` switch writes errors to a .log next to the source, which is
    exactly what the agent needs to auto-fix code.
    """
    src = Path(args.file)
    if not src.exists():
        return fail(f"source file not found: {src}")

    # Installation rule: the chain must exist BEFORE a compile is attempted. A
    # .mq5 has no other compiler, so this is not a nicety — skipping it produces a
    # misleading "compile failed" that the agent then tries to fix in the source.
    gate = require_installed_chain("compile")
    if gate is not None:
        return gate

    terminal = find_terminal()
    # Case-insensitive: MT5 installs ``MetaEditor64.exe``, not ``metaeditor64.exe``,
    # and Linux would never match the lowercase spelling on a good install.
    metaeditor = find_metaeditor(terminal)
    if metaeditor is None:
        return fail("MetaEditor64.exe not found next to the terminal", code=2)

    # MetaEditor only emits a .ex5 when it can find MQL5/Include (Trade.mqh and
    # friends live there); default it so a plain `#include <Trade/Trade.mqh>`
    # compiles without the caller having to know the layout.
    include = f"/include:{args.include}" if args.include else ""

    log_path = src.with_suffix(".log")
    if log_path.exists():
        log_path.unlink()
    cmd = [wine_bin(), str(metaeditor), f"/compile:{src}", "/log", include]
    cmd = [c for c in cmd if c]
    proc = subprocess.run(cmd, env=wine_env(), capture_output=True, text=True,
                          timeout=int(args.timeout))
    log_text = ""
    # MetaEditor writes the log either as UTF-16 or UTF-8; try both.
    if log_path.exists():
        raw = log_path.read_bytes()
        for enc in ("utf-16", "utf-8", "latin-1"):
            try:
                log_text = raw.decode(enc)
                break
            except Exception:
                continue
    errors = [ln.strip() for ln in log_text.splitlines()
              if "error" in ln.lower() or "warning" in ln.lower()]
    ex5 = src.with_suffix(".ex5")
    ok = ex5.exists()
    return emit(
        {
            "ok": ok,
            "source": str(src),
            "ex5": str(ex5) if ex5.exists() else None,
            "errors": errors,
            "log": log_text[-4000:],
            "exit_code": proc.returncode,
        },
        text="compiled" if ok else "compilation failed",
        code=0 if ok else 4,
    )


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def cmd_logs(args: argparse.Namespace) -> int:
    logs_dir = MT5_ROOT / "logs"
    candidates = [p for p in sorted(logs_dir.rglob("*.log"))] if logs_dir.exists() else []
    if not candidates:
        # Fall back to the logs shipped inside the Wine prefix.
        candidates = [p for p in sorted(WINE_PREFIX.rglob("logs/*.log"))][-5:]
    payload = {
        "ok": bool(candidates),
        "files": [str(p) for p in candidates[-5:]],
        "tail": {str(p): _tail(p, int(args.lines)) for p in candidates[-3:]},
    }
    return emit(payload, code=0 if candidates else 2)


def cmd_experts(args: argparse.Namespace) -> int:
    base = MT5_ROOT / "MQL5" / "Logs"
    candidates = [p for p in sorted(base.rglob("*.log"))] if base.exists() else []
    payload = {
        "ok": bool(candidates),
        "files": [str(p) for p in candidates[-5:]],
        "tail": {str(p): _tail(p, int(args.lines)) for p in candidates[-3:]},
    }
    return emit(payload, text="\n".join(payload["tail"].values())[-4000:],
                code=0 if candidates else 2)


def cmd_run(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    scope = {"mt5": mt5, "MetaTrader5": mt5}
    try:
        exec(args.code, scope)  # noqa: S102 - explicit agent escape hatch
    except Exception as exc:  # noqa: BLE001
        return fail(f"{type(exc).__name__}: {exc}", code=2)
    result = scope.get("result")
    return emit({"ok": True, "result": result})


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mt5_cli.py",
        description="Headless MetaTrader 5 command-line bridge (sandbox-only).",
    )
    parser.add_argument("--text", action="store_true", help="human-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="environment report")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("install", help="install Wine + MT5 + python bridge")
    p.add_argument("--script", default=str(Path(__file__).with_name("install_mt5_sandbox.sh")))
    p.add_argument("--timeout", type=int, default=1800)
    # Detached is the default because sandbox commands are timeout-capped; the
    # caller polls ``status`` instead of holding one long command open.
    p.add_argument("--detach", dest="detach", action="store_true", default=True)
    p.add_argument("--foreground", dest="detach", action="store_false")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("status", help="install progress / stack readiness (pollable)")
    p.add_argument("--lines", type=int, default=25)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("start", help="launch the terminal headless")
    p.add_argument("--wait", type=int, default=180)
    p.add_argument("--login", required=False)
    p.add_argument("--password", required=False)
    p.add_argument("--server", required=False)
    p.add_argument("--portable", action="store_true")
    p.set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the terminal").set_defaults(func=cmd_stop)

    p = sub.add_parser("login", help="log in to a broker account")
    p.add_argument("--login", required=False)
    p.add_argument("--password", required=False)
    p.add_argument("--server", required=False)
    p.add_argument("--path", required=False)
    p.set_defaults(func=cmd_login)

    sub.add_parser("account", help="account info").set_defaults(func=cmd_account)

    p = sub.add_parser("quote", help="current ticks")
    p.add_argument("symbol", nargs="+")
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("candles", help="OHLCV bars")
    p.add_argument("--symbol", required=True)
    p.add_argument("--timeframe", default="M15")
    p.add_argument("--count", type=int, default=200)
    p.set_defaults(func=cmd_candles)

    sub.add_parser("positions", help="open positions").set_defaults(func=cmd_positions)
    sub.add_parser("orders", help="pending orders").set_defaults(func=cmd_orders)

    p = sub.add_parser("history", help="closed deals")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("symbol", help="symbol metadata")
    p.add_argument("symbol")
    p.set_defaults(func=cmd_symbol)

    p = sub.add_parser("order", help="send a market order")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell", "long", "short"])
    p.add_argument("--volume", type=float, required=True)
    p.add_argument("--sl", type=float, default=None)
    p.add_argument("--tp", type=float, default=None)
    p.add_argument("--deviation", type=int, default=20)
    p.add_argument("--magic", type=int, default=20240919)
    p.add_argument("--comment", default="powerx-mt5")
    p.set_defaults(func=cmd_order)

    p = sub.add_parser("close", help="close a position")
    p.add_argument("--ticket", type=int, required=True)
    p.add_argument("--volume", type=float, default=None)
    p.add_argument("--deviation", type=int, default=20)
    p.add_argument("--magic", type=int, default=20240919)
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("close_all", help="flatten all positions")
    p.add_argument("--deviation", type=int, default=20)
    p.add_argument("--magic", type=int, default=20240919)
    p.set_defaults(func=cmd_close_all)

    p = sub.add_parser("compile", help="compile .mq5 via MetaEditor")
    p.add_argument("--file", required=True)
    p.add_argument("--include", default=None)
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("logs", help="terminal log tail")
    p.add_argument("--lines", type=int, default=100)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("experts", help="Experts journal tail")
    p.add_argument("--lines", type=int, default=100)
    p.set_defaults(func=cmd_experts)

    p = sub.add_parser("run", help="raw python escape hatch")
    p.add_argument("--code", required=True)
    p.set_defaults(func=cmd_run)

    return parser


#: Actions that need the MetaTrader5 bridge (i.e. must run inside Wine).
_BRIDGE_ACTIONS = frozenset(
    {
        "login", "account", "quote", "candles", "positions", "orders",
        "history", "symbol", "order", "close", "close_all", "run",
    }
)


def _to_wine_path(p: Path | str) -> str:
    """Translate a Linux path into the Wine ``Z:`` drive form."""
    return "Z:" + str(p).replace("/", "\\")


def _reexec_under_wine(argv: list[str]) -> int | None:
    """Run a bridge action inside Wine and return the child's exit code.

    Returns None when no re-exec is needed (already under Wine, action does not
    need the bridge, or no Windows python exists).

    WHY THE TEMP-FILE DANCE
    -----------------------
    Python under Wine aborts with ``init_sys_streams: can't initialize sys
    standard streams / OSError: [WinError 6] Invalid handle`` when it inherits
    Linux pipes that Wine cannot map onto a console handle. Rather than fight
    that, the command is written into a .bat inside the prefix and executed by
    ``cmd.exe``, which provides real handles and redirects the output to a file.
    The file is then read back verbatim, so the caller still sees the CLI's
    single-JSON-object stdout.
    """
    if under_wine() or not argv:
        return None
    action = argv[0]
    if action not in _BRIDGE_ACTIONS:
        return None
    winpy = win_python()
    if winpy is None:
        return None

    drive_c = WINE_PREFIX / "drive_c"
    tmp_dir = drive_c / "mt5tmp"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    out_linux = tmp_dir / "stdout.txt"
    if out_linux.exists():
        out_linux.unlink()

    # Quote every argument for cmd.exe (double quotes, escape inner quotes).
    win_args = " ".join('"' + a.replace('"', '\\"') + '"' for a in argv)
    bat = tmp_dir / "run.bat"
    bat.write_text(
        "@echo off\r\n"
        f'"{_to_wine_path(winpy)}" "{_to_wine_path(Path(__file__).resolve())}" '
        f"{win_args} > \"C:\\mt5tmp\\stdout.txt\" 2>&1\r\n",
        encoding="utf-8",
    )

    env = wine_env()
    env["MT5_UNDER_WINE"] = "1"
    try:
        subprocess.run(
            [wine_bin(), "cmd", "/c", "C:\\mt5tmp\\run.bat"],
            env=env,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("MT5_WINE_TIMEOUT", "900")),
        )
    except subprocess.TimeoutExpired:
        return fail("timed out waiting for the MT5 bridge inside Wine", code=2)

    if out_linux.exists():
        # CP1252 is the default console codepage Wine uses; fall back safely.
        raw = out_linux.read_bytes()
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                sys.stdout.write(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    return 0


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        # Bridge actions re-exec into the Wine windows-python; everything else
        # (doctor/install/start/stop/compile/logs) runs natively on Linux.
        reexec_code = _reexec_under_wine(args_list)
        if reexec_code is not None:
            return int(reexec_code)

        args = build_parser().parse_args(args_list)
        return int(args.func(args))
    except KeyboardInterrupt:
        return fail("interrupted", code=1)
    except Exception as exc:  # noqa: BLE001
        return fail(f"{type(exc).__name__}: {exc}", code=1)


if __name__ == "__main__":
    raise SystemExit(main())