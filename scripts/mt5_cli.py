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
  modify            set/move SL or TP on an OPEN position (--exit-at X routes it)
  guard             detached tick-level watcher that closes at a price (arm/status/stop/events)
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
CLI_VERSION = "2026-09-23.3"

MT5_ROOT = Path(os.environ.get("MT5_ROOT") or (Path.home() / ".mt5"))
WINE_PREFIX = Path(os.environ.get("WINE_PREFIX") or (Path.home() / ".wine-mt5"))

#: The generic (MetaQuotes) installer: the build a MetaQuotes-Demo account needs.
#: Selected explicitly (``--server MetaQuotes-Demo``/``MT5_GENERIC_INSTALLER=1``);
#: it is NOT what a caller who names no broker gets.
GENERIC_INSTALLER_URL = (
    "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
)

#: What the installer INSTALLS when the caller names no broker at all.
#:
#: MUST stay byte-identical to ``MT5_BROKER_INSTALLER_URL``'s default in
#: ``scripts/install_mt5_sandbox.sh``, because that default is what actually lands
#: on disk and gets recorded in ``.installed.url``. :func:`_pending_install_target`
#: compares the two strings, so a divergence makes a FINISHED install look
#: permanently pending.
#:
#: MEASURED 2026-09-22 (Runloop devbox): a bare ``install`` piped the script's own
#: default (Exness) onto disk while this file recorded ``GENERIC_INSTALLER_URL``.
#: The two markers could never converge, so ``status`` reported
#: ``stage="installing", in_progress=true`` forever even though the script's own
#: ``install.status`` already read ``done`` -- a poll loop that can never terminate,
#: i.e. exactly the "it hangs" symptom this whole mechanism exists to remove.
DEFAULT_INSTALLER_URL = (
    "https://download.mql5.com/cdn/web/exness.technologies.ltd/mt5/exness5setup.exe"
)
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
_PROBE_TIMEOUT = 45
#: Budget for list-heavy payloads (``symbols`` inventories).
_LIST_PAYLOAD_BUDGET = 6_000

#: Widest broker UTC offset a quote clock may sit at, for deciding whether the
#: server is streaming. See ``cmd_symbols``: MT5 reports tick time in server
#: time, and the bench box is UTC, so the newest tick of a LIVE session leads the
#: box clock by that offset (measured +3 h on MetaQuotes-Demo). Anything beyond a
#: full zone range is not an offset, it is a market that stopped ticking.
_ZONE_SKEW_TOLERANCE_S = 14 * 3600

#: Where the installer records WHICH MT5 build it laid down (the key of an entry
#: in :data:`BROKER_BUILDS`). Read by :func:`installed_broker_key` so a requested
#: server can be checked against the terminal that is actually on disk.
BROKER_KEY_FILE = MT5_ROOT / ".broker_key"

#: The installer URL that actually LANDED on disk (the installer writes it the
#: moment it starts downloading a build). Read by :func:`installed_broker_key`,
#: because a recorded key that names the ROUTE an install took is not a build and
#: must not make a decidable build undecidable.
INSTALLED_URL_FILE = MT5_ROOT / ".installed.url"

#: Recorded ``.broker_key`` values that name the route, not the build.
#:
#: ``unknown`` is the installer's ``${MT5_BROKER_KEY:-unknown}`` default (nobody
#: named a broker); ``explicit`` is what this CLI records when the caller supplied
#: the installer URL itself instead of naming a server. MEASURED 2026-09-22: a
#: Deriv box carried ``.broker_key=explicit`` beside the REGISTERED Deriv installer
#: URL, so ``doctor`` called a 722-symbol Deriv terminal undecidable while the URL
#: on disk named the build outright.
_NON_ANSWER_BROKER_KEYS = frozenset({"unknown", "explicit"})

#: The installer URL a detached ``install`` is laying down right now, written when
#: the install is launched and compared with ``.installed.url`` by
#: :func:`_pending_install_target`. Without it a broker SWITCH reads as "done" on
#: the first poll, because the PREVIOUS build's terminal is still on disk.
INSTALL_TARGET_FILE = MT5_ROOT / ".install.target"


# --------------------------------------------------------------------------- #
# broker resolution: which build can resolve which server
# --------------------------------------------------------------------------- #
#: Server-name -> the MT5 build whose ``Config/servers.dat`` carries that broker.
#:
#: WHY THIS EXISTS (measured 2026-09-22, real Runloop devbox): an MT5 terminal can
#: only resolve a server name its OWN server database carries. MetaQuotes' GENERIC
#: build ships an empty list, a branded build carries only its own broker's servers
#: -- and when a name does not resolve, MT5 does **not** error. It silently skips
#: the connection: ZERO ``Network`` log lines, then ``-10005 IPC timeout`` from the
#: bridge, which points at Wine/IPC and costs hours debugging the wrong layer. So
#: the build must be chosen from the SERVER the account lives on, not from a broker
#: hardcoded in a deployment. That hardcoding is what made a MetaQuotes-Demo
#: account hang against an Exness terminal.
#:
#: ``match`` entries are server-name prefixes, lowercased. Adding a broker is one
#: entry here (or ``MT5_BROKER_BUILDS`` in the environment -- see
#: :func:`_env_broker_builds`); copy the slug from the broker's own "Download MT5"
#: page. Every ``url`` below was fetched and returned 200 -- guessed slugs 404, so
#: do not add one you have not fetched.
BROKER_BUILDS: tuple[dict[str, Any], ...] = (
    {
        "key": "metaquotes",
        "label": "MetaQuotes (generic build)",
        "match": ("metaquotes",),
        # Empty URL means "the generic build", i.e. MT5_INSTALLER_URL. MetaQuotes'
        # own demo servers are treated as the one case that resolves there: a stock
        # terminal offers them in its registration wizard even with an empty
        # servers.dat (that is how a MetaQuotes-Demo account gets created at all),
        # whereas the Exness build's servers.dat carries no MetaQuotes entry --
        # which is exactly what made it hang.
        "url": "",
        "dir_name": "MetaTrader 5",
    },
    {
        "key": "exness",
        "label": "Exness",
        "match": ("exness",),
        "url": (
            "https://download.mql5.com/cdn/web/exness.technologies.ltd/"
            "mt5/exness5setup.exe"
        ),
        "dir_name": "MetaTrader 5 EXNESS",
    },
    {
        "key": "deriv",
        "label": "Deriv",
        "match": ("deriv",),
        "url": (
            "https://download.mql5.com/cdn/web/deriv.com.limited/"
            "mt5/deriv5setup.exe"
        ),
        # MEASURED 2026-09-22 on a live Runloop devbox, and worth reading twice:
        # Deriv's installer does NOT use the usual "MetaTrader 5 <BRAND>" pattern --
        # it creates "MetaTrader 5 Terminal". That name is load-bearing on both
        # sides: the installer waits for terminal64.exe in this exact directory
        # before declaring success, and find_terminal separate it from the other
        # builds by the same string. A wrong value here silently reintroduces the
        # coexistence failure. The slug is equally unguessable: deriv.com, deriv.ltd,
        # deriv.markets, deriv.me and deriv all return 404; only deriv.com.limited
        # (taken from Deriv's own download page) returns 200.
        "dir_name": "MetaTrader 5 Terminal",
    },
)


def _env_broker_builds() -> list[dict[str, Any]]:
    """Brokers added through ``MT5_BROKER_BUILDS`` without touching this file.

    Format: ``prefix1,prefix2|installer_url|install_dir_name`` records separated by
    ``;``. Example::

        MT5_BROKER_BUILDS='icmarkets|https://download.mql5.com/cdn/web/.../x.exe|MetaTrader 5 IC Markets'

    A deployment that trades one broker sets this once instead of waiting for a code
    change. Malformed records are ignored rather than failing the command -- a broken
    hint must never take out every MT5 action.
    """
    raw = (os.environ.get("MT5_BROKER_BUILDS") or "").strip()
    builds: list[dict[str, Any]] = []
    for record in raw.split(";"):
        record = record.strip()
        if not record:
            continue
        fields = [f.strip() for f in record.split("|")]
        if len(fields) < 2 or not fields[1]:
            continue
        prefixes = tuple(p.strip().lower() for p in fields[0].split(",") if p.strip())
        if not prefixes:
            continue
        dir_name = fields[2] if len(fields) > 2 and fields[2] else "MetaTrader 5"
        builds.append(
            {
                "key": prefixes[0],
                "label": prefixes[0],
                "match": prefixes,
                "url": fields[1],
                "dir_name": dir_name,
            }
        )
    return builds


def broker_builds() -> list[dict[str, Any]]:
    """Built-in builds, then env-supplied ones (an env entry overrides its key)."""
    builtin = [dict(b) for b in BROKER_BUILDS]
    extra = _env_broker_builds()
    overridden = {b["key"] for b in extra}
    return [b for b in builtin if b["key"] not in overridden] + extra


def broker_for_server(server: str | None) -> dict[str, Any] | None:
    """The registered build that can resolve ``server``, or None when unknown.

    None is meaningful and must not be read as "no broker": it means this CLI
    cannot reason about the name, so callers degrade to letting MT5 try.
    """
    name = (server or "").strip().lower()
    if not name:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for build in broker_builds():
        for prefix in build.get("match") or ():
            if name.startswith(prefix):
                # Most specific prefix wins, so a future "exness.pro" entry can
                # coexist with "exness".
                if best is None or len(prefix) > best[0]:
                    best = (len(prefix), build)
                break
    return best[1] if best else None


def _broker_build_by_key(key: str | None) -> dict[str, Any] | None:
    wanted = (key or "").strip().lower()
    if not wanted:
        return None
    for build in broker_builds():
        if build["key"].lower() == wanted:
            return build
    return None


def _broker_build_by_url(url: str | None) -> dict[str, Any] | None:
    """The registered build whose installer URL is exactly ``url``.

    Exact (after strip) and case-sensitive on the URL itself: a near-miss must not
    be reported as a build, or ``doctor`` would name the wrong terminal. Used only
    to recover from a ``.broker_key`` that names a route instead of a build.
    """
    wanted = (url or "").strip()
    if not wanted:
        return None
    for build in broker_builds():
        if str(build.get("url") or "").strip() == wanted:
            return build
    if wanted == GENERIC_INSTALLER_URL:
        # The generic build is registered with an EMPTY url (it IS MT5's own default
        # installer), so the loop above can never match it -- yet a caller who passed
        # that URL explicitly did land the generic build. Naming it turns
        # ``_terminal_has_broker_servers`` from undecidable into a real answer
        # (False), which is the one case where the generic build is the honest name.
        return _broker_build_by_key("metaquotes")
    return None


def _installed_url() -> str:
    """The installer URL on disk, or "" when the installer never recorded one."""
    try:
        return INSTALLED_URL_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def installed_broker_key(terminal: Path | None = None) -> str | None:
    """Which build is installed: the installer's record first, then the URL, then dir.

    The record is authoritative because two builds coexist in one prefix (a branded
    installer refuses to overwrite a generic install), so the mere presence of a
    directory says nothing about which terminal ``find_terminal`` hands out -- but
    only when the record actually names a build. See
    :data:`_NON_ANSWER_BROKER_KEYS`: a placeholder is skipped in favour of the two
    signals that can still decide it, the URL that landed and the directory name.
    """
    try:
        recorded = BROKER_KEY_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        recorded = ""
    if recorded and recorded.lower() not in _NON_ANSWER_BROKER_KEYS:
        return recorded

    # A placeholder key names the route the install took, not the build it laid
    # down, so it must not short-circuit the two signals that DO name a build.
    # MEASURED 2026-09-22: a bare install left ``.broker_key=unknown`` beside an
    # Exness terminal, and a Deriv install left ``.broker_key=explicit`` beside the
    # registered Deriv URL -- ``doctor`` reported ``installed_broker="unknown"`` for
    # a build the directory named, then ``terminal_has_broker_servers: false`` for a
    # terminal streaming 722 symbols.
    by_url = _broker_build_by_url(_installed_url())
    if by_url is not None:
        return str(by_url["key"])

    if terminal is None:
        terminal = find_terminal()
    if terminal is not None:
        named = broker_key_from_dir_name(terminal.parent.name)
        if named:
            return named

    # Nothing here can name the build: hand the placeholder back rather than
    # inventing one, so the caller still sees that an install happened.
    return recorded or None


def broker_key_from_dir_name(dir_name: str) -> str | None:
    """Map an install directory name onto a registry key ("MetaTrader 5 EXNESS")."""
    folded = (dir_name or "").strip().lower()
    if not folded:
        return None
    for build in broker_builds():
        if str(build.get("dir_name") or "").strip().lower() == folded:
            return str(build["key"])
    return None


def resolve_build_for_server(
    server: str | None, url: str = "", dir_name: str = ""
) -> dict[str, Any] | None:
    """Pick the installer build for a requested server, broker-agnostically.

    An explicit ``url`` always wins -- that is the escape hatch for any broker that
    is not in the registry -- then the registry lookup on the server name.
    """
    if url:
        return {
            "key": "explicit",
            "label": "caller-supplied",
            "match": (),
            "url": url,
            "dir_name": dir_name,
        }
    found = broker_for_server(server)
    if found is None:
        return None
    build = dict(found)
    if dir_name:
        build["dir_name"] = dir_name
    return build


def preflight_server(terminal: Path | None, server: str | None) -> dict[str, Any] | None:
    """Refuse, in under a second, a login this terminal demonstrably cannot resolve.

    THE HANG THIS PREVENTS (2026-09-22, live): an Exness terminal was asked to log in
    to ``MetaQuotes-Demo``. That name is not in the build's server database, so MT5
    never attempted the connection -- the bridge sat on ``initialize()``/``login()``
    until its IPC timeout and the account UI just looked frozen. A full minute of
    "nothing is happening" instead of "this terminal cannot resolve this server".

    The decision procedure is sound because a build's server database is its own:

    * unknown server -> ``None``: never block a login this CLI cannot reason about;
    * server -> broker X, X installed -> ``None`` (proceed);
    * server -> broker X, a DIFFERENT known build installed -> refuse;
    * server unknown, a branded build installed -> refuse (a branded build carries
      only its own broker's servers, so nothing else can resolve there).

    Returns a payload fragment to merge into the command's JSON, or None to proceed.
    """
    if not server:
        return None
    installed_key = installed_broker_key(terminal)
    installed_build = _broker_build_by_key(installed_key or "")
    server_build = broker_for_server(server)

    def _refusal(reason: str) -> dict[str, Any]:
        remedy: dict[str, Any] = {"action": "install", "server": server}
        if server_build and server_build.get("url"):
            remedy["broker_installer_url"] = server_build["url"]
            remedy["broker_dir_name"] = server_build["dir_name"]
        else:
            remedy["broker_installer_url"] = (
                "https://download.mql5.com/cdn/web/<broker-slug>/mt5/<name>setup.exe"
            )
            remedy["note"] = (
                "This broker is not in the built-in registry, so pass the installer "
                "URL from its own 'Download MT5' page, or register it once with "
                "MT5_BROKER_BUILDS='<server-prefix>|<installer-url>|<install-dir>'."
            )
        return {
            "ok": False,
            "failure": "server_not_in_terminal",
            "error": reason,
            "requested_server": server,
            "installed_broker": installed_key,
            "installed_terminal": str(terminal) if terminal else None,
            "server_broker": (server_build or {}).get("key"),
            "remedy": remedy,
        }

    if server_build is None:
        if installed_build is not None and installed_key != "metaquotes":
            return _refusal(
                f"server {server!r} is not in this CLI's broker registry, and the "
                f"installed terminal is the {installed_build['label']} build, which "
                f"carries only {installed_build['label']}'s own servers. MT5 would "
                "skip this login silently and the bridge would block until its IPC "
                "timeout."
            )
        return None

    if server_build["key"] == "metaquotes":
        # MetaQuotes' own demo servers resolve built-in on the GENERIC build and not
        # on a branded one -- measured: the Exness build carries no MetaQuotes-Demo.
        if installed_build is not None and installed_key != "metaquotes":
            return _refusal(
                f"server {server!r} belongs to MetaQuotes itself, but the installed "
                f"terminal is the {installed_build['label']} build. MT5 would skip "
                "this login silently and the bridge would block until its IPC timeout."
            )
        return None

    if installed_key is None:
        # Nothing recorded and no terminal resolved: do not guess about a box that
        # may not be installed at all (install/doctor own that case).
        if terminal is None:
            return None
    elif installed_key != server_build["key"]:
        installed_label = (
            installed_build["label"] if installed_build else f"{installed_key} build"
        )
        return _refusal(
            f"server {server!r} needs the {server_build['label']} build, but the "
            f"installed terminal is the {installed_label}. MT5 would skip this login "
            "silently and the bridge would block until its IPC timeout."
        )
    return None


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
    """Pick a Wine launcher that actually EXECUTES on this kernel.

    MEASURED FAILURE (2026-09-22, Runloop devbox, Debian 12, WineHQ 10.0):
    ``/usr/bin/wine`` is a 32-bit ELF. On a kernel with no IA32 emulation
    (``/proc/sys/abi/ldt16`` absent, ``ia32`` missing from /proc/cpuinfo) even
    the 32-bit ``ld-linux.so.2`` fails with ``Exec format error``, so every
    ``wine`` call died instantly — including ``wineboot``, which aborted the
    installer at exit code 2 and left a prefix with no ``drive_c``.

    ``which`` is not a sufficient check: it only proves the file exists and is
    +x, which was true for the unusable 32-bit launcher. Probe it instead.

    This costs nothing on a normal host: a working ``wine`` answers
    ``--version`` on the first try and is returned unchanged. 64-bit-only is
    sufficient here because ``mt5setup.exe``, the embeddable Windows python and
    the ``MetaTrader5`` win_amd64 wheels are all 64-bit PE/amd64 — verified by
    reading the installer's PE header (machine 0x8664).
    """
    fallback = ""
    for candidate in ("wine", "wine64"):
        if subprocess.run(["which", candidate], capture_output=True).returncode != 0:
            continue
        if not fallback:
            fallback = candidate
        try:
            probe = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                env=wine_env(),
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and (probe.stdout or probe.stderr).strip():
            return candidate
    return fallback or "wine"


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


def _cache_terminal(exe: Path) -> None:
    """Remember the resolved terminal so repeated calls skip the prefix walk."""
    try:
        TERMINAL_MARKER.write_text(str(exe), encoding="utf-8")
    except OSError:
        pass


def _terminal_matches_key(exe: Path, key: str | None) -> bool:
    """Whether ``exe`` belongs to the requested broker build (no key = any)."""
    if not key:
        return True
    return broker_key_from_dir_name(exe.parent.name) == key


def find_terminal(prefer_key: str | None = None) -> Path | None:
    """Locate the MT5 terminal, PREFERRING the broker-branded build.

    MEASURED FAILURE (2026-09-22, Runloop devbox): when a broker-branded terminal
    ("MetaTrader 5 EXNESS") and the generic MetaQuotes one ("MetaTrader 5") both
    exist in the prefix, an unqualified ``rglob`` returns whichever the
    filesystem lists first. Picking the generic build silently breaks logins: its
    ``servers.dat`` has no broker server list, so the broker's server name cannot
    be resolved and the terminal never authorizes (no ``Network`` log line, then
    ``-10005 IPC timeout`` from this bridge).

    The branded build is therefore resolved explicitly, by the same
    ``MT5_BROKER_DIR_NAME`` the installer honours, before any generic fallback.

    ``prefer_key`` is how a caller says "the server I am about to log in to lives on
    THAT broker's terminal". It matters because branded builds COEXIST in one prefix
    (the installer refuses to overwrite another broker's install), so without it the
    marker's cache hands back the PREVIOUS broker's terminal -- which cannot resolve
    the new server and then fails silently (see :func:`preflight_server`).
    """
    drive_c = WINE_PREFIX / "drive_c"

    # 0) The requested broker's build wins outright: of everything on disk it is the
    #    only terminal that can resolve that broker's server names.
    if prefer_key:
        build = _broker_build_by_key(prefer_key)
        dir_name = str((build or {}).get("dir_name") or "")
        if dir_name:
            requested = drive_c / "Program Files" / dir_name / "terminal64.exe"
            if requested.exists():
                _cache_terminal(requested)
                return requested

    if TERMINAL_MARKER.exists():
        cached = Path(TERMINAL_MARKER.read_text(encoding="utf-8").strip())
        # A cached terminal from a DIFFERENT broker is worse than no cache: it is
        # the path that produced "wrong terminal, silent no-login", so it must not
        # short-circuit a request for another broker.
        if cached.exists() and _terminal_matches_key(cached, prefer_key):
            return cached

    if not drive_c.exists():
        return None

    # 1) Broker-branded install, named exactly as the installer laid it down.
    brand = os.environ.get("MT5_BROKER_DIR_NAME", "MetaTrader 5 EXNESS")
    preferred = drive_c / "Program Files" / brand / "terminal64.exe"
    if preferred.exists():
        _cache_terminal(preferred)
        return preferred

    # 2) Any other "MetaTrader 5 <BRAND>" install, before the bare generic one.
    #    Sorted so the choice is deterministic rather than filesystem order.
    try:
        candidates = sorted(
            p for p in (drive_c / "Program Files").glob("MetaTrader 5 *")
            if (p / "terminal64.exe").exists()
        )
    except OSError:
        candidates = []
    if candidates:
        chosen = candidates[0] / "terminal64.exe"
        _cache_terminal(chosen)
        return chosen

    for exe in drive_c.rglob("terminal64.exe"):
        _cache_terminal(exe)
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


def _terminal_has_broker_servers(terminal: Path | None) -> bool | None:
    """Whether the installed terminal ships a broker server list.

    THE FAILURE THIS DETECTS (measured 2026-09-22): MetaQuotes' GENERIC terminal
    carries no broker servers, so a broker server name (e.g. ``Exness-MT5Trial9``)
    has nothing to resolve to. MT5 then skips the connection entirely -- the log
    gets ZERO ``Network`` lines and the bridge reports ``-10005 IPC timeout``,
    which points at Wine/IPC and sends you debugging the wrong layer for hours.

    A broker-branded installer embeds its servers: the Exness build's
    ``servers.dat`` is ~234 KB against the generic ~50 KB. Size is a crude but
    reliable signal, and it is the only thing readable without MT5's own parser
    (the file is not plain text; a string scan finds only the copyright line).

    Returns None when there is no terminal to inspect.
    """
    if terminal is None:
        return None
    servers = terminal.parent / "Config" / "servers.dat"
    try:
        size = servers.stat().st_size
    except OSError:
        return None
    if size > 100_000:
        return True
    # SIZE ALONE CANNOT REFUTE A BRANDED BUILD. The 100 KB line was drawn from
    # Exness (~234 KB) vs generic (~50 KB), but a branded terminal may ship a
    # SMALLER database than the generic one -- MEASURED 2026-09-22: Deriv's
    # servers.dat is 43,804 B, and `doctor` reported
    # `terminal_has_broker_servers: false` while that same terminal was streaming
    # 722 Deriv symbols. A byte count is not evidence of absence, and asserting
    # "this build carries no broker servers" about a build that demonstrably has
    # them is the exact wrong signal this whole area exists to remove.
    #
    # So ask the REGISTRY what build this is instead. A registered build with a
    # broker URL is branded and carries its broker's servers; the registered
    # generic build (empty URL) is the one case known to carry none. Anything
    # unregistered is reported as undecidable (None) rather than as a false.
    build = _broker_build_by_key(installed_broker_key(terminal) or "")
    if build is not None:
        return bool(build.get("url"))
    return None


def _terminal_network_lines(
    terminal: Path | None, login: str | int | None = None
) -> list[str]:
    """``Network`` lines from the terminal's own log, newest last.

    THE DISTINCTION THIS MAKES: a WRONG CREDENTIAL produces a ``Network`` line
    ("authorization failed", "invalid account"), while a server name the installed
    build cannot resolve produces NOTHING -- no line, no error, just a bridge that
    blocks until its IPC timeout. Counting these lines is the only way to tell the
    two apart without MT5's own (binary) server database, and they need opposite
    fixes: fix the password, versus install the build that carries this broker.
    """
    wanted = str(login) if login not in (None, "") else ""
    directories: list[Path] = []
    if terminal is not None:
        directories.append(terminal.parent / "logs")
    directories.append(MT5_ROOT / "logs")

    found: list[str] = []
    for directory in directories:
        try:
            files = sorted(directory.glob("*.log"))
        except OSError:
            continue
        for path in files:
            try:
                text = _read_log_text(path)
            except OSError:
                continue
            for line in text.splitlines():
                if "network" not in line.lower():
                    continue
                if wanted and wanted not in line:
                    continue
                found.append(line.strip())
    return found[-40:]


def cmd_doctor(_: argparse.Namespace) -> int:
    # Resolve the terminal for the broker that is RECORDED, not just "a terminal":
    # two builds coexist in one prefix, and the order the filesystem lists them in is
    # not the installer's record. Live 2026-09-22: after switching this box to the
    # Exness build, doctor reported installed_broker="exness" next to the GENERIC
    # terminal's path, then read that terminal's empty servers.dat and warned that a
    # broker login was impossible -- about a build the box was not using.
    terminal = find_terminal(prefer_key=installed_broker_key())

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
    has_broker_servers = _terminal_has_broker_servers(terminal)
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
        "terminal_has_broker_servers": has_broker_servers,
        # WHICH broker this terminal is for. The server name a login will use must
        # belong to this build, or MT5 skips the login with no error at all -- see
        # preflight_server, which now refuses that combination up front.
        "installed_broker": installed_broker_key(terminal),
        "supported_server_prefixes": sorted(
            {
                prefix
                for build in broker_builds()
                for prefix in (build.get("match") or ())
            }
        ),
        "windows_python": str(winpy) if winpy else None,
        "python_bridge": winpy is not None,
        "bridge_imports_in_wine": _bridge_imports_under_wine() if winpy else False,
        "running_under_wine": under_wine(),
        "mt5_root": str(MT5_ROOT),
    }
    installed = bool(
        info["wine_installed"] and info["prefix_ready"] and terminal and winpy is not None
    )
    # A GENERIC terminal is installed but cannot trade a broker: MT5 skips the
    # login entirely and the bridge reports -10005. Reporting
    # ``ready_for_trading: true`` there is what made this trap silent, so gate it
    # on broker servers when they are positively known to be missing. ``None``
    # means "not inspected" and must not block a healthy install.
    ready = installed and has_broker_servers is not False
    info["ready_for_trading"] = ready
    if installed and has_broker_servers is False:
        # Say this LOUDLY. The chain is complete and every other probe is green,
        # yet a BROKER login will silently never happen -- the generic build carries
        # no broker server list. (MetaQuotes' own demo servers are the exception:
        # MEASURED 2026-09-22, a generic terminal authorized on MetaQuotes-Demo and
        # read back balance 99 996.48 USD, so this warning is about real brokers.)
        info["warning"] = (
            "This terminal is the GENERIC MetaQuotes build: its Config/servers.dat "
            "carries no broker server list, so a BROKER server name cannot be "
            "resolved and a broker login will silently never even be attempted "
            "('-10005 IPC timeout' from the bridge, and no Network lines in the "
            "terminal log -- though a successful login can write none either, so "
            "compare installed_broker with the server's broker rather than trusting "
            "the line count). MetaQuotes' own demo servers are the exception and do "
            "resolve here. For a real broker, install the build that carries it: "
            "'install --server <broker server name>' -- e.g. 'install --server "
            "Exness-MT5Trial9' fetches "
            "https://download.mql5.com/cdn/web/exness.technologies.ltd/mt5/"
            "exness5setup.exe. For a broker that is not registered, pass its own "
            "installer with --broker-installer-url / --broker-dir-name, or export "
            "MT5_BROKER_BUILDS."
        )
        text = "MT5 chain ready, but the generic terminal cannot log in to a broker"
    elif ready:
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

    # WHICH BUILD TO INSTALL: derived from the server the account lives on, so this
    # is broker-agnostic. An explicit URL still wins (any broker, registered or not),
    # then the registry lookup, then the installer's own default.
    #
    # WHY IT MATTERS: a terminal can only resolve server names its own build ships.
    # Installing the deployment's default build for a server that belongs to another
    # broker used to produce a terminal that never even attempted the login --
    # silently, then "IPC timeout" from the bridge. Passing ``--server`` at install
    # time makes the FIRST install the right one instead of paying for a re-install.
    env = wine_env()
    server = getattr(args, "server", None)
    resolved = resolve_build_for_server(
        server,
        url=str(
            getattr(args, "broker_installer_url", "")
            or os.environ.get("MT5_BROKER_INSTALLER_URL")
            or ""
        ),
        dir_name=str(getattr(args, "broker_dir_name", "") or ""),
    )
    if resolved is not None:
        if resolved.get("url"):
            env["MT5_BROKER_INSTALLER_URL"] = str(resolved["url"])
            env["MT5_BROKER_DIR_NAME"] = str(resolved.get("dir_name") or "MetaTrader 5")
            env.pop("MT5_GENERIC_INSTALLER", None)
        else:
            # The generic build. MetaQuotes' own demo servers resolve on it; a real
            # broker's do not, and preflight_server will say so before a login runs.
            env["MT5_GENERIC_INSTALLER"] = "1"
            env.pop("MT5_BROKER_INSTALLER_URL", None)
        env["MT5_BROKER_KEY"] = str(resolved["key"])
        if server:
            env["MT5_BROKER_SERVER"] = str(server)

    # Switching broker must invalidate the cached terminal path and the recorded
    # build, or the next command hands out the PREVIOUS broker's terminal -- the
    # silent no-login trap. (The marker is a pure cache: it is always safe to drop.)
    for stale in (TERMINAL_MARKER, METAEDITOR_MARKER, BROKER_KEY_FILE):
        if stale.exists():
            stale.unlink()

    if args.detach:
        # WHY DETACHED: the execution sandbox clamps every single command to a
        # fixed ceiling (900 s on Novita) while a full Wine + MT5 + bridge
        # install legitimately runs longer. Holding one command open would be
        # killed mid-install and leave a half-built prefix. So the installer is
        # launched with nohup/setsid and the caller polls ``status`` instead.
        #
        # Record WHICH build this run is laying down. On a broker switch the
        # previous build's terminal is still on disk, so ``status`` would see
        # installed=True and answer "done" for an install that has not started --
        # MEASURED 2026-09-22: a re-install reported stage=done on the first poll
        # with an empty log and no .broker_key, which is exactly the signal that
        # makes an agent stop waiting for a terminal that is not there yet.
        # Precedence mirrors the installer's own: a broker URL first, then an
        # explicit generic URL, then the default the script would use.
        # The GENERIC flag has to be checked FIRST. When it is set the installer
        # downloads its own default (GENERIC_INSTALLER_URL) and never looks at
        # MT5_BROKER_INSTALLER_URL, so recording the broker URL here puts a URL on
        # disk that this install cannot possibly produce. `status` then compares it
        # against .installed.url forever, and answers "a different build is being
        # installed" -- a poll loop that can never terminate.
        #
        # MEASURED 2026-09-23 (Runloop devbox): `install --server MetaQuotes-Demo`
        # recorded the Exness URL, installed the generic terminal, and stayed at
        # stage="installing" for the life of the box while the installer's own
        # status file already read "done|install complete". Every generic-build
        # install -- which is what naming a MetaQuotes server selects -- reported a
        # hang that had not happened.
        if env.get("MT5_GENERIC_INSTALLER") == "1":
            install_target = str(env.get("MT5_INSTALLER_URL") or GENERIC_INSTALLER_URL)
        else:
            install_target = str(
                env.get("MT5_BROKER_INSTALLER_URL")
                or env.get("MT5_INSTALLER_URL")
                or DEFAULT_INSTALLER_URL
            )
        try:
            (MT5_ROOT / INSTALL_TARGET_FILE.name).write_text(
                install_target, encoding="utf-8"
            )
            status_path.write_text(
                "starting|a new build is being installed", encoding="utf-8"
            )
        except OSError:
            pass
        inner = f"bash {shlex.quote(str(script))} > {shlex.quote(str(log_path))} 2>&1"
        quoted = shlex.quote(inner)
        proc = subprocess.run(
            ["sh", "-c", f"nohup setsid sh -c {quoted} >/dev/null 2>&1 & echo $!"],
            env=env,
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
                "broker": (resolved or {}).get("key"),
                "server": server or None,
                "hint": "Poll action='status' (or mt5_cli.py status) until stage is "
                "'done' or 'failed'. A full install takes ~10-25 minutes.",
            },
            text=f"install started detached (pid {pid}); poll status until done",
        )

    # Foreground mode: only usable when the caller's command ceiling exceeds the
    # install duration (e.g. a local run or a self-hosted box).
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


def _pending_install_target() -> str:
    """The URL a running install is laying down, when it is NOT what landed yet.

    Empty string means "nothing pending": either no install has been started, or the
    build on disk IS the one that was asked for. A broker switch is the case this
    exists for -- installing Exness while the MetaQuotes terminal is still present
    used to report ``stage="done"`` (``installed`` is true either way) and hand back
    a green result for a terminal that had not been replaced.
    """
    try:
        target = INSTALL_TARGET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not target:
        return ""
    return target if target != _installed_url() else ""


def cmd_status(args: argparse.Namespace) -> int:
    """Report install progress and overall stack readiness (pollable)."""
    status_path = MT5_ROOT / "install.status"
    log_path = MT5_ROOT / "install.log"
    stage, message = "unknown", ""
    if status_path.exists():
        raw = status_path.read_text(encoding="utf-8", errors="replace").strip()
        stage, _, message = raw.partition("|")

    # Same rule as doctor: report the terminal of the broker that is RECORDED, not
    # whichever the filesystem lists (or the marker cached) first. Live 2026-09-22:
    # after switching the box back to the generic build, status still named the
    # Exness terminal it was no longer using.
    terminal = find_terminal(prefer_key=installed_broker_key())
    winpy = win_python()
    running = terminal_running()
    installed = bool(terminal and winpy is not None)
    failed = stage == "failed"
    done = installed or stage == "done"

    # A DIFFERENT build is being installed than the one on disk: the terminal that
    # "installed" is describing is the previous broker's, so this install is not
    # done whatever the stale marker says.
    pending_target = _pending_install_target()
    if pending_target and not failed:
        done = False
        if stage in ("done", "unknown"):
            stage = "installing"
            message = (
                "a different build is being installed; the terminal on disk is still "
                "the previous one"
            )

    in_progress = (
        not done
        and not failed
        and (bool(pending_target) or _installer_alive())
    )

    if not status_path.exists() and not done:
        stage, message = "not_started", "no install has been run in this sandbox"

    payload = {
        "ok": not failed,
        "stage": "done" if done else stage,
        "message": message,
        "in_progress": in_progress,
        "installed": installed,
        "installing_target": pending_target or None,
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
    # Resolve the terminal for the SERVER being logged in to, not just "a terminal".
    # Two branded builds coexist in one prefix, and only the one matching the server
    # can resolve it (see find_terminal / preflight_server).
    server = getattr(args, "server", None)
    terminal = find_terminal(prefer_key=(broker_for_server(server) or {}).get("key"))
    if terminal is None:
        return fail("terminal64.exe not found. Run install first.", code=2)

    # Fast refusal, in under a second, for a login this build cannot resolve. Without
    # it the terminal boots, silently never attempts the connection, and the whole
    # call blocks on the bridge's IPC timeout -- the "it hangs" symptom.
    refusal = preflight_server(terminal, server)
    if refusal is not None:
        return emit(
            refusal,
            text=f"error: {refusal['error']}",
            code=2,
        )

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
    # Another BROKER's terminal must not stay up while we boot this one: both would
    # publish an IPC socket and the bridge's attach would pick whichever answers
    # first -- reporting the wrong broker's account, or none. (Prefight above already
    # guarantees the requested build exists and matches the server.)
    if credentials:
        interlopers = _stop_terminal_processes(keep_terminal=terminal)
        if interlopers:
            restarted_for_credentials = True
            print(
                f"stopped terminal process(es) {interlopers} from another broker",
                file=sys.stderr,
            )

    if credentials and _terminal_has_credentials(terminal):
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

    # "Is a terminal running" is not the question -- "is THIS terminal running" is.
    # A different broker's terminal is not a substitute and must not suppress the
    # launch (it was stopped just above when credentials were supplied).
    _this_terminal_running = any(
        _process_is_for_terminal(argv, terminal) for _, argv in _terminal_processes()
    )
    if not _this_terminal_running:
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
        #
        # WHICH hint is honest depends on the build that is actually installed, not
        # on the log alone. An unresolvable server IS one cause of "no account
        # appeared" -- MT5 skips the connection without an error -- but the Network
        # count cannot prove it: MEASURED 2026-09-22 (generic MetaQuotes build,
        # Runloop devbox, MT5 build 6207) a login that SUCCEEDED -- `account`
        # returned balance 99 996.48 USD -- wrote ZERO Network lines. So zero lines
        # is a signal, not a diagnosis, and the build mismatch is checked separately
        # (preflight_server refuses it up front; this is the belt-and-braces case).
        network_lines = _terminal_network_lines(terminal, login=args.login)
        diagnosis = login_failure_diagnosis(
            terminal, args.server, args.login, args.wait, network_lines
        )
        payload["terminal_network_lines"] = len(network_lines)
        payload["installed_broker"] = diagnosis["installed_broker"]
        if diagnosis["failure"]:
            payload["failure"] = diagnosis["failure"]
        payload["hint"] = diagnosis["hint"]
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


def login_failure_diagnosis(
    terminal: Path | None,
    server: str | None,
    login: str | int | None,
    wait: int | str | None,
    network_lines: list[str],
) -> dict[str, Any]:
    """``{"failure", "hint", "installed_broker"}`` for a credentialed login that
    produced no account within ``wait`` seconds.

    WHY THIS IS ITS OWN FUNCTION: it is the one place that decides *why* a login
    failed, and the wrong answer sends the fix to the wrong layer -- "bad password"
    and "this terminal cannot resolve that server" need opposite repairs.

    ZERO ``Network`` lines is the tell for an unresolvable server name -- MT5 skips
    the connection with no error at all -- but it is a SIGNAL, NOT PROOF:
    MEASURED 2026-09-22 (generic MetaQuotes build, Runloop devbox, MT5 build 6207)
    a login that SUCCEEDED -- ``account`` returned balance 99 996.48 USD on
    MetaQuotes-Demo -- wrote ZERO Network lines. So the build that is installed is
    checked directly instead of being inferred from the log, and only a real
    build/server mismatch is reported as one.
    """
    installed_broker = installed_broker_key(terminal)
    requested_build = broker_for_server(server)
    wrong_build = bool(
        requested_build
        and installed_broker
        and requested_build["key"] != installed_broker
    )
    credentials_hint = (
        "Credentials were written and the terminal was launched with "
        f"/config: but no account appeared within {int(wait or 0)}s. Check the "
        "terminal log with action='logs' for 'authorization failed' / 'invalid "
        "account' lines, and confirm the server name matches the account "
        f"(got server={server!r}, login={login!r}). The account must exist on that "
        "server; MetaQuotes-Demo logins are created by the MetaQuotes demo "
        "registration, not by this platform."
    )
    if wrong_build:
        return {
            "failure": "server_not_in_terminal",
            "installed_broker": installed_broker,
            "hint": (
                f"The installed terminal is the {installed_broker} build, which "
                "carries only that broker's servers, but login was attempted on "
                f"server={server!r} ({requested_build['label']}). Install the build "
                f"for this server and retry: action='install' with server={server!r} "
                "(add broker_installer_url from the broker's own 'Download MT5' page "
                "if it is not a registered broker)."
            ),
        }
    if not network_lines:
        return {
            "failure": "no_network_activity",
            "installed_broker": installed_broker,
            "hint": (
                "No account appeared and the terminal log has ZERO Network lines for "
                f"server={server!r}. Two causes, and the log cannot separate them: "
                "(1) a server name this build cannot resolve -- MT5 skips the "
                "connection silently -- or (2) the terminal logged nothing for a "
                "connection it did attempt (measured: a generic MetaQuotes terminal "
                "logged zero Network lines on a login that worked). Which build owns "
                f"server={server!r}? doctor reports installed_broker="
                f"{installed_broker!r}. If that terminal is generic or belongs to "
                "another broker, install the matching build with action='install' "
                f"server={server!r}; if it already matches, treat this as a credential "
                "or server-name mismatch: " + credentials_hint
            ),
        }
    return {
        "failure": None,
        "installed_broker": installed_broker,
        "hint": credentials_hint,
    }


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


def _terminal_has_credentials(terminal: Path | None = None) -> bool:
    """True when a running terminal was launched with our ``/config:`` file.

    A terminal started WITHOUT it (the installer's own "materialise the MQL5
    library" launch, or an older credential-less ``start``) boots with no account
    and will never authorize, no matter how correct the credentials are. That
    distinction is the whole reason ``start`` used to look like it ignored the
    login/password/server arguments.

    ``terminal`` restricts the question to THAT terminal. With two branded builds in
    one prefix, a credential-carrying process belonging to another broker must not
    be mistaken for this one -- the credentials would apply to the wrong terminal.
    """
    for _, argv in _terminal_processes():
        if not _process_is_for_terminal(argv, terminal):
            continue
        for arg in argv:
            if arg.startswith("/config:") or arg.lower() == "/config:":
                return True
    return False


def _process_is_for_terminal(argv: list[str], terminal: Path | None) -> bool:
    """Whether a process's argv identifies ``terminal`` (None = any terminal).

    Two branded builds COEXIST in one prefix, so "a terminal is running" is not the
    same as "the terminal I asked for is running". The install-directory name is what
    distinguishes them in an argv (e.g. ``...\\MetaTrader 5 EXNESS\\terminal64.exe``).
    """
    if terminal is None:
        return True
    token = terminal.parent.name.strip().lower()
    if not token:
        return True
    return token in " ".join(argv).lower().replace("\\", "/")


def _stop_terminal_processes(*, keep_terminal: Path | None = None) -> list[int]:
    """SIGTERM then SIGKILL terminal processes; return the PIDs handled.

    ``keep_terminal`` spares that one terminal's processes. That is how ``start``
    clears another BROKER's leftover terminal: both would compete for the bridge's
    IPC socket, and the one that answers is not necessarily the one the caller asked
    for -- a silent way to report the wrong broker's account, or none at all.
    """

    def _targets() -> list[int]:
        out: list[int] = []
        for pid, argv in _terminal_processes():
            if keep_terminal is not None and _process_is_for_terminal(argv, keep_terminal):
                continue
            out.append(pid)
        return out

    handled: list[int] = []
    for pid in _targets():
        try:
            os.kill(pid, signal.SIGTERM)
            handled.append(pid)
        except OSError:
            continue
    if handled:
        time.sleep(3)
        for pid in _targets():
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
    # Same guard as ``start``: a server this terminal cannot resolve makes
    # ``mt5.initialize()``/``login()`` block until the IPC timeout, with no error
    # anywhere in the terminal log. Refuse here, in under a second, with the remedy.
    server = getattr(args, "server", None)
    terminal = find_terminal(prefer_key=(broker_for_server(server) or {}).get("key"))
    refusal = preflight_server(terminal, server)
    if refusal is not None:
        return emit(refusal, text=f"error: {refusal['error']}", code=2)

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


def _guard_summary() -> dict[str, Any]:
    """One-line answer to "is anything watching a price right now?".

    Attached to ``positions`` so a caller looking at open risk is told, in the
    same payload, whether the levels are held by the broker, watched by the
    guard, or by nobody at all -- the third case being the one that used to be
    indistinguishable from the first two.
    """
    state = _read_guard_state()
    rules = _read_guard_rules()
    live = _guard_is_live(state)
    alert = None
    if rules and not live:
        alert = "guard_not_running"
    elif live and (state or {}).get("unpriceable"):
        alert = "rule_unpriceable"
    return {
        "live": live,
        "rules_armed": len(rules),
        "alert": alert,
        "exit_reason": None if live else (state or {}).get("exit_reason"),
        "heartbeat_age_s": (
            round(time.time() - float((state or {}).get("heartbeat") or 0.0), 1)
            if (state or {}).get("heartbeat")
            else None
        ),
        "recovery": "guard ensure" if alert == "guard_not_running" else None,
    }


def cmd_positions(_: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    positions = mt5.positions_get()
    if positions is None:
        return fail(f"positions_get failed: {mt5.last_error()}", code=2)
    return emit({
        "ok": True,
        "count": len(positions),
        "positions": [p._asdict() for p in positions],
        "guard": _guard_summary(),
    })


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


# --------------------------------------------------------------------------- #
# instant exits at a price
# --------------------------------------------------------------------------- #
# THE BUG THIS FIXES (reported 2026-09-23: "when something hits a certain price
# it doesn't close trade instantly").
#
# An exit at a price had exactly two possible routes before this code existed,
# and BOTH of them put the model in the loop:
#
#   1. ``order`` with ``--tp``/``--sl``. Broker-side and instant -- but only
#      available at order time. There was no way to attach or move a stop after
#      the position existed, so "buy now, exit at 1.1650" could not be expressed
#      as a broker-side instruction at all.
#   2. poll ``quote`` in a loop, compare, then ``close``. Every poll costs a
#      sandbox round trip (the CLI re-execs the Windows python under Wine for
#      bridge actions, ~1-2 s) and then an LLM turn on top. The level is only
#      observed on the turns the model chooses to poll, and the market does not
#      stop ticking while it thinks. A level touched between two polls is simply
#      missed -- the position stays open and the user is told it "did not close".
#
# So there are now two primitives, and they answer different questions:
#
#   * ``modify``  attaches/moves SL and TP on an EXISTING position (and
#     ``--exit-at`` routes a bare "exit at X" to the correct side for the
#     position's direction). The terminal's server holds the level, so the exit
#     is executed by MetaQuotes itself -- the lowest possible latency, and it
#     survives this sandbox being paused or killed.
#   * ``guard``   runs a detached tick-level watcher INSIDE the sandbox for the
#     conditions a broker cannot hold: close every position on a symbol when the
#     symbol touches a level, exit a basket, exit at a level on the wrong side of
#     the spread, partial closes. It polls the tick stream every 100 ms and sends
#     the deal itself, with no model turn anywhere in the path.
#
# Both record MEASURED latency so a claim of "instant" is evidence, not a hope.
GUARD_DIR = MT5_ROOT / "guard"
GUARD_RULES_FILE = GUARD_DIR / "rules.json"
GUARD_EVENTS_FILE = GUARD_DIR / "events.jsonl"
GUARD_STATE_FILE = GUARD_DIR / "state.json"
GUARD_STOP_FILE = GUARD_DIR / "stop"
GUARD_WATCH_SCRIPT = GUARD_DIR / "guard_watch.py"
GUARD_LOG_FILE = GUARD_DIR / "watcher.log"
#: Wine writes its own debug chatter (``fixme:``/``err:`` lines) to stderr for
#: every process it starts, the watcher included. That chatter is kept out of
#: the log a reader is pointed at, and parked here instead -- otherwise the
#: guard log is nothing but Wine noise and a real traceback is unfindable.
GUARD_ERR_FILE = GUARD_DIR / "watcher.err"

#: Seconds after which a silent heartbeat means the watcher is dead. The watcher
#: beats every ``--interval-ms`` loop, so this is ~50 missed loops: long enough
#: that a slow tick or a busy box is not mistaken for a crash, short enough that
#: a user is never told a dead guard is protecting them.
GUARD_HEARTBEAT_STALE = 5.0

#: The watcher itself. It is written into the sandbox by ``guard start`` and run
#: by the WINDOWS python under Wine -- the only interpreter that can import
#: MetaTrader5 -- as a detached process with its output redirected to a file
#: (a Wine python cannot inherit Linux pipes; see ``_reexec_under_wine``).
#: It is armed by ``guard arm`` and shipped to the box with the CLI.
#:
#: It is embedded here rather than shipped as a second file because the sandbox
#: bootstraps exactly two files from the repo; a third one that the bootstrap
#: does not fetch would be missing in every sandbox that has not been rebuilt.
_GUARD_WATCH_SOURCE = r'''#!/usr/bin/env python3
"""Tick-level price guard: closes positions the instant a level is touched.

WHY THIS IS A DEDICATED PROCESS
    The agent only observes the market when it calls a tool. Measured in the
    sandbox (2026-09-23): one ``quote`` costs a Wine re-exec plus IPC, and an
    agent turn sits on top of that, so a price-triggered exit handled by the
    model lands seconds late -- and only on the turns the model happens to poll.
    A level touched between two polls is missed entirely.

    This process removes the model from the path: it polls the tick stream every
    ``--interval-ms`` (default 100 ms), evaluates the armed rules, and sends the
    closing deal itself. It writes an append-only event log with the trigger
    timestamp, the fill timestamp and the measured latency, so "instant" is a
    number rather than a claim.

STATE AND CONTROL (all files, so no process is ever killed by pattern)
    --rules     JSON list of armed rules; rules are removed as they fire
    --events    JSONL append-only log (watcher_start, fired, watcher_stop, ...)
    --state     heartbeat + last seen prices, rewritten every loop
    --stop-file presence asks the watcher to exit cleanly after this loop
"""
import argparse
import json
import os
import time

import MetaTrader5 as mt5

FILLING_IOC_FLAG = 2
FILLING_FOK_FLAG = 1
FILLING_RETURN_FLAG = 4
RETCODE_DONE = 10009
RETCODE_UNSUPPORTED_FILLING = 10030
#: A rejected close (no money, market closed, requote) keeps the rule ARMED --
#: silently disarming protection is worse than a retry -- but it is retried on a
#: cooldown so a persistent rejection cannot spam the broker 10x a second.
RETRY_COOLDOWN_SECONDS = 1.5
#: Seconds a rule's symbol may go unpriced before the watcher says so out loud.
#: A symbol with no tick is a rule that can never be evaluated, and a watcher
#: that is silent about it is indistinguishable from protection.
UNPRICEABLE_STALE = 10.0


def append_jsonl(path, payload):
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass


def write_json(path, payload):
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=str)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def read_rules(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def filling_candidates(info):
    mask = int(getattr(info, "filling_mode", 0) or 0)
    out = []
    if mask & FILLING_IOC_FLAG:
        out.append(mt5.ORDER_FILLING_IOC)
    if mask & FILLING_FOK_FLAG:
        out.append(mt5.ORDER_FILLING_FOK)
    if mask & FILLING_RETURN_FLAG:
        out.append(mt5.ORDER_FILLING_RETURN)
    if not out:
        out = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
    return out


def send_with_fillings(request, fillings):
    last = None
    for filling in fillings:
        attempt = dict(request)
        attempt["type_filling"] = filling
        result = mt5.order_send(attempt)
        if result is None:
            last = {"ok": False, "error": str(mt5.last_error()), "filling_used": filling}
            continue
        payload = {
            "ok": result.retcode == RETCODE_DONE,
            "retcode": result.retcode,
            "comment": result.comment,
            "deal": getattr(result, "deal", None),
            "filling_used": filling,
        }
        if payload["ok"]:
            return payload
        last = payload
        if result.retcode != RETCODE_UNSUPPORTED_FILLING:
            return payload
    return last or {"ok": False, "error": "no filling mode was attempted"}


def matching_positions(rule):
    scope = rule.get("scope") or {}
    if scope.get("ticket"):
        return list(mt5.positions_get(ticket=int(scope["ticket"])) or [])
    positions = list(mt5.positions_get() or [])
    if scope.get("all"):
        return positions
    if scope.get("symbol"):
        return [p for p in positions if p.symbol == scope["symbol"]]
    return [p for p in positions if p.symbol == rule.get("symbol")]


def close_positions(rule, positions, deviation, magic):
    results = []
    for position in positions:
        info = mt5.symbol_info(position.symbol)
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            results.append({"ticket": position.ticket, "ok": False, "error": "no tick"})
            continue
        is_long = position.type == mt5.POSITION_TYPE_BUY
        fillings = filling_candidates(info) if info is not None else None
        full = fillings or [mt5.ORDER_FILLING_RETURN]
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": float(rule.get("volume") or position.volume),
            "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": float(tick.bid if is_long else tick.ask),
            "deviation": int(deviation),
            "magic": int(magic),
            "comment": "powerx-guard",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": full[0],
        }
        payload = send_with_fillings(request, full)
        payload["ticket"] = position.ticket
        results.append(payload)
    return results


def price_for_rule(rule, tick):
    side = str(rule.get("side") or "mid").lower()
    if side == "bid":
        return float(tick.bid), "bid"
    if side == "ask":
        return float(tick.ask), "ask"
    return (float(tick.bid) + float(tick.ask)) / 2.0, "mid"


def triggered(price, op, level):
    if op == ">=":
        return price >= level
    if op == "<=":
        return price <= level
    if op == ">":
        return price > level
    if op == "<":
        return price < level
    return False


def note(message):
    """One human-readable line on stdout, which is redirected to the log.

    The event log is the machine channel; this is what a person tails while
    a rule waits for its level, and it is the only thing written to the log
    once Wine's own chatter is kept out of it.
    """
    try:
        print(f"[guard {time.strftime('%H:%M:%S')}] {message}", flush=True)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--interval-ms", type=int, default=100)
    parser.add_argument("--max-seconds", type=int, default=0)
    parser.add_argument("--deviation", type=int, default=30)
    parser.add_argument("--magic", type=int, default=20240919)
    args = parser.parse_args()

    started = time.time()
    write_json(args.state, {"pid": os.getpid(), "started_at": started, "status": "starting"})
    note(
        f"watching {len(read_rules(args.rules))} rule(s) every {args.interval_ms} ms; "
        f"max {int(args.max_seconds) if int(args.max_seconds) > 0 else 'none'} s"
    )
    append_jsonl(args.events, {
        "event": "watcher_start", "ts": started, "pid": os.getpid(),
        "interval_ms": args.interval_ms, "rules": len(read_rules(args.rules)),
    })

    if not mt5.initialize():
        error = str(mt5.last_error())
        note(f"cannot initialise MetaTrader5: {error}")
        append_jsonl(args.events, {"event": "watcher_failed", "ts": time.time(), "error": error})
        write_json(args.state, {
            "pid": os.getpid(), "started_at": started, "status": "failed", "error": error,
        })
        return 2

    for rule in read_rules(args.rules):
        try:
            mt5.symbol_select(str(rule.get("symbol")), True)
        except Exception:
            pass

    polls = 0
    exit_reason = "rules_satisfied"
    prices = {}
    #: Symbol -> the moment it stopped pricing; report_pending tracks the ones
    #: already reported, so the event log gets one line per outage, not one per
    #: 100 ms poll.
    unpriced_since = {}
    unpriced_reported = set()
    ever_priced = set()
    while True:
        now = time.time()
        # ``--max-seconds 0`` (the default) means NO limit: an exit at a price has
        # to be held for as long as the price takes to arrive, and a guard that
        # quietly stops after an hour leaves the level watched by nobody. A
        # positive value is still honoured for bounded runs and tests.
        if int(args.max_seconds) > 0 and now - started > int(args.max_seconds):
            exit_reason = "max_seconds"
            break
        if os.path.exists(args.stop_file):
            exit_reason = "stop_requested"
            break

        rules = read_rules(args.rules)
        if not rules:
            break

        changed = False
        for rule in list(rules):
            symbol = str(rule.get("symbol") or "")
            if not symbol:
                continue
            tick = mt5.symbol_info_tick(symbol)
            polls += 1
            if tick is None:
                mt5.symbol_select(symbol, True)
                since = unpriced_since.setdefault(symbol, now)
                if symbol not in unpriced_reported and now - since >= UNPRICEABLE_STALE:
                    unpriced_reported.add(symbol)
                    note(
                        f"{symbol} has had NO tick for {round(now - since, 1)} s -- "
                        f"{rule.get('id')} cannot be evaluated while that lasts"
                    )
                    append_jsonl(args.events, {
                        "event": "rule_unpriceable", "ts": now, "rule_id": rule.get("id"),
                        "symbol": symbol, "unpriced_seconds": round(now - since, 1),
                        "detail": "no tick from symbol_info_tick; the rule cannot fire "
                                  "until the symbol prices again",
                    })
                continue
            ever_priced.add(symbol)
            if symbol in unpriced_reported:
                unpriced_reported.discard(symbol)
                append_jsonl(args.events, {
                    "event": "rule_priceable", "ts": now, "symbol": symbol,
                    "outage_seconds": round(now - unpriced_since.get(symbol, now), 1),
                })
                note(f"{symbol} is pricing again after an outage")
            unpriced_since.pop(symbol, None)
            price, used_side = price_for_rule(rule, tick)
            prices[symbol] = price
            op = str(rule.get("op") or ">=")
            level = float(rule.get("price"))
            rule["last_price"] = price
            rule["last_price_ts"] = now
            if not triggered(price, op, level):
                continue
            if now - float(rule.get("last_attempt") or 0.0) < RETRY_COOLDOWN_SECONDS:
                continue
            rule["last_attempt"] = now
            trigger_ts = time.time()
            positions = matching_positions(rule)
            results = close_positions(rule, positions, args.deviation, args.magic)
            close_ts = time.time()
            ok = all(r.get("ok") for r in results) if results else True
            append_jsonl(args.events, {
                "event": "fired" if ok else "close_failed",
                "rule_id": rule.get("id"), "symbol": symbol, "op": op, "level": level,
                "side": used_side, "trigger_price": price,
                "bid": float(tick.bid), "ask": float(tick.ask),
                "tick_time": int(getattr(tick, "time", 0) or 0),
                "trigger_ts": trigger_ts, "close_ts": close_ts,
                "latency_ms": round((close_ts - trigger_ts) * 1000.0, 1),
                "positions_matched": len(positions), "results": results, "polls": polls,
            })
            if ok:
                note(
                    f"{rule.get('id')} FIRED {symbol} {op} {level} at {price} "
                    f"({used_side}) -- {len(positions)} position(s), "
                    f"{round((close_ts - trigger_ts) * 1000.0, 1)} ms to fill"
                )
            else:
                note(f"{rule.get('id')} close FAILED at {price}: {results}")
            if ok and rule.get("once", True):
                rules = [r for r in rules if r.get("id") != rule.get("id")]
                changed = True

        if changed:
            write_json(args.rules, rules)
        # Nothing has EVER been priced and every armed symbol is dark: this is
        # the misspelled-symbol case, not a market that has gone quiet. It stops
        # now, loudly, instead of polling an hour and looking like protection.
        if not ever_priced and prices == {} and unpriced_since:
            armed_symbols = {str(r.get("symbol") or "") for r in read_rules(args.rules)}
            if (
                armed_symbols <= set(unpriced_since)
                and now - min(unpriced_since.values()) >= UNPRICEABLE_STALE
            ):
                exit_reason = "unpriceable_symbol"
                break
        write_json(args.state, {
            "pid": os.getpid(), "started_at": started, "status": "running",
            "heartbeat": time.time(), "polls": polls, "interval_ms": args.interval_ms,
            "max_seconds": int(args.max_seconds),
            "prices": prices, "rules": len(read_rules(args.rules)),
            "unpriceable": {s: round(t, 1) for s, t in unpriced_since.items()},
        })
        time.sleep(max(0.01, args.interval_ms / 1000.0))

    write_json(args.state, {
        "pid": os.getpid(), "started_at": started, "status": "finished",
        "exit_reason": exit_reason, "finished_at": time.time(),
        "heartbeat": time.time(), "polls": polls,
        "rules": len(read_rules(args.rules)),
    })
    append_jsonl(args.events, {
        "event": "watcher_stop", "ts": time.time(), "exit_reason": exit_reason,
        "polls": polls, "ran_seconds": round(time.time() - started, 1),
    })
    note(f"finished: {exit_reason} after {polls} polls, "
         f"{round(time.time() - started, 1)} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _guard_python() -> tuple[str, Path] | None:
    """The (wine launcher, windows python) pair the watcher must run under."""
    winpy = win_python()
    if winpy is None:
        return None
    return wine_bin(), winpy


def _guard_is_live(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    if str(state.get("status")) != "running":
        return False
    try:
        heartbeat = float(state.get("heartbeat") or 0.0)
    except (TypeError, ValueError):
        return False
    return (time.time() - heartbeat) <= GUARD_HEARTBEAT_STALE


def _read_guard_state() -> dict[str, Any] | None:
    try:
        return json.loads(GUARD_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_guard_rules() -> list[dict[str, Any]]:
    try:
        data = json.loads(GUARD_RULES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _guard_events(limit: int) -> list[dict[str, Any]]:
    try:
        lines = GUARD_EVENTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _last_exit_reason() -> str | None:
    """Why the watcher stopped, from the event log (the state file is rewritten)."""
    for event in reversed(_guard_events(40)):
        if event.get("event") == "watcher_stop":
            reason = event.get("exit_reason")
            return str(reason) if reason else None
    return None


def _guard_max_seconds(value: Any, default: int = 0) -> int:
    """Normalise a guard time budget; ``0`` (or anything falsy) means NO limit.

    ``int(value or 3600)`` was the bug this replaces: an explicit ``0`` -- "watch
    until the level is reached, however long that takes" -- folded back to one
    hour, so the guard stopped exactly when the caller had said not to. A limit is
    a positive number of seconds; everything else means unlimited.
    """
    if value in (None, ""):
        return int(default)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return int(default)
    return seconds if seconds > 0 else 0


def _guard_fires_since(rule_ids: set[str], since: float) -> list[dict[str, Any]]:
    """Fired events for these rules that happened AT OR AFTER ``since``.

    The timestamp is the whole point. A rule id defaults to ``g<arm-time>-<index>``
    so a fresh arm gets a fresh id, but a caller may reuse an explicit id -- and
    re-arming one that already fired once would then find its own OLD fired event
    and be told "fired immediately" while the new watcher was in fact dead. That
    is the same lie in the opposite direction: a dead guard reported as a
    completed exit. ``since`` is the wall clock taken immediately before the
    watcher was started, and both sides run on the one container clock.
    """
    if not rule_ids:
        return []
    out: list[dict[str, Any]] = []
    for event in _guard_events(60):
        if event.get("event") not in ("fired", "close_failed"):
            continue
        if str(event.get("rule_id")) not in rule_ids:
            continue
        stamp = event.get("close_ts") or event.get("trigger_ts")
        try:
            when = float(stamp)
        except (TypeError, ValueError):
            continue
        if when >= float(since):
            out.append(event)
    return out


def _guard_current_price(symbol: str) -> float | None:
    """Mid price for a symbol, read through the bridge, or None if unavailable.

    Used only to INFER a trigger direction -- "close when it hits X" does not say
    which side of the market X is on. This costs one bridge round trip (~1-2 s),
    so it is only paid for rules that actually omit ``op``.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "quote", symbol],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    tick = ((payload.get("quotes") or {}).get(symbol) or {}).get("tick") or {}
    bid, ask = tick.get("bid"), tick.get("ask")
    if bid is None or ask is None:
        return None
    return (float(bid) + float(ask)) / 2.0


def _validate_rule(
    raw: dict[str, Any], index: int, price_hint: float | None = None
) -> dict[str, Any]:
    """Normalise one rule, or raise ValueError with the reason it cannot arm."""
    if not isinstance(raw, dict):
        raise ValueError(f"rule {index} is not an object")
    symbol = str(raw.get("symbol") or "").strip()
    if not symbol:
        raise ValueError(f"rule {index} needs a 'symbol'")
    if raw.get("price") is None:
        raise ValueError(f"rule {index} needs a 'price' (the level to act on)")
    try:
        level = float(raw["price"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rule {index}: 'price' is not a number ({exc})") from exc
    op = str(raw.get("op") or "").strip()
    inferred = False
    if not op:
        if price_hint is None:
            raise ValueError(
                f"rule {index}: give an 'op' (>= or <=), or arm a rule whose symbol "
                "price can be read so the direction can be inferred"
            )
        # "close when it hits X" does not say which side X is on. Inferring it
        # from the live price is what the person asking actually means, and it
        # removes the one input a caller most often gets backwards.
        op = ">=" if level >= price_hint else "<="
        inferred = True
    if op not in (">=", "<=", ">", "<"):
        raise ValueError(f"rule {index}: 'op' must be one of >= <= > < (got {op!r})")
    side = str(raw.get("side") or "mid").strip().lower() or "mid"
    if side not in ("bid", "ask", "mid"):
        raise ValueError(f"rule {index}: 'side' must be bid, ask or mid (got {side!r})")

    scope: dict[str, Any] = {}
    ticket = raw.get("ticket")
    scope_raw = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
    if ticket is None:
        ticket = scope_raw.get("ticket")
    if ticket not in (None, "", 0):
        try:
            scope = {"ticket": int(ticket)}
        except (TypeError, ValueError) as exc:
            raise ValueError(f"rule {index}: 'ticket' is not an integer") from exc
    elif scope_raw.get("all") or raw.get("all"):
        scope = {"all": True}
    else:
        scope = {"symbol": str(scope_raw.get("symbol") or symbol)}

    volume = raw.get("volume")
    if volume not in (None, "", 0):
        try:
            volume = float(volume)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"rule {index}: 'volume' is not a number") from exc
        if volume <= 0:
            raise ValueError(f"rule {index}: 'volume' must be positive")
    else:
        volume = None

    return {
        "id": str(raw.get("id") or f"g{int(time.time())}-{index}"),
        "symbol": symbol,
        "op": op,
        "price": level,
        "side": side,
        "scope": scope,
        "action": str(raw.get("action") or "close"),
        "volume": volume,
        "once": bool(raw.get("once", True)),
        "created_at": time.time(),
        "armed_by": "mt5_cli",
        "op_inferred": inferred,
        "price_at_arm": price_hint,
    }


def _guard_spawn(interval_ms: int, max_seconds: int, deviation: int) -> dict[str, Any]:
    """Write the watcher out and start it detached, then WAIT for a heartbeat.

    A pid is not evidence: the launcher subshell is gone the moment it returns,
    so the only thing that says the watcher is alive is the heartbeat it writes
    on its first loop. Every caller of this gets "started" or "failed", never
    "probably started" -- a guard that is assumed rather than confirmed is the
    whole failure mode this file exists to close.

    A stop file left behind by a previous run would kill the new watcher on its
    first loop, so it is removed here rather than trusted to be absent.
    """
    wine, winpy = _guard_python()
    GUARD_WATCH_SCRIPT.write_text(_GUARD_WATCH_SOURCE, encoding="utf-8")
    try:
        GUARD_STOP_FILE.unlink()
    except OSError:
        pass
    # Where this run's output starts in the append-only logs. Counted BEFORE the
    # spawn so the caller can tail only what THIS watcher wrote -- see _tail_new.
    log_mark = _log_line_count(GUARD_LOG_FILE)
    err_mark = _log_line_count(GUARD_ERR_FILE)
    inner = (
        f"{wine} {shlex.quote(_to_wine_path(winpy))} "
        f"{shlex.quote(_to_wine_path(GUARD_WATCH_SCRIPT))} "
        f"--rules {shlex.quote(_to_wine_path(GUARD_RULES_FILE))} "
        f"--events {shlex.quote(_to_wine_path(GUARD_EVENTS_FILE))} "
        f"--state {shlex.quote(_to_wine_path(GUARD_STATE_FILE))} "
        f"--stop-file {shlex.quote(_to_wine_path(GUARD_STOP_FILE))} "
        f"--interval-ms {int(interval_ms)} "
        f"--max-seconds {int(max_seconds)} "
        f"--deviation {int(deviation)}"
        f" >> {shlex.quote(str(GUARD_LOG_FILE))} "
        f"2>> {shlex.quote(str(GUARD_ERR_FILE))}"
    )
    proc = subprocess.run(
        ["sh", "-c", f"nohup setsid sh -c {shlex.quote(inner)} >/dev/null 2>&1 & echo $!"],
        env=wine_env(),
        capture_output=True,
        text=True,
    )
    launcher_pid = (proc.stdout or "").strip()
    started_wait = time.time()
    deadline = started_wait + float(os.environ.get("MT5_GUARD_ARM_WAIT", "12"))
    state: dict[str, Any] | None = None
    while time.time() < deadline:
        state = _read_guard_state()
        if _guard_is_live(state) or (state or {}).get("status") == "failed":
            break
        time.sleep(0.5)
    return {"state": state, "launcher_pid": launcher_pid,
            "log_mark": log_mark, "err_mark": err_mark,
            "waited_s": round(time.time() - started_wait, 1)}


def cmd_guard(args: argparse.Namespace) -> int:
    """Arm/inspect/stop the detached tick-level price guard."""
    subcommand = str(getattr(args, "guard_action", "status") or "status").lower()
    GUARD_DIR.mkdir(parents=True, exist_ok=True)

    if subcommand == "arm":
        raw_rules: list[dict[str, Any]] = []
        for blob in list(getattr(args, "rule", None) or []):
            try:
                parsed = json.loads(blob)
            except ValueError as exc:
                return fail(f"--rule is not valid JSON: {exc}", code=1)
            if isinstance(parsed, list):
                raw_rules.extend(parsed)
            else:
                raw_rules.append(parsed)
        if not raw_rules:
            return fail("guard arm needs at least one --rule '<json>'", code=1)

        # One bridge price read per distinct symbol, and it is paid for EVERY
        # rule -- not only the ones that omit 'op'. The read answers two
        # questions at once:
        #   * a rule without an 'op' needs a live price to infer its direction;
        #   * a symbol the CLI cannot price is a symbol the watcher will poll
        #     forever without ever evaluating a rule.
        # The second one is the point. MEASURED 2026-09-23 (MetaQuotes demo): a
        # rule on a misspelled symbol armed cleanly, answered guard="armed" with
        # a live heartbeat, polled 47 times, priced NOTHING, and was
        # indistinguishable from protection. A caller told "armed" about a guard
        # that cannot see the market is worse off than one told nothing.
        price_hints: dict[str, float] = {}
        unpriceable: list[str] = []
        for raw in raw_rules:
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol") or "").strip()
            if not symbol or symbol in price_hints or symbol in unpriceable:
                continue
            found = _guard_current_price(symbol)
            if found is None:
                unpriceable.append(symbol)
            else:
                price_hints[symbol] = found
        if unpriceable and not getattr(args, "allow_unpriceable", False):
            return fail(
                "refusing to arm: the CLI cannot read a price for "
                + ", ".join(repr(s) for s in unpriceable)
                + ". A guard on a symbol with no tick can never fire -- it would "
                "poll in silence and look exactly like protection. Check the "
                "symbol spelling with action='symbol' (or wait for the market to "
                "open), then arm again. Pass allow_unpriceable=true only if you "
                "know the symbol prices later and want the watcher to wait for it.",
                code=4,
                unpriceable=unpriceable,
                priced_symbols=sorted(price_hints),
            )

        rules: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_rules):
            try:
                hint = (
                    price_hints.get(str(raw.get("symbol") or "").strip())
                    if isinstance(raw, dict)
                    else None
                )
                rules.append(_validate_rule(raw, index, price_hint=hint))
            except ValueError as exc:
                return fail(str(exc), code=1)

        # Rules are validated BEFORE anything is armed, and merged by id so a
        # re-arm replaces its own rule instead of stacking duplicates that would
        # each fire a close.
        existing = {str(r.get("id")): r for r in _read_guard_rules()}
        for rule in rules:
            existing[rule["id"]] = rule
        merged = list(existing.values())
        try:
            GUARD_RULES_FILE.write_text(json.dumps(merged, default=str), encoding="utf-8")
        except OSError as exc:
            return fail(f"could not write {GUARD_RULES_FILE}: {exc}", code=2)

        # The moment the merged ruleset became the live one: the earliest time a
        # fire reported below can have been caused by THIS arm.
        armed_at = time.time()
        if _guard_python() is None:
            return fail(
                "the Windows python is not installed in the Wine prefix, so the "
                "tick-level guard cannot start. Run install first (the guard uses "
                "the MetaTrader5 module, which only publishes win_amd64 wheels).",
                code=2,
            )
        state = _read_guard_state()
        live = _guard_is_live(state)
        pid = str((state or {}).get("pid") or "")
        log_mark = 0
        err_mark = 0
        if not live:
            spawn = _guard_spawn(
                int(getattr(args, "interval_ms", 100) or 100),
                _guard_max_seconds(getattr(args, "max_seconds", None)),
                int(getattr(args, "deviation", 30) or 30),
            )
            state = spawn["state"]
            pid = spawn["launcher_pid"]
            log_mark = int(spawn.get("log_mark") or 0)
            err_mark = int(spawn.get("err_mark") or 0)

        state = _read_guard_state()
        live = _guard_is_live(state)
        payload: dict[str, Any] = {
            "ok": live,
            "guard": "armed" if live else "not_running",
            # The state file carries the watcher's OWN pid; ``pid`` is the
            # subshell that spawned it and is useless for anything.
            "pid": (state or {}).get("pid") or pid,
            "interval_ms": int(getattr(args, "interval_ms", 100) or 100),
            "max_seconds": _guard_max_seconds(getattr(args, "max_seconds", None)),
            "rules": _read_guard_rules(),
            "events_file": str(GUARD_EVENTS_FILE),
            "state_file": str(GUARD_STATE_FILE),
            "log_file": str(GUARD_LOG_FILE),
            "err_file": str(GUARD_ERR_FILE),
            "heartbeat": (state or {}).get("heartbeat"),
            "message": (
                "Guard is live: it polls the tick stream inside the sandbox and "
                "closes the instant the level is touched -- no model turn is "
                "involved. Poll action='guard' with guard_action='events' for the "
                "measured trigger->fill latency."
            ),
        }
        if not live:
            # A rule whose level is ALREADY satisfied fires on the first tick, so
            # the watcher can have done its entire job and exited before this call
            # finishes waiting for a heartbeat. MEASURED 2026-09-23 (MetaQuotes
            # demo, live): that came back guard="not_running" with ok=false -- a
            # complete, correct exit reported as a failed arm, which invites the
            # caller to arm a second guard over a position that is already closed.
            # The event log is what tells the two apart.
            armed_ids = {str(r.get("id")) for r in rules}
            strikes = _guard_fires_since(armed_ids, armed_at)
            fired_now = [e for e in strikes if e.get("event") == "fired"]
            failed_now = [e for e in strikes if e.get("event") == "close_failed"]
            if failed_now:
                # The level WAS touched and the close was REJECTED. That is not a
                # dead watcher and it is not a success: the position is still open
                # at a level the caller asked to be out at, and it has to be said
                # in those words or it reads as a broken arm.
                payload["ok"] = False
                payload["guard"] = "close_failed"
                payload["close_failed"] = [
                    {
                        "rule_id": e.get("rule_id"), "symbol": e.get("symbol"),
                        "level": e.get("level"),
                        "trigger_price": e.get("trigger_price"),
                        "results": e.get("results"),
                    }
                    for e in failed_now
                ]
                payload.pop("error", None)
                payload["message"] = (
                    "The level was already satisfied, the guard fired on the first "
                    "tick, and the CLOSE WAS REJECTED -- the position is still "
                    "open. Read 'close_failed' for the broker's answer."
                )
                return emit(
                    payload,
                    text="guard fired and the close failed",
                    code=2,
                )
            if fired_now:
                payload["ok"] = True
                payload["guard"] = "fired_immediately"
                payload["fired"] = [
                    {
                        "rule_id": e.get("rule_id"), "symbol": e.get("symbol"),
                        "op": e.get("op"), "level": e.get("level"),
                        "trigger_price": e.get("trigger_price"),
                        "latency_ms": e.get("latency_ms"),
                        "positions_matched": e.get("positions_matched"),
                    }
                    for e in fired_now
                ]
                payload["rules"] = _read_guard_rules()
                payload["message"] = (
                    "The level was already satisfied when the guard armed, so it "
                    "fired on the first tick and has stopped -- the exit has "
                    "already happened. Read 'fired' for the measured latency, and "
                    "action='positions' to confirm what is left open."
                )
                payload.pop("error", None)
                return emit(
                    payload,
                    text=f"guard fired immediately ({fired_now[-1].get('latency_ms')} ms)",
                )
            payload["error"] = (
                "the guard watcher did not report a heartbeat. Read log_file: a "
                "watcher that cannot import MetaTrader5 or reach the terminal "
                "writes 'watcher_failed' to the event log."
            )
            payload["log_tail"] = _tail_new(GUARD_LOG_FILE, 15, log_mark)
            payload["err_tail"] = _tail_new(GUARD_ERR_FILE, 15, err_mark)
            if not payload["log_tail"] and not payload["err_tail"]:
                payload["log_note"] = (
                    "this watcher wrote nothing at all to the log, so it died "
                    "before its first loop -- check that the shared directory is "
                    "mounted and that Wine can start the Windows python."
                )
            return emit(payload, text="guard failed to start", code=2)
        return emit(payload, text=f"guard armed ({len(payload['rules'])} rule(s))")

    if subcommand in ("status", "ensure"):
        state = _read_guard_state()
        live = _guard_is_live(state)
        rules = _read_guard_rules()
        if subcommand == "ensure" and rules and not live and _guard_python() is None:
            return fail(
                "the Windows python is not in the Wine prefix, so the guard cannot "
                "be restarted. The armed rules are UNPROTECTED: run install first, "
                "then call guard action='ensure' again.",
                code=2,
                rules_armed=len(rules),
                alert="guard_not_running",
            )
        if subcommand == "ensure" and rules and not live:
            # THE HOLE THIS CLOSES: a guard can stop for reasons that have nothing
            # to do with the market -- the watcher's own --max-seconds runs out,
            # the sandbox is suspended, the Wine prefix is restarted. Nothing then
            # tells the caller, and the level that used to be watched is watched
            # by nobody. Re-arming is one call, and the gap is REPORTED rather
            # than papered over, because anything that touched the level while the
            # watcher was down was missed.
            previous_hb = (state or {}).get("heartbeat")
            # Captured BEFORE the spawn overwrites the state file, or the reason
            # the guard had stopped is lost by the very call that recovers it.
            previous_exit = (state or {}).get("exit_reason") or _last_exit_reason()
            # A re-armed guard faces a market that has moved on. A rule whose level
            # is satisfied NOW fires on the first tick and the watcher is gone by
            # the time the heartbeat wait ends -- the exact case the arm path had
            # to be taught, and re-arming is where it is most likely: the level was
            # very often reached while nothing was watching.
            rearmed_at = time.time()
            spawn = _guard_spawn(
                int((state or {}).get("interval_ms") or 100),
                # Reuse the budget the guard was ARMED with when this call does
                # not restate one: a re-arm must not silently downgrade an
                # unlimited guard to an hour, or extend a deliberately bounded one.
                _guard_max_seconds(
                    getattr(args, "max_seconds", None),
                    default=_guard_max_seconds((state or {}).get("max_seconds")),
                ),
                int(getattr(args, "deviation", 30) or 30),
            )
            state = spawn["state"]
            live = _guard_is_live(state)
            strikes = [] if live else _guard_fires_since(
                {str(r.get("id")) for r in rules}, rearmed_at
            )
            fired_now = [e for e in strikes if e.get("event") == "fired"]
            # "rearmed" and "rearmed then fired" are different outcomes and only
            # one of them leaves a watcher running. Saying just "rearmed" for the
            # second leaves the caller believing a guard is live over a position
            # that is already closed; saying "rearm_failed" reports a completed
            # exit as a broken guard. Both are wrong, so they are separate.
            if live:
                action = "rearmed"
            elif fired_now:
                action = "rearmed_and_fired"
            else:
                action = "rearm_failed"
            payload: dict[str, Any] = {
                "ok": bool(live or fired_now),
                "action": action,
                "running": live,
                "previous_exit_reason": previous_exit,
                "unprotected_seconds": (
                    round(time.time() - float(previous_hb), 1)
                    if previous_hb not in (None, "")
                    else None
                ),
                "rules_armed": len(rules),
                "warning": (
                    "The guard had stopped and has been restarted. Anything that "
                    "touched an armed level while it was down was NOT acted on -- "
                    "check action='positions' before trusting the rules again."
                    if live
                    else "The guard could not be restarted; the armed rules are "
                         "still unprotected. Read log_file/err_file."
                ),
                "log_tail": _tail_new(GUARD_LOG_FILE, 10, int(spawn.get("log_mark") or 0))
                if not live else None,
                "err_tail": _tail_new(GUARD_ERR_FILE, 10, int(spawn.get("err_mark") or 0))
                if not live else None,
            }
            if fired_now:
                # The level that was reached while nothing was watching has been
                # acted on by the restarted guard, on its first tick.
                payload["fired"] = [
                    {
                        "rule_id": e.get("rule_id"), "symbol": e.get("symbol"),
                        "op": e.get("op"), "level": e.get("level"),
                        "trigger_price": e.get("trigger_price"),
                        "latency_ms": e.get("latency_ms"),
                        "positions_matched": e.get("positions_matched"),
                    }
                    for e in fired_now
                ]
                payload["warning"] = (
                    "The guard had stopped and was restarted; the level was "
                    "already reached, so it fired on its first tick and has "
                    "stopped again -- that exit has now happened. Anything that "
                    "touched the level BEFORE the restart was NOT acted on by this "
                    "fire; check action='positions'."
                )
                payload.pop("log_tail", None)
                payload.pop("err_tail", None)
            return emit(
                payload,
                text={
                    "rearmed": "guard re-armed",
                    "rearmed_and_fired": "guard re-armed and fired immediately",
                }.get(action, "guard re-arm failed"),
                code=0 if payload["ok"] else 2,
            )

        unpriceable = (state or {}).get("unpriceable") or {}
        # A stopped watcher with rules still armed is an ALARM, not a status. It
        # used to come back ok=True, which reads as "everything is fine" while
        # the level is being watched by nobody -- the one answer this action must
        # never give.
        alarm = None
        if rules and not live:
            alarm = "guard_not_running"
        elif live and unpriceable:
            alarm = "rule_unpriceable"
        payload = {
            "ok": alarm is None,
            "running": live,
            "state": state,
            "rules_armed": len(rules),
            "rules": rules,
            "interval_ms": (state or {}).get("interval_ms"),
            "polls": (state or {}).get("polls"),
            "prices": (state or {}).get("prices") or {},
            "unpriceable": unpriceable,
            "heartbeat_age_s": (
                round(time.time() - float((state or {}).get("heartbeat") or 0.0), 1)
                if (state or {}).get("heartbeat")
                else None
            ),
            "events_file": str(GUARD_EVENTS_FILE),
            "hint": (
                "A guard that is not running protects nothing: re-arm it with "
                "guard action='arm'. Read the event log for why it stopped."
                if not live
                else "Live. 'prices' is the tick the watcher is seeing right now."
            ),
        }
        if alarm:
            payload["alert"] = alarm
            payload["recovery"] = "guard action='ensure' (or arm again)"
            if alarm == "guard_not_running":
                payload["exit_reason"] = (state or {}).get("exit_reason")
                payload["warning"] = (
                    f"{len(rules)} rule(s) are armed and NOTHING is watching them: "
                    "the watcher stopped"
                    + (
                        f" ({state.get('exit_reason')})"
                        if (state or {}).get("exit_reason")
                        else ""
                    )
                    + ". Nothing will close on these levels until it runs again."
                )
            else:
                payload["warning"] = (
                    "the watcher is running but cannot price "
                    + ", ".join(sorted(unpriceable))
                    + f" (dark for {max(unpriceable.values())} s at last beat). A "
                    "rule on an unpriced symbol can never fire."
                )
        if not live:
            payload["last_events"] = _guard_events(5)
        return emit(payload)

    if subcommand == "stop":
        try:
            GUARD_STOP_FILE.write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            return fail(f"could not write the stop file: {exc}", code=2)
        deadline = time.time() + 5.0
        state = _read_guard_state()
        while time.time() < deadline and _guard_is_live(state):
            time.sleep(0.25)
            state = _read_guard_state()
        return emit(
            {
                "ok": True,
                "running": _guard_is_live(state),
                "note": (
                    "The watcher exits after its current loop (a stop FILE is used "
                    "on purpose -- killing by process name would match the shell "
                    "that launches it)."
                ),
                "rules_armed": len(_read_guard_rules()),
                "state": state,
            },
            text="guard stopped",
        )

    if subcommand == "clear":
        try:
            GUARD_RULES_FILE.write_text("[]", encoding="utf-8")
        except OSError as exc:
            return fail(f"could not clear the rules: {exc}", code=2)
        return emit({"ok": True, "rules_armed": 0, "state": _read_guard_state()})

    if subcommand == "events":
        events = _guard_events(int(getattr(args, "lines", 20) or 20))
        fired = [e for e in events if e.get("event") == "fired"]
        return emit(
            {
                "ok": True,
                "count": len(events),
                "events": events,
                "last_latency_ms": fired[-1].get("latency_ms") if fired else None,
                "note": (
                    "latency_ms is measured inside the watcher: the tick that "
                    "crossed the level to the broker's fill acknowledgement."
                ),
            }
        )

    return fail(
        f"unknown guard action {subcommand!r}; use arm, status, stop, clear, events or ensure",
        code=1,
    )


def _position_targets(mt5: Any, args: argparse.Namespace) -> tuple[list[Any], int | None]:
    """Resolve which positions ``modify`` should act on."""
    tickets = list(getattr(args, "ticket", None) or [])
    positions = list(mt5.positions_get() or [])
    if tickets:
        wanted = {int(t) for t in tickets}
        selected = [p for p in positions if int(p.ticket) in wanted]
        missing = wanted - {int(p.ticket) for p in selected}
        if missing:
            return selected, fail(
                "no open position with ticket(s) "
                + ", ".join(str(t) for t in sorted(missing))
                + " -- it may have already closed. Read action='positions'.",
                code=2,
            )
        return selected, None
    symbol = str(getattr(args, "symbol", "") or "").strip()
    if symbol:
        selected = [p for p in positions if p.symbol == symbol]
        if not selected:
            return [], fail(f"no open position on {symbol}", code=2)
        return selected, None
    if getattr(args, "all", False):
        if not positions:
            return [], fail("no open positions", code=2)
        return positions, None
    return [], fail(
        "modify needs a target: --ticket N, --symbol <SYMBOL>, or --all", code=1
    )


#: How a requested stop/target relates to the market it has to sit in.
LEG_EXACT = "exact"          # the server will hold it exactly as asked
LEG_ADJUSTED = "adjusted"    # too tight: moved to the nearest legal distance
LEG_WRONG_SIDE = "wrong_side"  # the market is already through it -- refused


def _place_leg(
    is_long: bool, leg: str, level: float, bid: float, ask: float, min_distance: float
) -> tuple[str, float]:
    """Where a requested SL/TP can legally sit, or why it cannot sit at all.

    ``leg`` is ``"sl"`` or ``"tp"``. A long's stop sits BELOW the bid and its
    target ABOVE the ask; a short is the mirror. Two things can be wrong with a
    level, and they are NOT the same thing:

    * TOO TIGHT -- it is on the correct side of the market but inside the
      broker's minimum stop distance, so the server answers retcode 10016
      "invalid stops". Moving it out to that distance keeps the caller's
      instruction and is reported.
    * WRONG SIDE -- the market has already gone through it. There is no honest
      clamp for that: moving it to the other side of the market turns "take
      profit at 1.1400" into a take profit ABOVE a long, which is the opposite
      instruction about the money. MEASURED 2026-09-23: the previous code did
      exactly that, silently reporting ``adjusted_from``. It is refused instead,
      and ``--exit-at`` is offered for a caller who really does mean "exit at
      this level" (a bare level carries no side, so routing it is right).

    Returns ``(status, placed_level)`` for the exact and adjusted cases.
    """
    if leg == "tp":
        bound = ask + min_distance if is_long else bid - min_distance
        wrong_side = level <= bid if is_long else level >= ask
        too_tight = level < bound if is_long else level > bound
    else:
        bound = bid - min_distance if is_long else ask + min_distance
        wrong_side = level >= bid if is_long else level <= ask
        too_tight = level > bound if is_long else level < bound
    if wrong_side:
        return LEG_WRONG_SIDE, level
    if too_tight:
        return LEG_ADJUSTED, bound
    return LEG_EXACT, level


def cmd_modify(args: argparse.Namespace) -> int:
    """Attach or move SL/TP on positions that are ALREADY open.

    THE GAP THIS FILLS: ``order --tp`` was the only way to get a broker-side
    exit, and only at order time. A position opened without a stop could not be
    given one afterwards, so "exit when it reaches X" had to be polled by the
    model -- which is exactly why the exit was late.
    """
    mt5, err = require_bridge()
    if err is not None:
        return err

    exit_at = getattr(args, "exit_at", None)
    sl = getattr(args, "sl", None)
    tp = getattr(args, "tp", None)
    if exit_at is None and sl is None and tp is None:
        return fail("modify needs --exit-at PRICE, or --sl/--tp", code=1)

    positions, target_error = _position_targets(mt5, args)
    if target_error is not None:
        return target_error

    results = []
    for position in positions:
        info = mt5.symbol_info(position.symbol)
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            results.append({"ticket": position.ticket, "ok": False, "error": "no tick"})
            continue
        is_long = position.type == mt5.POSITION_TYPE_BUY
        point = float(getattr(info, "point", 0.0) or 0.0)
        # Stops must clear the broker's minimum distance or the server rejects
        # them with retcode 10016 "invalid stops" -- which reads like a bad price
        # rather than a too-tight one.
        min_points = max(
            int(getattr(info, "trade_stops_level", 0) or 0),
            int(getattr(info, "trade_freeze_level", 0) or 0),
            1,
        )
        min_distance = min_points * point if point else 0.0

        new_sl = float(sl) if sl is not None else float(position.sl or 0.0)
        new_tp = float(tp) if tp is not None else float(position.tp or 0.0)
        routed = None
        #: Every leg this call actually moved, with the level that was asked for.
        #: A LIST, not one field: clamping both legs used to report only the last
        #: one, so half of what was changed about the caller's money was silent.
        adjustments: list[dict[str, Any]] = []

        if exit_at is not None:
            level = float(exit_at)
            # An "exit at X" says nothing about side, so it is routed to the stop
            # that can actually be held by the server. A long exits at the BID and
            # a short at the ASK (the price the closing deal is filled at), and
            # the level must clear that side by the broker's minimum distance --
            # a level inside the spread would otherwise come back 10016.
            upper = float(tick.ask) + min_distance
            lower = float(tick.bid) - min_distance
            # A level ABOVE the market protects a LONG in profit and a SHORT from
            # loss; below the market it is the other way round. Getting this
            # backwards would attach a take-profit where a stop was meant, which
            # is the one mistake that costs money instead of an error message.
            if is_long:
                if level >= upper:
                    routed, new_tp, new_sl = "tp", level, 0.0
                elif level <= lower:
                    routed, new_sl, new_tp = "sl", level, 0.0
                else:
                    routed = None
            else:
                if level >= upper:
                    routed, new_sl, new_tp = "sl", level, 0.0
                elif level <= lower:
                    routed, new_tp, new_sl = "tp", level, 0.0
                else:
                    routed = None
            if routed is None:
                # Too close to trade: clamp to the side the caller MEANT and say
                # so. Which side that is comes from the price the position is
                # VALUED at (the bid for a long, the ask for a short): a level
                # above it is "exit when the market comes up to me", below it is
                # the opposite. Deciding this by distance to the two legal bounds
                # instead made it a coin flip -- at a level between them the
                # comparison came down to binary float noise, so the SAME level
                # could attach a take-profit or a stop-loss on different ticks,
                # and those two mean opposite things about the money.
                meant_high = level >= (float(tick.bid) if is_long else float(tick.ask))
                # "Above the market" protects a long in profit (a target) and a
                # short from loss (a stop); below it is the mirror. Same routing
                # table as the two branches above, applied to the bound the caller
                # meant rather than the one their level happened to land nearest.
                if meant_high:
                    routed, new_tp, new_sl = ("tp", upper, 0.0) if is_long else ("sl", upper, 0.0)
                else:
                    routed, new_sl, new_tp = ("sl", lower, 0.0) if is_long else ("tp", lower, 0.0)
                adjustments.append({
                    "leg": routed, "requested": level,
                    "placed": new_tp if routed == "tp" else new_sl,
                })
        else:
            # An explicit --sl/--tp states the side, so each leg is checked
            # against the side it claims to be on. Only the legs the caller
            # PASSED are touched: re-clamping a stop that was already on the
            # position would let "modify --tp" quietly rewrite the caller's
            # protection, which is not what they asked for.
            refused: list[str] = []
            for leg, requested in (("tp", tp), ("sl", sl)):
                if requested is None:
                    continue
                level = float(requested)
                if not level:
                    # An explicit 0 REMOVES that leg rather than placing one at
                    # price zero, so there is nothing to check it against.
                    if leg == "tp":
                        new_tp = 0.0
                    else:
                        new_sl = 0.0
                    continue
                status, placed = _place_leg(
                    is_long, leg, level, float(tick.bid), float(tick.ask), min_distance
                )
                if status == LEG_WRONG_SIDE:
                    refused.append(leg)
                    continue
                if leg == "tp":
                    new_tp = placed
                else:
                    new_sl = placed
                if status == LEG_ADJUSTED:
                    adjustments.append({"leg": leg, "requested": level, "placed": placed})
            if refused:
                # The required side depends on the LEG as well as the direction:
                # a long's stop is below the market and its target is above it, so
                # naming one side for both would tell half the callers the exact
                # opposite of what they need to do.
                leg = sorted(refused)[0]
                must_be_below = (leg == "sl") == bool(is_long)
                side = "below" if must_be_below else "above"
                results.append({
                    "ticket": position.ticket,
                    "symbol": position.symbol,
                    "ok": False,
                    "error": "the level is on the wrong side of the market",
                    "wrong_side": sorted(refused),
                    "bid": float(tick.bid),
                    "ask": float(tick.ask),
                    "hint": (
                        f"a --{leg} on a "
                        f"{'long' if is_long else 'short'} has to be {side} the "
                        f"market (bid {tick.bid} / ask {tick.ask}), and the market "
                        "is already through the level you gave. Nothing was sent. "
                        "If you meant 'exit at this price' rather than a stop or a "
                        "target, use --exit-at and the side will be chosen for you."
                    ),
                })
                continue

        digits = int(getattr(info, "digits", 5) or 5)
        # Snap to the symbol's precision: the server compares prices digit by
        # digit and a 6-decimal level on a 5-digit symbol is an invalid stop.
        new_sl = round(new_sl, digits) if new_sl else 0.0
        new_tp = round(new_tp, digits) if new_tp else 0.0

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": int(position.ticket),
            "sl": new_sl,
            "tp": new_tp,
        }
        result = mt5.order_send(request)
        if result is None:
            results.append({
                "ticket": position.ticket, "ok": False,
                "error": str(mt5.last_error()), "request": request,
            })
            continue
        payload: dict[str, Any] = {
            "ticket": position.ticket,
            "symbol": position.symbol,
            "ok": result.retcode == mt5.TRADE_RETCODE_DONE,
            "retcode": result.retcode,
            "comment": result.comment,
            "sl": new_sl,
            "tp": new_tp,
            "routed_to": routed,
            "min_distance": min_distance,
        }
        if adjustments:
            payload["adjustments"] = adjustments
            # One scalar too, for the common single-leg case: "did my level
            # survive?" is the first thing read off a modify result.
            payload["adjusted_from"] = adjustments[0]["requested"]
            payload["adjust_reason"] = (
                "the requested level was inside the broker's minimum stop "
                f"distance ({min_distance}); it was moved just far enough for the "
                "server to hold it"
            )
        if not payload["ok"] and result.retcode == 10016:
            payload["hint"] = (
                "retcode 10016 means the stop is too close to the market for this "
                "symbol (or on the wrong side of it). Read min_distance/point from "
                "action='symbol' and move the level further out."
            )
        results.append(payload)

    ok = all(r.get("ok") for r in results) if results else False
    payload: dict[str, Any] = {
        "ok": ok,
        "modified": sum(1 for r in results if r.get("ok")),
        "results": results,
    }
    if ok:
        payload["note"] = (
            "The broker's server now holds this level: the exit is executed "
            "by MetaQuotes with no process and no model turn involved, and it "
            "survives this sandbox being paused or killed."
        )
    else:
        # A refusal must not be wrapped in a sentence that says the level is held:
        # that note was attached to every result, including the ones where
        # nothing was sent and the level is held by nobody.
        payload["note"] = (
            "NOTHING was changed: every result above with ok=false was refused, so "
            "that position still has whatever protection it had before."
        )
    return emit(
        payload,
        text=f"modified {sum(1 for r in results if r.get('ok'))} position(s)",
        code=0 if ok else 3,
    )


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


def _log_line_count(path: Path) -> int:
    """How many lines a log already holds, for tailing only what comes next."""
    text = _read_log_text(path)
    return len(text.replace("\x00", "").splitlines()) if text else 0


def _tail_new(
    path: Path, lines: int, skip: int, max_chars: int = _LOG_TAIL_MAX_CHARS
) -> str:
    """Last ``lines`` lines written AFTER the first ``skip`` lines of the file.

    A watcher log is appended to across runs, so the last lines of the file at a
    moment when the CURRENT watcher just died are easily the PREVIOUS watcher's.
    MEASURED 2026-09-23 (MetaQuotes demo, live): a failed arm reported "watching
    2 rule(s) every 100 ms; max 600 s" -- a guard that had finished four minutes
    earlier -- against the 120 s this one had actually been given. A diagnostic
    that describes a different run is worse than none, because it is read as
    this one and sends the caller looking for a bug that is not there.

    Counting lines rather than bytes is deliberate: the log is UTF-16LE from
    Wine, where a byte offset can land mid-character and decode to garbage.
    """
    text = _read_log_text(path)
    content = text.replace("\x00", "").splitlines() if text else []
    tail = "\n".join(content[max(0, int(skip)):][-max(1, int(lines)):])
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
    # The SERVER decides which broker build is installed, so this is the argument
    # that makes a first install correct for ANY broker. Omitted, the installer's
    # own default build is used and a mismatch is refused by the next start/login
    # with the exact install to run instead.
    p.add_argument(
        "--server",
        required=False,
        help="broker server the account lives on, e.g. 'Exness-MT5Trial9' or "
        "'MetaQuotes-Demo'; selects the matching MT5 build",
    )
    p.add_argument(
        "--broker-installer-url",
        required=False,
        default="",
        help="explicit broker-branded installer URL (overrides the registry)",
    )
    p.add_argument("--broker-dir-name", required=False, default="")
    # Detached is the default because sandbox commands are timeout-capped; the
    # caller polls ``status`` instead of holding one long command open.
    p.add_argument("--detach", dest="detach", action="store_true", default=True)
    p.add_argument("--foreground", dest="detach", action="store_false")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("status", help="install progress / stack readiness (pollable)")
    p.add_argument("--lines", type=int, default=25)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("start", help="launch the terminal headless")
    # Authorization against a broker is not instant -- ~130s from boot to "trading
    # has been enabled" on a cold prefix (IP discovery -> TCP connect -> auth ->
    # symbol sync of ~12k symbols). The wait is nevertheless bounded WELL BELOW the
    # caller's per-command ceiling, because a command the sandbox kills returns no
    # JSON at all, which reads as "the tool is broken" rather than "still
    # authorizing". Nothing is lost by returning early: the terminal keeps
    # authorizing in the background, so the next `account`/`start` call sees it.
    p.add_argument("--wait", type=int, default=90)
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

    p = sub.add_parser(
        "modify",
        help="attach/move SL or TP on an OPEN position (the broker then holds the exit)",
    )
    p.add_argument("--ticket", type=int, action="append", default=[], help="position ticket (repeatable)")
    p.add_argument("--symbol", default=None, help="every open position on this symbol")
    p.add_argument("--all", action="store_true", help="every open position")
    p.add_argument("--exit-at", type=float, default=None, help="price to exit at; routed to SL or TP by direction")
    p.add_argument("--sl", type=float, default=None, help="explicit stop loss (0 clears it)")
    p.add_argument("--tp", type=float, default=None, help="explicit take profit (0 clears it)")
    p.set_defaults(func=cmd_modify)

    p = sub.add_parser(
        "guard",
        help="detached tick-level watcher: closes the instant a price is touched",
    )
    p.add_argument(
        "guard_action",
        nargs="?",
        default="status",
        choices=["arm", "status", "stop", "clear", "events", "ensure"],
        help="arm rules, read status, stop, clear, read events, or restart a "
             "stopped watcher that still has rules armed",
    )
    p.add_argument("--rule", action="append", default=[], help="rule JSON (repeatable)")
    p.add_argument("--interval-ms", type=int, default=100, help="tick poll interval (default 100 ms)")
    p.add_argument(
        "--max-seconds",
        type=int,
        default=None,
        help=(
            "how long the guard may watch; omit (or 0) to hold the level for as "
            "long as it takes. A positive limit is a bounded run, and an expired "
            "guard is reported as an alert, never as protection."
        ),
    )
    p.add_argument("--deviation", type=int, default=30)
    p.add_argument(
        "--allow-unpriceable",
        action="store_true",
        help="arm even if a rule's symbol has no tick right now (the watcher then "
             "waits for it, and says so in the event log)",
    )
    p.add_argument("--lines", type=int, default=20, help="events to return")
    p.set_defaults(func=cmd_guard)

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
        # `modify` talks to the terminal (TRADE_ACTION_SLTP), so it must run
        # under Wine. `guard` deliberately does NOT appear here: arming the
        # watcher spawns wine FROM the Linux python, which the re-exec'd
        # Windows process could not do.
        "modify",
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