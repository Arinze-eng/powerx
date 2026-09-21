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
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

#: Contract version of this CLI, checked by the host-side bootstrap before a
#: downloaded copy is used. MUST be bumped together with ``_CLI_VERSION`` in
#: ``nanobot/agent/tools/mt5_sandbox.py`` whenever the tool/CLI contract changes.
#:
#: WHY: the sandbox's network path caches GitHub raw responses by path, so a
#: branch URL can quietly deliver a revision several pushes old. The bootstrap
#: greps for this marker so a stale file is rejected instead of executed — the
#: agent then sees a loud warning rather than debugging code that is not running.
CLI_VERSION = "2026-09-21.4"

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

#: Upper bound on the JSON this CLI prints for a log-reading command.
#:
#: MEASURED FAILURE (2026-09-21, real Novita sandbox): the execution sandbox
#: wrapper keeps only the LAST ``_MAX_RESULT_CHARS`` (16 000) characters of a
#: command's output. ``logs --lines 60`` produced ~43 000 characters of JSON
#: (MT5 logs are UTF-16LE, so every real character arrived as TWO NUL-interleaved
#: characters and then expanded into a 6-character ``\u00XX`` escape each), the
#: head of the JSON -- including its opening ``{`` -- was sliced off, and the
#: host-side parser found no JSON at all. The agent was then told "MT5 command
#: produced no JSON result ... run action='install' first", which is nonsense
#: after a successful install. Keeping the whole payload under this budget makes
#: that impossible; the tails are trimmed instead.
_LOG_PAYLOAD_BUDGET = 7_000
#: Per-file cap for a single log tail (before the whole-payload budget above).
_LOG_TAIL_MAX_CHARS = 4_000
#: How long one readiness probe may block inside ``start``.
_PROBE_TIMEOUT = 90
#: Budget for list-heavy payloads (``symbols`` inventories).
_LIST_PAYLOAD_BUDGET = 6_000

#: Widest broker UTC offset a quote clock may sit at, for deciding whether the
#: server is streaming. See ``cmd_symbols``: MT5 reports tick time in server
#: time, and the bench box is UTC, so the newest tick of a LIVE session leads the
#: box clock by that offset (measured +3 h on MetaQuotes-Demo). Anything beyond a
#: full zone range is not an offset, it is a market that stopped ticking.
_ZONE_SKEW_TOLERANCE_S = 14 * 3600


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
    """Whether the MT5 terminal process is alive.

    Uses :func:`_terminal_pids` rather than ``pgrep -f``: the latter also matches
    the ``bash -lc`` that invoked us whenever its command string mentions
    ``terminal64.exe``, which made ``start`` believe a dead terminal was running
    (and made ``pkill -f`` kill the caller).
    """
    return bool(_terminal_pids())


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
        # Distinguishes the installer's credential-less "materialise the MQL5
        # library" terminal from one that was launched with our /config: file.
        # Without this an agent polling status sees only "terminal_running": true
        # and reasonably but wrongly assumes the terminal is usable.
        "terminal_has_credentials": _terminal_has_credentials() if running else False,
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


def _bridge_probe(timeout: int = _PROBE_TIMEOUT) -> dict[str, Any] | None:
    """Ask the Wine-side bridge for account info and return the parsed JSON.

    ``start`` runs on the Linux python, which can NEVER import MetaTrader5 (the
    package is Windows-only). Probing the module locally would therefore always
    look "not ready" and the terminal would be reported as unconnected even when
    it is fine. So readiness is delegated to a child invocation of this same
    script, which the re-exec layer automatically routes through Wine.

    ``timeout`` is bounded and configurable because an unbounded probe is itself
    a hang: ``mt5.initialize()`` blocks for the full IPC timeout (~240 s) when the
    terminal is up but not authorized, so ONE probe used to swallow the entire
    ``--wait`` window and the loop never re-checked a terminal that was still
    authorizing.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "account"],
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout)),
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
    #
    # IMPORTANT (verified in a Wine 10 + MT5 build 6204 sandbox):
    #
    # * ``/login:X /password:Y /server:Z`` on the command line does NOT work. The
    #   official docs list only ``/login:``, ``/config:``, ``/profile:`` and
    #   ``/portable``; an unrecognised switch is silently ignored ("the default
    #   value will be used"), so the terminal boots with no account and never
    #   emits a single Network log line.
    # * Writing ``common.ini`` is not enough either. MT5 reads it (it appends its
    #   own ``Environment=`` key to the file it loaded) but does not treat a
    #   plain ``common.ini`` as an authorization source on a fresh prefix.
    # * What DOES work is an explicit ``/config:<file>`` with all three of
    #   Login / Password / Server under ``[Common]``. That reliably produced:
    #     Network  '10012768157': previous successful authorization
    #     Network  '10012768157': terminal synchronized ... 12375 symbols
    #     Network  '10012768157': trading has been enabled, demo account
    #
    # ``/config:`` files are used read-only, which is fine and actually desirable
    # here: the platform must not rewrite our credentials file.
    credentials = bool(getattr(args, "login", None) and args.password and args.server)
    if credentials:
        common_ini = (
            "[Common]\n"
            f"Login={int(args.login)}\n"
            f"Password={args.password}\n"
            f"Server={args.server}\n"
            "KeepPrivate=1\n"
            "NewsEnable=0\n"
            "CertInstall=1\n"
            "\n"
            "[Experts]\n"
            "AllowLiveTrading=1\n"
            "AllowDllImport=0\n"
            "Enabled=1\n"
        )
        # Keep a copy at MT5_ROOT/config for humans and for non-portable boots.
        cfg_dir = MT5_ROOT / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "common.ini").write_text(common_ini, encoding="utf-8")

        # The file actually handed to the terminal via /config: lives inside the
        # prefix so it is reachable through a stable C:\ path.
        launcher_dir = WINE_PREFIX / "drive_c" / "mt5cfg"
        launcher_dir.mkdir(parents=True, exist_ok=True)
        launcher_ini = launcher_dir / "powerx.ini"
        launcher_ini.write_text(common_ini, encoding="utf-8")
        launch_config_arg = "/config:C:\\mt5cfg\\powerx.ini"

        # Portable mode keeps the data tree in the install dir, which is the path
        # the installer prepares (Common\MQL5\Include and friends).
        portable = True
    else:
        portable = bool(getattr(args, "portable", False))
        launch_config_arg = None

    # A terminal that is already up is NOT automatically the right terminal.
    #
    # MEASURED FAILURE (2026-09-21, real Novita sandbox): ``start`` was given
    # login/password/server, wrote both ini files correctly, and then skipped the
    # launch because *a* terminal was running -- the one the installer leaves
    # behind while it materialises the MQL5 library (it only kills it once
    # ``timeout 600 wine terminal64.exe`` returns, so it is frequently still alive
    # when the agent calls ``start``). That terminal has no ``/config:`` argument,
    # so it booted with no account and never authorized. The command still
    # reported ok=false with the hint "Pass login/password/server", which is
    # actively misleading -- the agent HAD passed them and had no way to tell that
    # they were dropped on the floor.
    #
    # So: when credentials are supplied, the terminal that serves them must have
    # been launched with our ``/config:`` file. If it was not, restart it.
    restarted_for_credentials = False
    if credentials and _terminal_has_credentials():
        # Already launched with credentials. If it is also live, report success
        # immediately instead of re-waiting the full --wait window.
        quick = _bridge_probe(timeout=min(60, max(15, int(args.wait))))
        if quick and quick.get("ok") and quick.get("account"):
            return emit(
                {
                    "ok": True,
                    "terminal_path": str(terminal),
                    "running": True,
                    "ipc_ready": True,
                    "portable": portable,
                    "reused_running_terminal": True,
                    "account_login": (quick.get("account") or {}).get("login"),
                    "hint": None,
                },
                text="terminal already running and connected",
            )
    elif credentials and _terminal_processes():
        stale = _stop_terminal_processes()
        restarted_for_credentials = True
        print(
            f"restarted terminal process(es) {stale} so /config: credentials apply",
            file=sys.stderr,
        )

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
        if launch_config_arg:
            launch.append(launch_config_arg)
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
        # Bounded probe: an unbounded one (240 s) could consume the entire --wait
        # window in a single call, so the loop never got a second look at a
        # terminal that was still authorizing.
        probe = _bridge_probe(timeout=min(_PROBE_TIMEOUT, max(15, int(deadline - time.time()))))
        # A terminal with no account still returns account=null; require an
        # actual account before declaring the stack ready for quotes/orders.
        if probe and probe.get("ok") and probe.get("account"):
            ready = True
            break
        time.sleep(5)

    payload: dict[str, Any] = {
        "ok": ready,
        "terminal_path": str(terminal),
        "running": terminal_running(),
        "ipc_ready": ready,
        "portable": portable,
        "credentials_applied": credentials,
        "restarted_stale_terminal": restarted_for_credentials,
    }
    if ready:
        payload["hint"] = None
    elif credentials:
        # Credentials WERE supplied: telling the agent to supply them again is the
        # bug that made this look unfixable. Point at the real next step instead.
        payload["hint"] = (
            "Credentials were written and the terminal was launched with "
            f"/config: but no account appeared within {int(args.wait)}s. Check the "
            "terminal log with action='logs' for 'authorization failed' / 'invalid "
            "account' lines, and confirm the server name matches the account "
            f"(got server={args.server!r}, login={args.login!r}). The account must "
            "exist on that server; MetaQuotes-Demo logins are created by the "
            "MetaQuotes demo registration, not by this platform."
        )
    else:
        payload["hint"] = (
            "Terminal is up but has no account. Pass login/password/server to "
            "action='start' (or use action='login') so MT5 can connect; quotes and "
            "orders need a broker account."
        )
    return emit(
        payload,
        text="terminal ready" if ready else "terminal started but not connected to an account",
        code=0 if ready else 2,
    )


def _terminal_processes() -> list[tuple[int, list[str]]]:
    """``(pid, argv)`` for every running MT5 terminal process.

    Same /proc-based discovery as :func:`_terminal_pids`, but the argv is kept so
    callers can tell *how* the terminal was launched -- specifically whether it
    was given our ``/config:`` credentials file.
    """
    found: list[tuple[int, list[str]]] = []
    me = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                parts = [p.decode("utf-8", "replace")
                         for p in handle.read().split(b"\x00") if p]
        except OSError:
            continue
        program = parts[0] if parts else ""
        if program.lower().endswith("terminal64.exe"):
            found.append((pid, parts))
            continue
        for arg in parts[1:]:
            if arg.lower().endswith("terminal64.exe") and not program.endswith("bash"):
                found.append((pid, parts))
                break
    return found


def _terminal_has_credentials() -> bool:
    """True when a running terminal was launched with our ``/config:`` file.

    A terminal started WITHOUT it (the installer's own "materialise the MQL5
    library" launch, or an older credential-less ``start``) boots with no account
    and will never authorize, no matter how correct the credentials are. That
    distinction is the whole reason ``start`` used to look like it ignored the
    login/password/server arguments.
    """
    for _, argv in _terminal_processes():
        for arg in argv:
            if arg.startswith("/config:") or arg.lower() == "/config:":
                return True
    return False


def _stop_terminal_processes() -> list[int]:
    """SIGTERM then SIGKILL every terminal process; return the PIDs handled."""
    handled: list[int] = []
    for pid, _ in _terminal_processes():
        try:
            os.kill(pid, signal.SIGTERM)
            handled.append(pid)
        except OSError:
            continue
    if handled:
        time.sleep(3)
        for pid, _ in _terminal_processes():
            try:
                os.kill(pid, signal.SIGKILL)
                if pid not in handled:
                    handled.append(pid)
            except OSError:
                continue
        # The IPC socket lingers briefly; a relaunch that races it fails to bind.
        time.sleep(2)
    return handled


def _terminal_pids() -> list[int]:
    """PIDs of the running MT5 terminal, found without shell self-matches.

    Kept as the public seam used by ``stop`` and the tests; the scanning lives in
    :func:`_terminal_processes`.
    """
    return [pid for pid, _ in _terminal_processes()]


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the terminal by PID, never by a command-line pattern.

    A pattern-based kill either misses (``-x``) or suicides (``-f``); see
    :func:`_terminal_pids`.
    """
    killed = _stop_terminal_processes()
    remaining = _terminal_pids()
    return emit(
        {"ok": not remaining, "stopped": killed, "still_running": remaining},
        text=f"stopped {len(killed)} terminal process(es)"
        + (f", {len(remaining)} still running" if remaining else ""),
    )


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


def _server_clock_offset(mt5: Any) -> int:
    """Seconds the broker's clock runs ahead of this box's clock, or 0 if unknown.

    The MetaTrader5 python wrapper takes naive datetimes as SERVER time (it
    forwards them as-is to the terminal), while the box runs on UTC. So a history
    window built from the box clock is really asking for a window that ended
    three hours in the broker's past — which is why ``history`` could not see the
    deals that had been placed seconds earlier.

    Measured on MetaQuotes-Demo, 2026-09-21: ticks ran 10 799 s (UTC+3) ahead of
    the box. A tick that is further off than any real UTC offset can explain is
    not an offset, it is a market that stopped ticking (weekend close, dead
    feed), so it is rejected and the caller falls back to the box clock.
    """
    probes = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "XAUUSD")
    latest = 0
    for name in probes:
        try:
            tick = mt5.symbol_info_tick(name)
        except Exception:  # noqa: BLE001 - a missing probe symbol is not fatal
            continue
        latest = max(latest, int(getattr(tick, "time", 0) or 0))
    if not latest:
        # The usual majors are not guaranteed to exist on every server.
        for info in (mt5.symbols_get() or [])[:200]:
            tick = mt5.symbol_info_tick(getattr(info, "name", ""))
            latest = max(latest, int(getattr(tick, "time", 0) or 0))
    offset = int(latest - time.time()) if latest else 0
    return offset if -_ZONE_SKEW_TOLERANCE_S <= offset <= _ZONE_SKEW_TOLERANCE_S else 0


def cmd_history(args: argparse.Namespace) -> int:
    """Deals in a window that is anchored to the BROKER's clock, not the box's.

    MEASURED FAILURE (2026-09-21, live MetaQuotes-Demo): a market buy filled with
    retcode 10009 and appeared in ``positions``, but ``history --days 1`` and
    ``--days 7`` both returned only the account's opening deposit. The window was
    ``datetime.now() - days`` .. ``datetime.now()`` from the UTC box, while the
    terminal reads those datetimes as SERVER time (UTC+3 here) — so the window
    ended three hours in the broker's past and every deal of this session fell
    outside it. The agent could place a trade and then find no record of it: the
    exact "it says it traded but I cannot check" failure this CLI exists to fix.
    """
    mt5, err = require_bridge()
    if err is not None:
        return err
    import datetime as _dt

    offset = _server_clock_offset(mt5)
    box_now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    server_now = box_now + _dt.timedelta(seconds=offset)
    # The window ends in the FUTURE on purpose. A deal that has already been
    # closed cannot be hidden by a clock that is off — a wrong or unavailable
    # server offset can only ever shift the window further back, and the margin
    # absorbs it. Only the lookback the caller asked for is honoured.
    end = server_now + _dt.timedelta(hours=6)
    start = end - _dt.timedelta(days=int(args.days))
    deals = mt5.history_deals_get(start, end)
    if deals is None:
        return fail(f"history_deals_get failed: {mt5.last_error()}", code=2)
    # Newest first. ``_fit_payload`` trims a list from the END, so the order has
    # to be descending for a long window to drop the oldest deals rather than the
    # ones the agent just placed.
    rows = sorted((d._asdict() for d in deals), key=lambda d: d.get("time", 0), reverse=True)
    payload = {
        "ok": True,
        "count": len(rows),
        "order": "newest_first",
        "server_clock_offset_s": offset,
        "window": {
            "from": start.isoformat(sep=" ", timespec="seconds"),
            "to": end.isoformat(sep=" ", timespec="seconds"),
            "timezone": "broker server time",
        },
        "deals": rows,
    }
    return emit(_fit_payload(payload, _LIST_PAYLOAD_BUDGET))


def cmd_symbols(args: argparse.Namespace) -> int:
    """Discover what can actually be traded, instead of guessing symbol names.

    MEASURED FAILURE (2026-09-21, live MetaQuotes-Demo account): the agent was
    asked to trade and reached for ``BTCUSD``/``ETHUSD`` because they are the
    obvious 24/7 instruments. MetaQuotes-Demo does NOT carry crypto at all, so
    every attempt died with ``symbol BTCUSD not found: (-4, 'Terminal: Not
    found')`` and ``copy_rates_from_pos failed: (-1, 'Terminal: Call failed')``
    — errors that read like a broken bridge rather than "that instrument is not
    offered here". There was no way to ask the terminal what it does offer, so
    the trade step was effectively unreachable without guessing.

    Reports name, group, trade mode, lot limits and whether the market has
    produced a tick recently (i.e. is open right now) — the two things needed to
    pick a workable symbol.
    """
    mt5, err = require_bridge()
    if err is not None:
        return err

    needle = (getattr(args, "filter", "") or "").strip().upper()
    # The terminal only streams symbols it knows about; the list is complete
    # without symbol_select.
    infos = mt5.symbols_get() or []

    # MEASURED FAILURE (2026-09-21, live MetaQuotes-Demo, 04:41 UTC, terminal log
    # "GMT+0"): the first version of this compared a tick's ``time`` against the
    # SANDBOX clock and reported ``last_tick_age_s: -10798`` — a negative age, a
    # quote from the future. MT5 returns tick time in SERVER time (measured:
    # box 04:41 UTC, tick 07:41, so this server is UTC+3), so subtracting the box
    # clock produced a nonsense age that could never exceed ``fresh_seconds``:
    # the market_open flag was true for every symbol no matter what.
    #
    # Two clocks, two questions, so measure both against the broker's own clock:
    #   * age vs the newest tick on the server -> "is THIS symbol stale versus
    #     the rest of the market" (skew cancels out exactly, no timezone maths);
    #   * newest tick vs the box clock -> "is the server streaming at all", which
    #     is what separates a live session from a frozen weekend close. A live
    #     quote sits at the broker's UTC offset (bounded by the real zone range),
    #     while a closed market's newest quote is behind the box clock by the
    #     whole gap — ~48 h over a weekend, which no offset can explain away.
    ticks: dict[str, int] = {}
    for info in infos:
        name = getattr(info, "name", "")
        if needle and name.upper().find(needle) == -1:
            continue
        tick = mt5.symbol_info_tick(name)
        ticks[name] = int(getattr(tick, "time", 0) or 0)
    latest = max(ticks.values()) if ticks else 0
    now = time.time()
    ahead = (latest - now) if latest else 0
    # Conservative by construction: the bench is a UTC box and the demo server
    # this CLI is aimed at runs at UTC+3 (measured), so requiring the newest
    # quote to lead the box clock by no more than a full zone range keeps a live
    # session marked open while a weekend close (-48 h) and an empty feed (0)
    # both read as closed. ``broker_clock_skew_s`` is reported either way, so a
    # server outside that band is visible rather than silently mislabelled.
    streaming = bool(latest) and -int(args.fresh_seconds) <= ahead <= _ZONE_SKEW_TOLERANCE_S

    rows: list[dict[str, Any]] = []
    for info in infos:
        name = getattr(info, "name", "")
        if name not in ticks:
            continue
        trade_mode = int(getattr(info, "trade_mode", 0) or 0)
        if getattr(args, "tradable", False) and trade_mode == 0:
            # trade_mode 0 == SYMBOL_TRADE_MODE_DISABLED
            continue
        tick_time = ticks[name]
        age = (latest - tick_time) if (latest and tick_time) else None
        fresh = bool(
            streaming
            and age is not None
            and age <= int(args.fresh_seconds)
        )
        if getattr(args, "tradable", False) and not fresh:
            continue
        rows.append(
            {
                "name": name,
                "group": str(getattr(info, "path", "") or "").split("\\")[0],
                "trade_mode": trade_mode,
                "visible": bool(getattr(info, "visible", False)),
                "volume_min": float(getattr(info, "volume_min", 0) or 0),
                "volume_step": float(getattr(info, "volume_step", 0) or 0),
                "spread": int(getattr(info, "spread", 0) or 0),
                "digits": int(getattr(info, "digits", 0) or 0),
                "filling_mode": int(getattr(info, "filling_mode", 0) or 0),
                "market_open": fresh,
                # Age measured against the broker's newest tick, never the box
                # clock, so it is meaningful across timezones.
                "last_tick_age_s": age,
                "last_tick_time": tick_time or None,
            }
        )

    rows.sort(key=lambda r: (not r["market_open"], r["name"]))
    total = len(infos)
    shipped = rows[: max(1, int(args.limit))]
    payload: dict[str, Any] = {
        "ok": True,
        "total_symbols": total,
        "matching": len(rows),
        "market_open_now": sum(1 for r in rows if r["market_open"]),
        "broker_latest_tick_time": latest or None,
        "broker_clock_skew_s": int(ahead) if latest else None,
        "sandbox_clock_skew_s": int(ahead) if latest else None,
        "server_streaming": streaming,
        "filter": needle or None,
        "symbols": shipped,
        "note": (
            "Choose a symbol with market_open=true. Tick times are SERVER time "
            "(this server ran ~3 h ahead of the box clock when measured), so "
            "last_tick_age_s is measured against the broker's newest tick and "
            "never against the box clock. market_open also requires the server to "
            "be streaming at all (see server_streaming / broker_clock_skew_s); if "
            "market_open_now is 0 the session is closed, so an order will come "
            "back as retcode 10018 'Market closed' — that is plumbing success, "
            "not a bug in the bridge."
        ),
    }
    if len(rows) > len(shipped):
        payload["truncated"] = True
        payload["note"] += f" Showing {len(shipped)} of {len(rows)}; pass --limit/--filter."
    return emit(_fit_payload(payload, _LIST_PAYLOAD_BUDGET))


def cmd_symbol(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    mt5.symbol_select(args.symbol, True)
    info = mt5.symbol_info(args.symbol)
    if info is None:
        return fail(f"symbol {args.symbol} not found: {mt5.last_error()}", code=2)
    return emit({"ok": True, "symbol": info._asdict()})


# ``SymbolInfo.filling_mode`` is a BITMASK of the modes the symbol accepts.
#
# The MetaTrader5 python module does NOT export ``SYMBOL_FILLING_FOK`` /
# ``SYMBOL_FILLING_IOC`` — only ``ORDER_FILLING_*`` (verified against 5.0.6180:
# ``[n for n in dir(mt5) if "FILLING" in n]`` returns exactly
# ``ORDER_FILLING_BOC/FOK/IOC/RETURN``). Referencing the SYMBOL_ names raised
# ``AttributeError: module 'MetaTrader5' has no attribute 'SYMBOL_FILLING_FOK'``
# and killed every single ``order`` call, so trading was unreachable even though
# login, quotes and the account all worked. The flag values come from the
# terminal's SYMBOL_FILLING_MODE enum.
SYMBOL_FILLING_FOK_FLAG = 1
SYMBOL_FILLING_IOC_FLAG = 2
SYMBOL_FILLING_RETURN_FLAG = 4

#: Broker rejects a request whose type_filling the symbol does not support.
RETCODE_UNSUPPORTED_FILLING = 10030


def filling_candidates(mt5, info: Any) -> list[int]:
    """Every ``ORDER_FILLING_*`` the symbol supports, most preferred first.

    Guessing one mode is not enough: a FOK-only symbol rejects an IOC request
    with retcode 10030, and the previous code then reported a trade failure for
    what was purely a mode mismatch.
    """
    mask = int(getattr(info, "filling_mode", 0) or 0)
    out: list[int] = []
    # IOC first: it is the mode that succeeds on both market and closing orders
    # at MetaQuotes, while FOK can fail on fast-moving symbols.
    if mask & SYMBOL_FILLING_IOC_FLAG:
        out.append(mt5.ORDER_FILLING_IOC)
    if mask & SYMBOL_FILLING_FOK_FLAG:
        out.append(mt5.ORDER_FILLING_FOK)
    if mask & SYMBOL_FILLING_RETURN_FLAG:
        out.append(mt5.ORDER_FILLING_RETURN)
    if not out:
        # Zero/unknown mask: try the order the broker is most likely to accept.
        out = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
    return out


def _order_send(mt5, request: dict[str, Any],
                fillings: list[int] | None = None) -> dict[str, Any]:
    """Send a trade request, retrying every supported filling mode.

    Only retcode 10030 ("unsupported filling mode") triggers a retry — a
    rejection for insufficient margin, invalid stops or a market closure is a
    real answer, and resending it would just spam the broker.
    """
    last: dict[str, Any] | None = None
    for filling in (fillings or [request.get("type_filling", mt5.ORDER_FILLING_RETURN)]):
        req = dict(request)
        req["type_filling"] = filling
        result = mt5.order_send(req)
        if result is None:
            last = {"ok": False, "error": str(mt5.last_error()), "request": req}
            continue
        payload: dict[str, Any] = {
            "ok": result.retcode == mt5.TRADE_RETCODE_DONE,
            "retcode": result.retcode,
            "comment": result.comment,
            "order": result.order,
            "deal": result.deal,
            "filling_used": filling,
        }
        if payload["ok"]:
            return payload
        payload["request"] = req
        payload["last_error"] = str(mt5.last_error())
        last = payload
        if result.retcode != RETCODE_UNSUPPORTED_FILLING:
            return payload
    if last is None:
        return {"ok": False, "error": "no filling mode was attempted", "request": request}
    return last


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

    # Filling has to be one the symbol actually supports, and the choice is a
    # bitmask lookup, not a constant comparison — see filling_candidates().
    fillings = filling_candidates(mt5, info)

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
        "type_filling": fillings[0],
    }
    if args.sl is not None:
        request["sl"] = float(args.sl)
    if args.tp is not None:
        request["tp"] = float(args.tp)

    payload = _order_send(mt5, request, fillings)
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
    # Hard-coding ORDER_FILLING_RETURN made every close fail with retcode 10030
    # on symbols that only advertise FOK/IOC, so resolve it from the symbol.
    payload = _order_send(mt5, request,
                          filling_candidates(mt5, mt5.symbol_info(pos.symbol)))
    return emit(payload, text="position closed" if payload["ok"] else "close failed",
                code=0 if payload["ok"] else 3)


def cmd_close_all(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    positions = mt5.positions_get() or []
    results = []
    for pos in positions:
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            results.append({"ticket": pos.ticket, "ok": False, "error": "no tick"})
            continue
        # Filling mode is per-symbol, so the metadata has to be fetched for each
        # position rather than assumed.
        info = mt5.symbol_info(pos.symbol)
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
        # Same filling-mode resolution as a single close: a hard-coded
        # ORDER_FILLING_RETURN is rejected (10030) on FOK/IOC-only symbols.
        fillings = filling_candidates(mt5, info) if info is not None else None
        if fillings:
            request["type_filling"] = fillings[0]
        results.append({"ticket": pos.ticket, **_order_send(mt5, request, fillings)})
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


def _ensure_data_tree_includes(metaeditor: Path, src: Path) -> bool:
    """Mirror the stock MQL5 standard library into the source's data tree.

    MetaEditor resolves ``#include <Trade/Trade.mqh>`` relative to the MQL5 data
    directory that owns the source file. The installer creates that directory but
    leaves its ``Include`` folder empty (the real headers are unpacked next to the
    terminal in ``Program Files``), and ``--include`` does NOT override this. The
    resulting ``error 106: ... Include\\Trade\\Trade.mqh  not found`` looks like a
    defect in the user's ``.mq5`` and reliably sends agents off editing working
    code — the exact trap this helper closes.

    Returns ``True`` when the library is present in the data tree afterwards, so
    the caller can safely drop its own ``/include:`` flag (which would otherwise
    make MetaEditor build a doubled path and fail all over again).

    Best-effort by design: a read-only or alien layout must never turn a compile
    into a crash, so every filesystem error is swallowed.
    """
    try:
        stdlib = metaeditor.parent / "MQL5" / "Include"
        if not stdlib.is_dir():
            return False

        # Walk up from the source to the MQL5 data root (the parent of Experts/
        # Include/ Scripts/ ...). Sources are normally placed under it, but a
        # source compiled from an arbitrary path still needs a sane target.
        data_root: Path | None = None
        for parent in [src.parent, *src.parents]:
            if parent.name == "MQL5":
                data_root = parent
                break
        if data_root is None:
            data_root = (
                WINE_PREFIX
                / "drive_c"
                / "users"
                / "user"
                / "AppData"
                / "Roaming"
                / "MetaQuotes"
                / "Terminal"
                / "Common"
                / "MQL5"
            )

        target = data_root / "Include"
        target.mkdir(parents=True, exist_ok=True)
        for item in stdlib.rglob("*.mqh"):
            rel = item.relative_to(stdlib)
            dest = target / rel
            if dest.exists():
                continue  # never clobber a broker-supplied header
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, dest)
        return any(target.rglob("*.mqh"))
    except Exception:
        print("mt5: MQL5 data-tree include mirror skipped", file=sys.stderr)
        return False


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
    if not include:
        stdlib = metaeditor.parent / "MQL5"
        if not stdlib.is_dir():
            stdlib = metaeditor.parent / "MQL5" / "Include"
        if stdlib.is_dir():
            include = f"/include:{stdlib}"

    # Self-heal the MQL5 data tree before compiling.
    #
    # MetaEditor resolves angle-bracket includes such as ``<Trade/Trade.mqh>``
    # against the MQL5 data directory that owns the SOURCE file, not against
    # ``--include``. The installer ships that tree's ``Include`` folder empty, so
    # a source living under the conventional
    # ``.../MetaQuotes/Terminal/Common/MQL5/Experts/`` path fails with
    #   error 106: file '...\Common\MQL5\Include\Trade\Trade.mqh' not found
    # even though the library exists in the install dir and even when
    # ``--include`` points straight at it. That message reads like a bug in the
    # user's code, which is precisely how it misleads. Mirroring the stock
    # library in makes a plain ``#include <Trade/...>`` compile with no flags.
    mirrored = _ensure_data_tree_includes(metaeditor, src)

    # Passing an explicit ``/include:`` for the stock library actively BREAKS a
    # normal angle-bracket include: MetaEditor concatenates the flag with the
    # source-relative path and then reports a doubled, nonexistent path
    #   error 106: file '...\Include\Include\Trade\Trade.mqh' not found
    # Once the data tree holds the library, the implicit lookup is both correct
    # and sufficient, so drop the auto-added flag. An caller-supplied --include
    # is still honoured verbatim (that is their explicit intent).
    if mirrored and not args.include:
        include = ""

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


def _read_log_text(path: Path) -> str:
    """Decode an MT5 log file, honouring its encoding.

    MEASURED FAILURE (2026-09-21, real Novita sandbox): MT5 writes its terminal
    and MetaEditor logs as UTF-16LE. Reading them with
    ``read_text(encoding="utf-8", errors="replace")`` did not fail loudly -- it
    produced NUL-interleaved garbage::

        L\\x00i\\x00v\\x00e\\x00U\\x00p\\x00d\\x00a\\x00t\\x00e

    Every real character therefore cost two characters in the payload and six
    more when JSON-escaped -- roughly a 6x blow-up that pushed ``logs`` output
    past the sandbox's output cap and destroyed the JSON (see
    ``_LOG_PAYLOAD_BUDGET``). Decode by BOM, and fall back to the NUL-density
    heuristic for BOM-less files (MetaEditor's log has no BOM).
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", "replace")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    # No BOM: UTF-16LE text is mostly ASCII bytes separated by NULs.
    if raw:
        sample = raw[:4096]
        if sample.count(0) > len(sample) // 4:
            return raw.decode("utf-16-le", "replace")
    return raw.decode("utf-8", "replace")


def _tail(path: Path, lines: int, max_chars: int = _LOG_TAIL_MAX_CHARS) -> str:
    """Last ``lines`` lines of a log, decoded properly and capped in length."""
    text = _read_log_text(path)
    if not text:
        return ""
    content = text.replace("\x00", "").splitlines()
    tail = "\n".join(content[-max(1, int(lines)):])
    if len(tail) > max_chars:
        tail = "...\n" + tail[-max_chars:]
    return tail


def _fit_payload(payload: dict[str, Any], budget: int = _LOG_PAYLOAD_BUDGET) -> dict[str, Any]:
    """Trim a payload until its serialized JSON fits the sandbox's output cap.

    A truncated JSON is worse than a short one: the host-side parser cannot
    recover it and reports "no JSON result", which the agent misreads as a broken
    install. Trimming here always leaves parseable JSON, and ``truncated`` says
    so explicitly.
    """
    def size() -> int:
        return len(json.dumps(payload, default=str))

    if size() <= budget:
        payload.setdefault("truncated", False)
        return payload
    payload["truncated"] = True

    # Log tails: halve the longest entry until it fits.
    tails = payload.get("tail") or {}
    while tails and size() > budget:
        longest = max(tails, key=lambda key: len(tails[key]))
        if len(tails[longest]) <= 200:
            tails.pop(longest, None)
            payload["omitted"] = payload.get("omitted", 0) + 1
            continue
        tails[longest] = tails[longest][len(tails[longest]) // 2:]

    # Lists (e.g. symbols): drop from the end until it fits.
    for key in ("symbols", "deals", "positions", "orders"):
        items = payload.get(key)
        while isinstance(items, list) and len(items) > 1 and size() > budget:
            items.pop()

    if size() > budget:
        # Worst case (a pathologically long key set) -- keep the structure, drop
        # the bodies, and say where to read them instead.
        if tails:
            payload["tail"] = {}
        for key in ("symbols", "deals", "positions", "orders"):
            if payload.get(key):
                payload[key] = []
        payload["note"] = (
            str(payload.get("note") or "")
            + " Bodies omitted to keep this JSON parseable: raise --limit, or read "
            "them in the sandbox directly."
        ).strip()
    return payload


def _log_payload(directory: Path, pattern: str, lines: int) -> dict[str, Any]:
    candidates = [p for p in sorted(directory.rglob(pattern))] if directory.exists() else []
    payload: dict[str, Any] = {
        "ok": bool(candidates),
        "files": [str(p) for p in candidates[-5:]],
        "requested_lines": int(lines),
        "tail": {str(p): _tail(p, int(lines)) for p in candidates[-3:]},
    }
    return _fit_payload(payload)


def cmd_logs(args: argparse.Namespace) -> int:
    payload = _log_payload(MT5_ROOT / "logs", "*.log", int(args.lines))
    if not payload["files"]:
        # Fall back to the logs shipped inside the Wine prefix.
        payload = _log_payload(WINE_PREFIX, "logs/*.log", int(args.lines))
    if not payload["files"]:
        return emit(
            {
                "ok": False,
                "error": "No MT5 logs found yet. Run action='start' (with "
                "login/password/server) so the terminal boots and writes its log.",
                "searched": [str(MT5_ROOT / "logs"), str(WINE_PREFIX / "logs")],
            },
            text="error: no MT5 logs found yet",
            code=2,
        )
    return emit(payload, code=0)


def cmd_experts(args: argparse.Namespace) -> int:
    payload = _log_payload(MT5_ROOT / "MQL5" / "Logs", "*.log", int(args.lines))
    return emit(payload, text="\n".join(payload["tail"].values())[-4000:],
                code=0 if payload["ok"] else 2)


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
    # Authorization against a broker is not instant: in sandbox testing the
    # terminal needed ~130s to go from boot to "trading has been enabled" for a
    # MetaQuotes demo account (IP discovery -> TCP connect -> auth -> symbol
    # sync of ~12k symbols). The default wait must comfortably exceed that or
    # ``start`` reports failure for a login that is still in flight.
    p.add_argument("--wait", type=int, default=300)
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

    p = sub.add_parser("symbols", help="list the symbols this server offers")
    p.add_argument("--filter", default="", help="case-insensitive substring match")
    p.add_argument("--tradable", action="store_true",
                   help="only symbols that are enabled and have a fresh tick")
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--fresh-seconds", type=int, default=900,
                   help="a tick younger than this means the market is open")
    p.set_defaults(func=cmd_symbols)

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
#:
#: Every action that touches ``MetaTrader5`` MUST be listed here. A missing entry
#: does not fail loudly — it runs the command on the Linux python, where the
#: bridge import is impossible, and answers with the generic refusal
#: "mt5_cli.py must run inside Wine". Measured live: ``symbols`` was added
#: without this entry and returned exactly that, which reads like a broken
#: install rather than a dispatch bug.
_BRIDGE_ACTIONS = frozenset(
    {
        "login", "account", "quote", "candles", "positions", "orders",
        "history", "symbol", "symbols", "order", "close", "close_all", "run",
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
            # Same rule: the payload is in the JSON, never in the exit code.
            return 0

        args = build_parser().parse_args(args_list)
        # ALWAYS exit 0. Novita's command runner raises an exception for any
        # non-zero exit status ("Command exited with status 5") and discards the
        # stdout we carefully wrote, so a structured result — a refused compile,
        # a genuine MetaEditor compilation error, a not-installed directive — never
        # reached the model. It only saw a raw traceback with no JSON, which is
        # precisely why it concluded "the mt5_sandbox tool was not responding
        # because the MT5/Wine container was not initialized" and handed the .mq5
        # back for a local compile. Outcome is carried in the JSON (`ok`, `stage`,
        # `errors`), never in the process exit code.
        args.func(args)
        return 0
    except KeyboardInterrupt:
        fail("interrupted", code=1)
        return 0
    except Exception as exc:  # noqa: BLE001
        # Even an unexpected crash must arrive as parseable JSON.
        fail(f"{type(exc).__name__}: {exc}", code=1)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())