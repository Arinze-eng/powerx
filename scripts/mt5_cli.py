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
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
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
CLI_VERSION = "2026-09-24.9"

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


def _read_install_status() -> tuple[str, str]:
    """The raw ``stage|message`` a running installer last wrote, or unknown."""
    status_path = MT5_ROOT / "install.status"
    if not status_path.exists():
        return "unknown", ""
    raw = status_path.read_text(encoding="utf-8", errors="replace").strip()
    stage, _, message = raw.partition("|")
    return stage, message


#: The stages an install can reach and never leave on its own.
_TERMINAL_INSTALL_STAGES = frozenset({"done", "failed"})


def _install_fingerprint() -> dict[str, Any]:
    """The things that can MOVE during an install, as one comparable snapshot.

    These are the file and process signals, not the derived status snapshot. A
    10-25 minute install moves in exactly three ways -- the status file's stage
    changes, the installer writes more output, and the installer process goes
    away -- and each one is a reason for a caller to look again.

    The log is measured in BYTES rather than lines on purpose: the installer's
    own output is UTF-8, but it shells out to Wine, whose children can emit
    UTF-16LE, so a line count depends on which codec the reader guessed. A size
    does not.

    The ALIVENESS is sampled first, and that order is deliberate. An installer's
    last act is to write its outcome and flush its log, so the interesting poll
    is the one where all three signals move at once. Sampling aliveness first
    means the log and the stage are read *after* the process is known to be gone,
    so its dying words are in the same snapshot as its death -- whatever the
    caller then prioritises, nothing is missed for having been read a moment too
    early.
    """
    alive = _installer_alive()
    try:
        log_bytes = int((MT5_ROOT / "install.log").stat().st_size)
    except OSError:
        log_bytes = 0
    stage, message = _read_install_status()
    return {
        "stage": stage,
        "message": message,
        "log_bytes": log_bytes,
        "installer_alive": alive,
        "pending_target": _pending_install_target(),
    }


def _install_quiescent_event(fingerprint: dict[str, Any]) -> str | None:
    """The event name for "there is nothing here to wait for", or None.

    A wait against a finished install is not an observation, it is a 120 s stall
    that then reports a timeout -- which reads as "something is wrong" when the
    truth is "it is already over". Every state that cannot change on its own is
    named here so the caller is told which one it is, immediately.
    """
    if fingerprint["installer_alive"] or fingerprint["pending_target"]:
        return None
    stage = fingerprint["stage"]
    if stage == "failed":
        return "install_already_failed"
    if stage in _TERMINAL_INSTALL_STAGES:
        return "install_already_done"
    return "install_not_started"


def _install_wait(seconds: float, poll_seconds: float) -> dict[str, Any]:
    """Block, sampling the installer, until it moves or the budget runs out.

    WHY THIS EXISTS: ``install`` is DETACHED (a full Wine + MT5 + bridge install
    takes longer than any sandbox command ceiling), so ``status`` used to be the
    only way to follow it -- and ``status`` answered instantly. A caller in that
    position either hammers status in a loop it has to pace itself, or reports
    "installing" and stops, which is exactly the silence the whole CLI exists to
    remove: the model is told to wait, is given nothing to wait ON, and fills the
    gap with an assertion.

    This call OBSERVES. It samples every ``poll_seconds`` and returns the moment
    the stage changes, the installer's log grows, or the installer process exits
    -- or when the budget runs out, and it says which of the two it was. The
    caller gets the log tail of that moment, so "thinking about the install" has
    something new in it each time round instead of the same snapshot.

    Bounded at ``WATCH_MAX_WAIT_SECONDS`` deliberately, and the bound is returned
    in the answer, so the caller waits again rather than the wait being killed by
    a command timeout with nothing to show.
    """
    budget = max(0.0, min(float(seconds or 0.0), WATCH_MAX_WAIT_SECONDS))
    poll = max(0.2, float(poll_seconds or 1.0))
    started = time.time()
    before = _install_fingerprint()
    quiescent = _install_quiescent_event(before)
    if quiescent is not None:
        return {
            "observed": [{
                "event": quiescent,
                "ts": time.time(),
                "stage": before["stage"],
                "detail": (
                    f"nothing to wait for: stage is already '{before['stage']}' and "
                    "no installer is running"
                ),
            }],
            "observed_event": quiescent,
            "waited_s": 0.0,
            "samples": 0,
            "poll_seconds": poll,
            "timed_out": False,
            "capped_at_s": WATCH_MAX_WAIT_SECONDS,
            "from": before,
            "to": before,
            "note": (
                "no wait was needed: the install this could have watched is already "
                f"over (stage '{before['stage']}')."
            ),
        }

    deadline = started + budget
    samples = 0
    after = before
    observed: list[dict[str, Any]] = []
    while True:
        remaining = deadline - time.time()
        if remaining <= 0.0:
            break
        time.sleep(min(poll, remaining))
        samples += 1
        after = _install_fingerprint()
        # ORDER MATTERS. A stage change is the strongest signal (it is the
        # installer saying what it is doing), then the installer disappearing,
        # and only then "it wrote something". Checking the log first would hide
        # the exit: an installer that ends always writes to the log as its last
        # act, so the caller would be told "log grew" and never told that the
        # process that was doing the work is gone.
        if after["stage"] != before["stage"]:
            observed = [{
                "event": {
                    "done": "install_done",
                    "failed": "install_failed",
                }.get(after["stage"], "install_stage_changed"),
                "ts": time.time(),
                "from_stage": before["stage"],
                "stage": after["stage"],
                "message": after["message"],
                "detail": (
                    f"the install stage went '{before['stage']}' -> "
                    f"'{after['stage']}'"
                ),
            }]
        elif before["installer_alive"] and not after["installer_alive"]:
            observed = [{
                "event": "installer_exited",
                "ts": time.time(),
                "stage": after["stage"],
                "message": after["message"],
                "detail": (
                    "the installer process is gone; the stage file is whatever it "
                    "last wrote"
                ),
            }]
        elif after["log_bytes"] > before["log_bytes"]:
            observed = [{
                "event": "install_log_grew",
                "ts": time.time(),
                "grew_bytes": after["log_bytes"] - before["log_bytes"],
                "log_bytes": after["log_bytes"],
                "detail": "the installer wrote more output",
            }]
        if observed:
            break

    watched_seconds = round(time.time() - started, 2)
    return {
        "observed": observed,
        "observed_event": observed[-1]["event"] if observed else None,
        "waited_s": watched_seconds,
        "samples": samples,
        "poll_seconds": poll,
        "timed_out": not observed,
        "capped_at_s": WATCH_MAX_WAIT_SECONDS,
        "from": before,
        "to": after,
        "note": (
            f"observed '{observed[-1]['event']}' after {watched_seconds} s "
            f"({samples} samples)."
            if observed
            else (
                f"nothing moved in {watched_seconds} s of watching ({samples} "
                f"samples): the stage is still '{after['stage']}' and the installer "
                "is still running. This is an observation, not a claim."
            )
        ),
    }


def cmd_status(args: argparse.Namespace) -> int:
    """Report install progress and overall stack readiness (pollable)."""
    # A WAIT IS DONE FIRST, so the snapshot below describes what the install
    # looked like AFTER the wait rather than before it. Watching and then
    # reporting the pre-watch state is the failure mode this ordering avoids.
    watched: dict[str, Any] | None = None
    wait_seconds = float(getattr(args, "wait_seconds", 0.0) or 0.0)
    if wait_seconds > 0:
        watched = _install_wait(wait_seconds, float(getattr(args, "poll_seconds", 1.0) or 1.0))

    status_path = MT5_ROOT / "install.status"
    log_path = MT5_ROOT / "install.log"
    stage, message = _read_install_status()

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
    if watched is not None:
        # What the wait SAW, next to what the install looks like now. Both are
        # needed: the observation says why this call came back, the snapshot says
        # where the install is.
        payload["watched"] = watched
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


def _margin_mode_of(mt5: Any) -> dict[str, Any]:
    """Whether this account can hold MORE THAN ONE position on a symbol.

    THE BUG THIS CATCHES: under ``ACCOUNT_MARGIN_MODE_RETAIL_NETTING`` a symbol
    can hold exactly ONE position, and a second order does not open a second
    ticket -- it ADDS TO the first, at a blended price, with the first ticket's
    stop. Every part of split trading assumes the opposite: that N tickets exist,
    that "close 3 of them" is a thing, and that each carries its own entry. On a
    netting account ``split --splits 10`` would net into one position and
    ``close --group --count 3`` would have nothing to select, so the method would
    silently become a single oversized trade -- the exact opposite of granular
    exits, on a stop that now covers ten times the size.

    ``margin_mode`` is absent on older builds, so an unknown mode is reported as
    unknown and never assumed to be hedging.
    """
    acct = None
    try:
        acct = mt5.account_info()
    except Exception:  # noqa: BLE001 - an unreadable account is "unknown", not a crash
        acct = None
    raw = getattr(acct, "margin_mode", None) if acct is not None else None
    names = {0: "netting", 1: "exchange", 2: "hedging"}
    mode = names.get(int(raw)) if raw is not None else None
    return {
        "margin_mode": int(raw) if raw is not None else None,
        "margin_mode_name": mode or "unknown",
        #: Only an explicit hedging account may be told a split is safe. Unknown
        #: is NOT permissive: guessing wrong here multiplies a position's size.
        "hedging": mode == "hedging",
        "netting": mode == "netting",
        "multiple_positions_ok": mode == "hedging",
    }


def _position_margin(mt5: Any, symbol: str, volume: float, price: float) -> float | None:
    """Margin the broker says ``volume`` at ``price`` needs, or None if unknown."""
    for action in (
        getattr(mt5, "ORDER_TYPE_BUY", 0),
        getattr(mt5, "ORDER_TYPE_SELL", 1),
    ):
        try:
            value = mt5.order_calc_margin(action, symbol, float(volume), float(price))
        except Exception:  # noqa: BLE001 - a missing helper means "cannot precheck"
            return None
        if value is not None:
            return float(value)
    return None


def cmd_account(_: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err
    acct = mt5.account_info()
    if acct is None:
        return fail(f"account_info() returned None: {mt5.last_error()}", code=2)
    payload: dict[str, Any] = {"ok": True, "account": acct._asdict()}
    # Answered here rather than left in the raw struct, because the model asks
    # "can this account hold N positions?" far more often than it asks for the
    # numeric code, and getting it wrong turns a split into one big trade.
    payload["margin"] = _margin_mode_of(mt5)
    return emit(payload)



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


def _watch_quote(mt5: Any, symbol: str) -> dict[str, Any] | None:
    """One symbol's live bid/ask/mid, or None when it cannot be priced.

    None is returned rather than a zero-filled row on purpose: a symbol that
    cannot be priced is the difference between "watching" and "watching nothing",
    and a row of zeros would read as a real quote of 0.00000.
    """
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    bid = float(getattr(tick, "bid", 0.0) or 0.0)
    ask = float(getattr(tick, "ask", 0.0) or 0.0)
    if not bid and not ask:
        return None
    mid = round((bid + ask) / 2.0, 8) if (bid and ask) else (bid or ask)
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": round(ask - bid, 8) if (bid and ask) else None,
        "time_msc": int(getattr(tick, "time_msc", 0) or 0),
    }


#: ONE GOLD PIP, in price. Every gold stop, target and guard level in this file
#: is expressed in pips, so this single number decides whether "a 20-pip stop"
#: is a $2.00 stop or a $0.20 one.
#:
#: The convention comes from the trading guide this bridge trades by, whose own
#: worked example is ``entry 4162.50 / SL 4160.50 / TP 4176.50`` -- "20 pips"
#: and "140 pips". ``4162.50 - 4160.50 = 2.00``, so a pip is 0.10: ten points on
#: a two-decimal quote.
#:
#: This is NOT ``10 ** -digits``. MEASURED 2026-09-24 on a live Deriv-Demo
#: terminal: ``quote XAUUSD`` returns ``digits: 2, point: 0.01``, so the naive
#: rule gives 0.01 -- the point, not the pip -- and every gold distance in pips
#: is inflated 10x. A "20-pip" stop then sits $0.20 away, inside the spread of
#: the quote itself, and the position is stopped out on the next tick.
#:
#: Kept in step with ``GOLD_PIP`` in
#: ``nanobot/trading/gold_strategy.py``; a test pins both to 0.10.
GOLD_PIP = 0.10

#: Troy ounces in one Gold lot, so a pip is worth ``volume * contract * pip``.
#: Kept in step with ``GOLD_CONTRACT`` in ``nanobot/trading/gold_strategy.py``.
GOLD_CONTRACT = 100.0


def _is_gold_symbol(symbol: str) -> bool:
    """Whether ``symbol`` is Gold, by the broker's own spelling.

    Brokers label it ``XAUUSD``, ``GOLD``, ``frxXAUUSD``, ``XAUUSD.raw``,
    ``XAUUSDm`` … A substring test rather than a fixed list, because a symbol
    this fails to recognise silently falls back to the point-as-pip rule and
    gets a 10x-wrong stop.
    """
    name = (symbol or "").upper()
    return "XAU" in name or "GOLD" in name


def _watch_symbol_pips(mt5: Any, symbol: str) -> tuple[int, float]:
    """``(digits, pip)`` for a symbol; a sane default when it cannot be read."""
    info = mt5.symbol_info(symbol)
    digits = int(getattr(info, "digits", 5) or 5) if info is not None else 5
    if _is_gold_symbol(symbol):
        return digits, GOLD_PIP
    return digits, float(10 ** -digits)


def _analyse_trade(
    positions: list[Any],
    pips: dict[str, float],
    prices: dict[str, Any],
    contracts: dict[str, float] | None = None,
    equity: float | None = None,
) -> dict[str, Any]:
    """The decision material for the trade that is open RIGHT NOW.

    WHY THIS EXISTS: a polling loop that only reports prices and positions makes
    the caller re-derive the same arithmetic every 90 seconds -- how far to the
    stop, how much is at risk, how many R the trade has made, where breakeven is.
    That arithmetic is the part a model gets subtly wrong at 3 a.m. on call
    forty, and it is the part that decides whether money is taken or lost.

    So it is computed once, in one place, from the position itself:

    * ``r_multiple`` is ``(price_now - entry) / (entry - sl)`` -- a RATIO, so it
      needs no contract size and is right on Gold, EURUSD and anything else.
    * ``pips_to_sl`` / ``pips_to_tp`` use the symbol's own pip, so Gold's 0.10
      and EURUSD's 0.00001 are not conflated.
    * ``breakeven_price`` is the entry: the level to move the stop to once the
      trade has paid for its own risk.

    It deliberately does NOT act. It reports, and the caller decides -- a program
    that closes positions on its own arithmetic is an EA, and an EA cannot read
    the reason the price is where it is.
    """
    contracts = contracts or {}
    rows: list[dict[str, Any]] = []
    notes: list[str] = []

    for pos in positions:
        symbol = str(getattr(pos, "symbol", "") or "")
        pip = pips.get(symbol) or 0.0
        is_buy = int(getattr(pos, "type", 0) or 0) == 0
        entry = float(getattr(pos, "price_open", 0.0) or 0.0)
        sl = float(getattr(pos, "sl", 0.0) or 0.0)
        tp = float(getattr(pos, "tp", 0.0) or 0.0)
        volume = float(getattr(pos, "volume", 0.0) or 0.0)
        quote = prices.get(symbol) or {}
        now = quote.get("mid")
        now = float(now) if now is not None else None
        profit = float(getattr(pos, "profit", 0.0) or 0.0)

        # R is a ratio of PRICE distances, so it is currency- and
        # contract-size-agnostic -- the same formula is correct on Gold and on FX.
        # A stop of 0 is NO STOP, not a stop at zero: without this guard a naked
        # position divides by its whole entry price and reports a plausible,
        # meaningless R that hides the fact that its risk is unbounded.
        risk_price = (entry - sl) if is_buy else (sl - entry) if sl else 0.0
        r_multiple = None
        if now is not None and sl and risk_price > 0:
            moved = (now - entry) if is_buy else (entry - now)
            r_multiple = round(moved / risk_price, 2)

        per_pip = volume * (contracts.get(symbol) or 0.0) * pip if pip else 0.0
        risk_money = (
            round(risk_price / pip * per_pip, 2)
            if sl and risk_price > 0 and per_pip
            else None
        )

        row: dict[str, Any] = {
            "ticket": getattr(pos, "ticket", None),
            "symbol": symbol,
            "side": "buy" if is_buy else "sell",
            "volume": volume,
            "entry": entry,
            "price": now,
            "sl": sl or None,
            "tp": tp or None,
            "profit_money": round(profit, 2),
            "profit_pips": (
                round(((now - entry) if is_buy else (entry - now)) / pip, 1)
                if now is not None and pip
                else None
            ),
            "r_multiple": r_multiple,
            "risk_money": risk_money,
            "pips_to_sl": round(abs(now - sl) / pip, 1) if now is not None and sl and pip else None,
            "pips_to_tp": round(abs(tp - now) / pip, 1) if now is not None and tp and pip else None,
            "breakeven_price": entry,
            "comment": getattr(pos, "comment", ""),
        }

        # The two states that decide the next action, named rather than left for
        # the caller to notice: a position with no stop, and one that has earned
        # its risk but is still carrying it.
        if not sl:
            row["alert"] = "no_stop"
            notes.append(
                f"ticket {row['ticket']} on {symbol} has NO STOP: its risk is "
                "whatever the market decides. Set one with action=modify."
            )
        if r_multiple is not None and r_multiple >= 1.0 and sl:
            at_be = (sl >= entry) if is_buy else (sl <= entry)
            if not at_be:
                row["breakeven_due"] = True
                notes.append(
                    f"ticket {row['ticket']} is at {r_multiple}R and its stop is "
                    f"still {row['pips_to_sl']} pips away -- it has paid for its own "
                    f"risk. Moving sl to {entry} makes it free; a runner can then be "
                    "left with the target intact."
                )
        rows.append(row)

    open_profit = round(sum(r["profit_money"] for r in rows), 2)
    known_risk = [r["risk_money"] for r in rows if r["risk_money"] is not None]
    total_risk = round(sum(known_risk), 2) if known_risk else None
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]

    totals: dict[str, Any] = {
        "positions": len(rows),
        "volume": round(sum(r["volume"] for r in rows), 8),
        "profit_money": open_profit,
        "risk_money": total_risk,
        "risk_pct_of_equity": (
            round(total_risk / equity * 100.0, 2)
            if total_risk is not None and equity
            else None
        ),
        "total_r": round(sum(rs), 2) if rs else None,
        "worst_r": min(rs) if rs else None,
        "best_r": max(rs) if rs else None,
    }
    #: A risk figure that is a large share of the account is the fact a
    #: risk-conscious caller is asking for, and it is not visible from any one
    #: position.
    pct = totals["risk_pct_of_equity"]
    if pct is not None and pct >= 5.0:
        notes.append(
            f"{total_risk} is {pct}% of equity in open risk across "
            f"{len(rows)} positions. The playbook's own limit is 20% on ONE "
            "idea, so this is worth saying out loud before adding another."
        )
    return {"positions": rows, "totals": totals, "notes": notes}


def _watch_track(store: dict[str, Any], prices: dict[str, Any]) -> None:
    """Fold one sample into the per-symbol price path.

    The path is the reasoning material a single snapshot cannot give: "the price
    moved 3 pips and came back" and "the price sat still" are the same number in
    a snapshot and different facts about the market. Accumulated in-process so no
    extra bridge call is paid for it.
    """
    for symbol, quote in prices.items():
        row = store.setdefault(
            symbol,
            {"samples": 0, "first_mid": None, "last_mid": None,
             "min_mid": None, "max_mid": None},
        )
        if quote is None:
            continue
        mid = quote["mid"]
        row["samples"] += 1
        if row["first_mid"] is None:
            row["first_mid"] = mid
        row["last_mid"] = mid
        row["min_mid"] = mid if row["min_mid"] is None else min(row["min_mid"], mid)
        row["max_mid"] = mid if row["max_mid"] is None else max(row["max_mid"], mid)


def _watch_path_report(store: dict[str, Any], pips: dict[str, float]) -> dict[str, Any]:
    """Round the accumulated path and express the movement in pips."""
    out: dict[str, Any] = {}
    for symbol, row in store.items():
        pip = pips.get(symbol) or 0.0
        span = (
            round((row["max_mid"] - row["min_mid"]) / pip, 1)
            if pip and row["max_mid"] is not None else None
        )
        drift = (
            round((row["last_mid"] - row["first_mid"]) / pip, 1)
            if pip and row["last_mid"] is not None else None
        )
        out[symbol] = {
            "samples": row["samples"],
            "first_mid": row["first_mid"],
            "last_mid": row["last_mid"],
            "min_mid": row["min_mid"],
            "max_mid": row["max_mid"],
            "drift_pips": drift,
            "range_pips": span,
            "pip": pip or None,
        }
    return out


def _positions_by_ticket(mt5: Any) -> tuple[list[Any], bool]:
    """Open positions plus whether the terminal answered at all.

    ``positions_get`` returns None for a request that FAILED and an empty tuple
    for "nothing is open", so the two are kept apart here: a watch that treated a
    failed read as flat would report an open position as closed, which is the
    worst thing this command could say.
    """
    positions = mt5.positions_get()
    if positions is None:
        return [], False
    return list(positions), True


# --------------------------------------------------------------------------- #
# ONE observation, many calls -- `watch --session NAME`
# --------------------------------------------------------------------------- #
# WHY THIS EXISTS: `watch` is bounded at WATCH_MAX_WAIT_SECONDS per call, and it
# has to be -- a sandbox command cannot be held open for the life of a trade.
# But "watch this trade" is not a 90-second question; a trade runs for hours.
# Without continuity the caller gets a series of UNRELATED windows: each answers
# with its own first price, its own drift, its own range, and not one of them can
# say what the price has done SINCE the trade opened. Watching a long trade that
# way is watching a different trade every two minutes.
#
# `--session NAME` fixes exactly that, and nothing else. Every call carrying the
# same name folds its samples into a ledger on disk and returns the CUMULATIVE
# view next to this call's own, so consecutive calls are one continuous
# observation of one idea and the caller can THINK between slices rather than
# start over each time. That is the difference between polling and a loop of
# unrelated snapshots.
#
# The ledger is a plain JSON file under the MT5 root, so it survives the sandbox
# command that wrote it -- which is the point, because each call is a different
# command.
WATCH_SESSION_MAX_EVENTS = 200


def _watch_session_path(name: str) -> Path | None:
    """``<MT5_ROOT>/watch_sessions/<safe-name>.json``; ``None`` if unusable."""
    # No regex: a session name is a label, not an expression, and this file
    # keeps its dependency list to the stdlib it already imports.
    safe = "".join(
        c for c in str(name or "").strip() if c.isalnum() or c in "._-"
    )[:64]
    if not safe:
        return None
    return MT5_ROOT / "watch_sessions" / f"{safe}.json"


def _watch_session_load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _watch_session_fold(
    path: Path,
    name: str,
    symbols: list[str],
    track: dict[str, Any],
    pips: dict[str, float],
    observed: list[dict[str, Any]],
    watch_seconds: float,
    samples: int,
    tickets_at_start: set[int],
    trade_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold this call's samples into the ledger and return the cumulative view.

    The merge is per symbol and deliberately conservative: the first price is the
    one the SESSION started at (not this call), the last is always the newest,
    and min/max only ever widen. So a two-hour trade watched in eighty slices
    reports the true high and low of the whole two hours, which is the number a
    stop and a target are actually judged against.
    """
    ledger = _watch_session_load(path)
    now = time.time()
    ledger.setdefault("name", name)
    ledger["calls"] = int(ledger.get("calls") or 0) + 1
    ledger["first_at"] = float(ledger.get("first_at") or now)
    ledger["last_at"] = now
    ledger["watch_seconds"] = round(
        float(ledger.get("watch_seconds") or 0.0) + float(watch_seconds), 2
    )
    ledger["samples"] = int(ledger.get("samples") or 0) + int(samples)
    ledger["symbols_watched"] = sorted(
        set(ledger.get("symbols_watched") or []) | set(symbols)
    )
    if tickets_at_start:
        ledger.setdefault("tickets_at_start", sorted(tickets_at_start))

    stored: dict[str, Any] = ledger.get("symbols") or {}
    # What the previous call last saw, captured BEFORE this call overwrites it:
    # "moved since you last looked" is a different and more useful question than
    # "moved since this call began", and only the ledger can answer it.
    previous_last = {s: (row or {}).get("last_mid") for s, row in stored.items()}
    for symbol, row in (track or {}).items():
        if row.get("first_mid") is None:
            continue
        into = stored.setdefault(
            symbol,
            {"samples": 0, "first_mid": None, "last_mid": None,
             "min_mid": None, "max_mid": None},
        )
        into["samples"] = int(into.get("samples") or 0) + int(row.get("samples") or 0)
        if into.get("first_mid") is None:
            into["first_mid"] = row["first_mid"]
        into["last_mid"] = row["last_mid"]
        into["min_mid"] = (
            row["min_mid"] if into.get("min_mid") is None
            else min(into["min_mid"], row["min_mid"])
        )
        into["max_mid"] = (
            row["max_mid"] if into.get("max_mid") is None
            else max(into["max_mid"], row["max_mid"])
        )
    ledger["symbols"] = stored

    # The trade's own history, not just its price history: the best and worst this
    # position has been while this session watched it. A trade that was +3R an
    # hour ago and is -0.4R now is a different decision from one that has never
    # been in profit, and only the ledger saw the first one.
    totals = (trade_state or {}).get("totals") or {}
    if totals.get("positions"):
        path_state = ledger.setdefault("trade_path", {})
        for key, value, pick in (
            ("best_r", totals.get("best_r"), max),
            ("worst_r", totals.get("worst_r"), min),
            ("best_profit_money", totals.get("profit_money"), max),
            ("worst_profit_money", totals.get("profit_money"), min),
        ):
            if value is None:
                continue
            prior = path_state.get(key)
            path_state[key] = value if prior is None else pick(float(prior), float(value))
        path_state["last_profit_money"] = totals.get("profit_money")
        path_state["risk_money"] = totals.get("risk_money")
        path_state["risk_pct_of_equity"] = totals.get("risk_pct_of_equity")

    events = list(ledger.get("events") or [])
    for event in observed or []:
        events.append({"at": now, **event})
    ledger["events"] = events[-WATCH_SESSION_MAX_EVENTS:]
    ledger["event_count"] = len(events)

    ledger_error = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, indent=1, default=str), encoding="utf-8")
    except OSError:
        # A watch whose ledger cannot be written is still a valid watch. Losing
        # continuity is worth reporting, not worth failing the observation for.
        ledger_error = "could not write the session ledger"

    cumulative = _watch_path_report(stored, pips)
    since_last: dict[str, Any] = {}
    for symbol, row in cumulative.items():
        before = previous_last.get(symbol)
        after = row.get("last_mid")
        pip = pips.get(symbol) or 0.0
        since_last[symbol] = {
            "was": before,
            "now": after,
            "moved_pips": (
                round((after - before) / pip, 1)
                if before is not None and after is not None and pip
                else None
            ),
        }
    elapsed = round(now - float(ledger.get("first_at") or now), 1)
    return {
        "name": ledger.get("name"),
        "calls": ledger.get("calls"),
        "first_at": ledger.get("first_at"),
        "elapsed_s": elapsed,
        "watch_seconds_total": ledger.get("watch_seconds"),
        "samples_total": ledger.get("samples"),
        "event_count": ledger.get("event_count"),
        "tickets_at_start": ledger.get("tickets_at_start") or [],
        "price_path_total": cumulative,
        "since_last_call": since_last,
        # MFE/MAE across every call: the best and worst this trade has been since
        # the session opened, which is not recoverable from the current frame.
        "trade_path": ledger.get("trade_path") or None,
        "ledger": str(path),
        "ledger_error": ledger_error,
        "note": (
            f"session '{ledger.get('name')}': call {ledger.get('calls')}, "
            f"{ledger.get('watch_seconds')} s and {ledger.get('samples')} samples of "
            f"watching across {elapsed} s of wall clock. price_path_total is the "
            "WHOLE session, not this call, and since_last_call is the move since "
            "you last looked. Call again with the same session to continue it."
        ),
    }


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch a live trade: block, sample, and return what to think about.

    WHY THIS EXISTS: following a trade used to take three calls per turn --
    ``positions`` for the risk, ``quote`` for the price, ``guard status`` for
    whether anything is still watching the level -- each a separate trip and each
    a snapshot of a different instant. A caller doing that is looking at three
    photographs and guessing at the film.

    This call returns one frame of the film: the open positions, the live bid/ask
    of every symbol involved, how far each price TRAVELLED while it was watching,
    the guard's liveness and tick counts, and any event the watcher logged in the
    meantime. With ``wait_seconds`` it blocks first and returns the moment
    something happens -- a rule fires, a close is refused, a level is touched and
    reverted, the watcher stops, or the set of open positions changes -- so the
    caller is told that something happened rather than invited to assume it.

    Bounded at ``WATCH_MAX_WAIT_SECONDS``: the caller wants to watch in real time,
    not to hold one sandbox command open until it is killed. The cap is in the
    answer, so the next call continues the watch.
    """
    mt5, err = require_bridge()
    if err is not None:
        return err

    symbols = [
        s.strip().upper()
        for s in (getattr(args, "symbol", None) or [])
        if str(s).strip()
    ]
    budget = max(
        0.0, min(float(getattr(args, "wait_seconds", 0.0) or 0.0), WATCH_MAX_WAIT_SECONDS)
    )
    poll = max(0.2, float(getattr(args, "poll_seconds", 1.0) or 1.0))
    started = time.time()

    # The rules of engagement are the set of open positions, and the levels are
    # the guard's event log. Both are captured BEFORE the first sample so the
    # answer can say what changed relative to the moment the caller called.
    positions, terminal_ok = _positions_by_ticket(mt5)
    tickets_at_start = {int(p.ticket) for p in positions}
    if not symbols:
        # Default to the symbols at risk: a watch with no symbol and no positions
        # has nothing to say, and asking for one is the caller's job, not a guess.
        symbols = sorted({str(p.symbol) for p in positions if str(p.symbol)})
    pips = {s: _watch_symbol_pips(mt5, s)[1] for s in symbols}
    event_mark = len(_guard_event_lines())
    state = _read_guard_state()
    live = _guard_is_live(state)

    track: dict[str, Any] = {}
    prices: dict[str, Any] = {}
    samples = 0
    observed: list[dict[str, Any]] = []
    deadline = started + budget

    while True:
        now = time.time()
        if terminal_ok:
            prices = {s: _watch_quote(mt5, s) for s in symbols}
        samples += 1
        _watch_track(track, prices)

        events = _guard_events_after(event_mark)
        if events:
            # A logged event is the strongest signal there is: it is the watcher
            # saying, in its own words, that the level was reached, that the
            # broker refused, or that it is about to stop.
            observed = events
            break

        current, answered = _positions_by_ticket(mt5)
        if answered and not terminal_ok:
            terminal_ok = True
        if answered:
            tickets_now = {int(p.ticket) for p in current}
            if tickets_now != tickets_at_start:
                opened = sorted(tickets_now - tickets_at_start)
                closed = sorted(tickets_at_start - tickets_now)
                observed = [{
                    "event": "position_opened" if opened else "position_closed",
                    "ts": now,
                    "opened": opened,
                    "closed": closed,
                    "open_now": sorted(tickets_now),
                    "detail": (
                        "the set of open positions changed while this call was "
                        "watching"
                    ),
                }]
                break

        state_now = _read_guard_state()
        if live and not _guard_is_live(state_now):
            # The watcher died while this call was watching it. That is an
            # OBSERVATION, not a timeout: the armed levels are now watched by
            # nobody, and "timed out, nothing happened" would be exactly the
            # silence this command exists to remove.
            observed = [{
                "event": "watcher_stop",
                "ts": now,
                "exit_reason": (state_now or {}).get("exit_reason"),
                "detail": "the watcher stopped while this call was watching it",
            }]
            state = state_now
            break
        state = state_now

        remaining = deadline - time.time()
        if remaining <= 0.0:
            break
        time.sleep(min(poll, remaining))

    watched_seconds = round(time.time() - started, 2)
    positions, terminal_ok = _positions_by_ticket(mt5)
    steps = _watch_path_report(track, pips)
    guard = _guard_summary()

    # The trade's own numbers, so the caller decides on arithmetic it did not
    # have to redo. Contract size comes from the symbol (Gold and FX differ by
    # 100x); equity is what makes "risk" a share of the account rather than a
    # number with no scale.
    contracts: dict[str, float] = {}
    for symbol in {str(getattr(p, "symbol", "") or "") for p in positions}:
        if not symbol:
            continue
        info = mt5.symbol_info(symbol)
        if info is not None:
            contracts[symbol] = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
    equity = None
    try:
        account = mt5.account_info()
        if account is not None:
            equity = float(getattr(account, "equity", 0.0) or 0.0) or None
    except Exception:  # noqa: BLE001 - a watch is still valid without equity
        equity = None
    trade_state = _analyse_trade(positions, pips, prices, contracts, equity)

    payload: dict[str, Any] = {
        "ok": True,
        "position_count": len(positions),
        # The trade's own arithmetic: R multiple, pips to the stop and the
        # target, breakeven price, open risk in money and as a share of equity.
        # This is what a 90-second call is FOR -- without it the caller redoes
        # the same sums on call forty, at 3 a.m., and that is where money goes.
        "trade_state": trade_state,
        "positions": [p._asdict() for p in positions],
        "symbols_watched": symbols,
        "prices": prices,
        # How each price TRAVELLED, not just where it is. A snapshot cannot tell
        # "moved 3 pips and came back" from "sat still"; this can.
        "price_path": steps,
        "guard": guard,
        "guard_state": state,
        # Every recorded tick the watcher has examined, per symbol, and the last
        # level touched and already back inside. Together these answer "is it
        # watching?" with a count instead of a claim.
        "ticks_scanned": (state or {}).get("ticks_scanned") or {},
        "near_miss": (state or {}).get("near_miss") or {},
        "events": _guard_events(int(getattr(args, "lines", 20) or 20)),
        "terminal": {
            "available": terminal_ok,
            "last_error": None if terminal_ok else mt5.last_error(),
        },
        "watched": {
            "observed": observed,
            "observed_event": observed[-1].get("event") if observed else None,
            "waited_s": watched_seconds,
            "samples": samples,
            "poll_seconds": poll,
            "timed_out": not observed,
            "capped_at_s": WATCH_MAX_WAIT_SECONDS,
            "positions_at_start": sorted(tickets_at_start),
            "note": (
                f"observed '{observed[-1].get('event')}' after {watched_seconds} s "
                f"({samples} samples)."
                if observed
                else (
                    f"nothing happened in {watched_seconds} s of watching "
                    f"({samples} samples): no fire, no refused close, no near miss, "
                    "no position change, and the watcher is still up. "
                    + (
                        "Price moved: "
                        + "; ".join(
                            f"{s} {row['first_mid']} -> {row['last_mid']} "
                            f"(range {row['range_pips']} pips)"
                            for s, row in steps.items()
                            if row["first_mid"] is not None
                        )
                        + ". "
                        if any(r["first_mid"] is not None for r in steps.values())
                        else ""
                    )
                    + "This is an observation, not a claim."
                )
            ),
        },
    }
    # The continuous-observation view. Attached BEFORE the alert flags below so
    # a session is reported even when the terminal or the guard is unhappy --
    # losing the timeline exactly when something went wrong is the one time it
    # is worth most.
    session_name = str(getattr(args, "session", "") or "").strip()
    if session_name:
        session_path = _watch_session_path(session_name)
        if session_path is None:
            payload["session"] = {
                "error": f"session name '{session_name}' has no usable characters"
            }
        else:
            payload["session"] = _watch_session_fold(
                session_path,
                session_name,
                symbols,
                track,
                pips,
                observed,
                watched_seconds,
                samples,
                tickets_at_start,
                trade_state,
            )
            payload.setdefault("hint", payload["session"]["note"])
    if not terminal_ok:
        payload["ok"] = False
        payload["alert"] = "terminal_unavailable"
        payload["warning"] = (
            f"the terminal did not answer positions_get ({mt5.last_error()}), so the "
            "positions above are NOT known to be the whole picture. The guard state "
            "and event log are still read from disk and are unaffected."
        )
    # An observed refusal or a dead watcher is not a healthy result: the caller
    # asked to be out and is not, or the level is protected by nobody.
    bad = {"close_failed", "close_gave_up", "watcher_stop"}
    if payload["watched"]["observed_event"] in bad:
        payload["ok"] = False
        payload.setdefault("alert", payload["watched"]["observed_event"])
    if guard.get("alert"):
        payload.setdefault("alert", guard["alert"])
        payload.setdefault("warning", (
            f"the guard reports '{guard['alert']}': the levels armed on this "
            "account are not all being watched as they should be. Read the events "
            "above and act on it before treating any level as covered."
        ))
    if not symbols and not positions:
        payload["hint"] = (
            "nothing to watch yet: no open positions and no --symbol given. Place "
            "an order (or pass symbols) and call watch again."
        )
    elif not guard.get("live"):
        payload["hint"] = (
            "no guard watcher is running, so this call can see the price but "
            "nothing will act on it. Arm one with guard action='arm' -- a watch "
            "observes, it does not protect."
        )
    return emit(payload)


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


def _guard_retry_state(rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Closes the watcher is RETRYING, and the ones it gave up on.

    A refused close is not just a line in the event log any more: it is retry
    state on the rule, so "is anything still trying to get me out of this
    position?" has an answer that does not require reading the tail of a JSONL
    file. The two cases are kept apart on purpose -- still trying is protection
    with the broker saying no, given up is a position that needs a human.
    """
    armed = _read_guard_rules() if rules is None else rules
    now = time.time()
    retrying: list[dict[str, Any]] = []
    gave_up: list[dict[str, Any]] = []
    for rule in armed:
        if not isinstance(rule, dict):
            continue
        if rule.get("pending_since"):
            try:
                since = float(rule.get("pending_since") or now)
            except (TypeError, ValueError):
                since = now
            try:
                attempt_at = float(rule.get("last_attempt") or 0.0)
            except (TypeError, ValueError):
                attempt_at = 0.0
            retrying.append({
                "rule_id": rule.get("id"),
                "symbol": rule.get("symbol"),
                "attempts": int(rule.get("pending_attempts") or 0),
                "trying_for_s": round(now - since, 1),
                "retry_in_s": round(
                    max(0.0, attempt_at + GUARD_CLOSE_RETRY_COOLDOWN_SECONDS - now), 1
                ),
                "deadline_in_s": round(
                    float(rule.get("pending_deadline") or now) - now, 1
                ),
            })
        elif rule.get("gave_up_at"):
            gave_up.append({
                "rule_id": rule.get("id"),
                "symbol": rule.get("symbol"),
                "attempts": int(rule.get("gave_up_attempts") or 0),
                "retcodes": rule.get("gave_up_retcodes") or [],
                "gave_up_s_ago": round(
                    now - float(rule.get("gave_up_at") or now), 1
                ),
                "next_window_in_s": round(
                    max(0.0, float(rule.get("parked_until") or now) - now), 1
                ),
            })
    return {"retrying": retrying, "gave_up": gave_up}


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
    retry = _guard_retry_state(rules)
    alert = None
    if rules and not live:
        alert = "guard_not_running"
    elif live and retry["gave_up"]:
        # A close the watcher could not get through. The rule is still armed and
        # will try again, but until it does there is a position that a caller
        # believes is covered and that is not out yet.
        alert = "close_gave_up"
    elif live and retry["retrying"]:
        alert = "close_retrying"
    elif live and (state or {}).get("unpriceable"):
        alert = "rule_unpriceable"
    return {
        "live": live,
        "rules_armed": len(rules),
        "alert": alert,
        "retrying": retry["retrying"],
        "gave_up": retry["gave_up"],
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
        # The price the DEAL executed at. This is not the price the request asked
        # for, and on a market order they are not the same number: MEASURED
        # 2026-09-24 on a live Deriv-Demo terminal, a 10-way split of XAUUSD
        # requested at one price filled across 4284.06..4284.25. The request price
        # is what was ASKED; this is what the account got, and a report that gives
        # only the first is wrong about a dividend of the money.
        executed = getattr(result, "price", None)
        if executed:
            payload["price"] = float(executed)
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


# --------------------------------------------------------------------------- #
# SPLIT TRADING -- one idea, N positions
# --------------------------------------------------------------------------- #
# The method: instead of risking $100 on one 1.00-lot entry, open TEN 0.10-lot
# entries at the same price. Nothing about the idea changes -- same symbol, same
# direction, same stop, same total risk -- but the exits stop being all-or-
# nothing. Three can come off into a run, seven can be left to work, and the
# runners can be walked to breakeven so the rest of the trade is free.
#
# THE TWO THINGS THAT MAKE IT WORK, AND THE ONE THAT MAKES IT A TRAP:
#
#   * Total risk is IDENTICAL to the single position *only if every ticket
#     carries the same stop*. Ten 0.10 lots with a 20-pip stop risk exactly what
#     one 1.00 lot with a 20-pip stop risks. The splits multiply EXITS, not risk.
#     A split that leaves stops off the later tickets multiplies the risk
#     instead, and this command will not do it silently.
#   * Cost is not always proportional. Commission charged PER DEAL (common on
#     FX and metals) is paid ten times, and so is any slippage or requote, while
#     1.00 lot would have paid it once. On a 20-pip stop that difference is real
#     money, so `--check-cost` reports it before anything is sent.
#   * It is NOT a grid. Adding tickets as the price goes AGAINST you is the
#     martingale the trading guide warns can empty an account, and it is a
#     different command from this one: `split` fires exactly once, at one price,
#     with one stop, and refuses to fire again while its own group is open.
SPLIT_MAX_TICKETS = 50


def _split_group_tag(group: str) -> str:
    """A comment-safe group tag. Broker comments are short and often truncated."""
    safe = "".join(
        c for c in str(group or "").strip() if c.isalnum() or c in "._-"
    )[:24]
    return safe


def cmd_split(args: argparse.Namespace) -> int:
    """Open ONE idea as ``--splits`` positions of equal volume, in one call.

    Deliberately one bridge invocation: ``--splits 10`` as ten separate CLI calls
    would be ten Wine re-execs and ten windows in which the price moves between
    the first ticket and the last, which is the opposite of "the same price".
    Inside one call the tick is read once and every ticket prices off it.

    Every ticket gets the SAME sl and tp, so the split's total risk is the single
    position's total risk. That is the property the method depends on and the one
    that is checked in the answer.
    """
    mt5, err = require_bridge()
    if err is not None:
        return err

    count = int(getattr(args, "splits", 0) or 0)
    if count < 2 or count > SPLIT_MAX_TICKETS:
        return fail(
            f"--splits must be between 2 and {SPLIT_MAX_TICKETS}, got {count}. "
            "A split of one is just `order`.",
            code=1,
        )
    symbol = args.symbol
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return fail(f"symbol {symbol} unavailable", code=2)

    # A netting account holds ONE position per symbol, so N tickets would net
    # into a single trade at a blended price under the first ticket's stop. That
    # is not a split -- it is one oversized position, the opposite of granular
    # exits -- and it is refused BEFORE anything is sent, not reported after.
    mode = _margin_mode_of(mt5)
    if mode["netting"]:
        return fail(
            f"this account is NETTING (margin_mode={mode['margin_mode']}), so it "
            "holds only ONE position per symbol. A split would net all "
            f"{count} tickets into a single {args.volume}-lot trade at a blended "
            "price under one stop -- it would multiply size, not exits, which is "
            "the opposite of what this command is for. Use action=order for a "
            "single position and scale out of it with close --volume, or move to "
            "a HEDGING account, which is a change at the broker and not here.",
            code=1,
        )
    if mode["margin_mode"] is None:
        # Not refused -- a build that does not expose margin_mode must not block
        # a legitimate trade -- but the uncertainty is stated, because guessing
        # wrong here is the one error that multiplies the position.
        mode_note: str | None = (
            "margin_mode could not be read on this terminal, so it is UNKNOWN "
            "whether this account can hold several positions per symbol. If it is "
            "a netting account the tickets will net into one position."
        )
    else:
        mode_note = None

    side = args.side.lower()
    if side in ("buy", "long"):
        order_type = mt5.ORDER_TYPE_BUY
        price = float(tick.ask)
    elif side in ("sell", "short"):
        order_type = mt5.ORDER_TYPE_SELL
        price = float(tick.bid)
    else:
        return fail("side must be buy or sell", code=1)

    # Volume has to be split into lots the broker will actually accept. Rounding
    # DOWN to the step and refusing the remainder is the honest option: rounding
    # UP would make the split risk more than the caller asked for, which is the
    # one error this method cannot survive.
    step = float(getattr(info, "volume_step", 0.01) or 0.01)
    minimum = float(getattr(info, "volume_min", 0.01) or 0.01)
    maximum = float(getattr(info, "volume_max", 100.0) or 100.0)
    total = float(args.volume)
    per = total / count
    per = math.floor(per / step + 1e-9) * step
    per = round(per, 8)
    if per < minimum:
        return fail(
            f"{total} over {count} splits is {total / count} per ticket, below this "
            f"symbol's minimum lot of {minimum}. Use {int(total / minimum)} splits "
            f"or fewer, or raise the total volume.",
            code=1,
        )
    if per > maximum:
        return fail(f"per-ticket volume {per} exceeds the symbol maximum {maximum}", code=1)

    actual_total = round(per * count, 8)
    left_over = round(total - actual_total, 8)
    shortfall = left_over > step / 2

    # --- can the account actually carry this? -------------------------------- #
    # Ten tickets are ten separate margin reservations, and the failure mode is
    # not a clean refusal: the first few fill and the rest come back 10019 "no
    # money", leaving a HALF-OPEN split whose stop covers fewer tickets than
    # intended. That is much worse than being told before sending, so the
    # broker's own margin figure is asked for first.
    free_margin = None
    margin_needed = None
    try:
        acct = mt5.account_info()
        free_margin = float(getattr(acct, "margin_free", 0.0) or 0.0) if acct else None
    except Exception:  # noqa: BLE001 - no account info means no precheck, not a crash
        free_margin = None
    if free_margin:
        per_ticket_margin = _position_margin(mt5, symbol, per, price)
        if per_ticket_margin is not None:
            margin_needed = round(per_ticket_margin * count, 2)
            if margin_needed > free_margin:
                affordable = int(free_margin // per_ticket_margin) if per_ticket_margin else 0
                return fail(
                    f"{count} tickets of {per} lots need about {margin_needed} of "
                    f"margin and only {round(free_margin, 2)} is free, so this would "
                    f"half-fill: the first few tickets would open and the rest would "
                    "be refused for no money, leaving a partial position whose stop "
                    "covers fewer tickets than intended. "
                    + (
                        f"Use --splits {affordable} or fewer, or raise the free "
                        "margin."
                        if affordable >= 2
                        else "There is not enough free margin to split this at all -- "
                        "close something first, or trade a smaller total volume."
                    ),
                    code=1,
                )

    group = _split_group_tag(getattr(args, "group", "") or "") or _split_group_tag(
        args.comment or "split"
    )
    fillings = filling_candidates(mt5, info)
    base_comment = (args.comment or "powerx-split")[:16]

    results: list[dict[str, Any]] = []
    filled = 0
    for index in range(count):
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": per,
            "type": order_type,
            # Every ticket prices off the ONE tick read above, so they are the
            # same price and not merely near each other.
            "price": price,
            "deviation": int(args.deviation),
            "magic": int(args.magic),
            "comment": f"{base_comment}:{group}:{index + 1}of{count}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": fillings[0],
        }
        if args.sl is not None:
            request["sl"] = float(args.sl)
        if args.tp is not None:
            request["tp"] = float(args.tp)
        payload = _order_send(mt5, request, fillings)
        entry = {
            "index": index + 1,
            "volume": per,
            "ok": bool(payload.get("ok")),
            "retcode": payload.get("retcode"),
            "comment": payload.get("comment"),
        }
        result = payload.get("result") or {}
        for key in ("order", "deal", "price"):
            if result.get(key) is not None:
                entry[key] = result.get(key)
        # The executed price lives at the TOP level of an `_order_send` payload
        # (`price`), while a wrapper's nested `result` carries the ids. Read both,
        # so the actual fill is reported whichever shape the bridge returns.
        if entry.get("price") is None and payload.get("price") is not None:
            entry["price"] = payload["price"]
        results.append(entry)
        if payload.get("ok"):
            filled += 1
        elif args.stop_on_failure:
            break

    # --- what the account actually got --------------------------------------- #
    # "The same price" is the promise of this method, so it is MEASURED rather
    # than asserted. A market order fills at whatever the other side is when it
    # lands, and N of them land at N moments: MEASURED 2026-09-24 on a live
    # Deriv-Demo terminal, ten XAUUSD tickets requested at 4284.15 filled across
    # 4284.06..4284.25 -- 0.19 of price, which is 1.9 pips on a 20-pip stop, from
    # nothing but the market moving between tickets. A split is *near* one price,
    # never exactly one, and saying otherwise would make a backtest of this method
    # silently better than the method.
    fills = sorted(
        e["price"] for e in results if e.get("ok") and e.get("price") is not None
    )
    pip = _watch_symbol_pips(mt5, symbol)[1]
    fill_spread_pips = None
    if len(fills) > 1 and pip > 0:
        fill_spread_pips = round((fills[-1] - fills[0]) / pip, 1)

    # Risk is summed PER TICKET off each ticket's own fill, not off the one price
    # the request asked for: those differ by exactly the dispersion above, and on
    # the tickets that filled worst the stop is closer than planned.
    risk_money = None
    if args.sl is not None and fills:
        # Money per pip at this ticket size -- same arithmetic as
        # gold_strategy.money_per_pip (volume * contract * pip).
        per_pip = per * GOLD_CONTRACT * pip if _is_gold_symbol(symbol) else None
        if per_pip is not None:
            risk_money = round(
                sum(abs(f - float(args.sl)) / pip * per_pip for f in fills), 2
            )

    # `ok` is about TICKETS, not lots. A shortfall opens slightly less volume than
    # asked for, which is safe on purpose -- it carries a warning, and it is not a
    # failure. Reporting it as one would make a caller that retries a failed split
    # open a SECOND live position on top of the first. Only a ticket that did not
    # fill is a failure, and only that gets a non-zero exit.
    ok = filled == count
    out: dict[str, Any] = {
        "ok": ok,
        "action": "split",
        "symbol": symbol,
        "side": side,
        "price": price,
        "splits_requested": count,
        "splits_filled": filled,
        "volume_per_ticket": per,
        "volume_total": actual_total,
        "volume_left_over": left_over,
        "group": group,
        "sl": args.sl,
        "tp": args.tp,
        "total_risk_money": risk_money,
        # The account facts that decide whether a split is even meaningful here.
        "account_margin_mode": mode,
        "margin_required": margin_needed,
        "margin_free": round(free_margin, 2) if free_margin else None,
        # What the account actually got, versus the price that was asked for.
        "fill_price_first": fills[0] if fills else None,
        "fill_price_last": fills[-1] if fills else None,
        "fill_dispersion_pips": fill_spread_pips,
        "results": results,
        "note": (
            f"{filled}/{count} tickets of {per} lots on {symbol} at {price}. "
            f"Total {actual_total} lots. Every ticket carries the same stop, so the "
            "split's total risk is one position's risk -- it multiplies exits, not risk. "
            "Close any subset by ticket, or by group with close --group."
        ),
    }
    if mode_note:
        out["warning"] = ((out.get("warning", "") + " ") + mode_note).strip()
    if fill_spread_pips is not None and fill_spread_pips > 0:
        out["note"] += (
            f" Filled {fills[0]}..{fills[-1]} ({fill_spread_pips} pips of dispersion): "
            "a split is NEAR one price, not exactly one -- the market moved between "
            "tickets. Report the fills, not the requested price."
        )
    if shortfall:
        out["warning"] = (
            f"{total} lots does not divide into {count} x {per}: {left_over} lots "
            f"({int(left_over / step)} step(s)) is NOT open. The split is "
            f"{actual_total} lots, not {total} -- a smaller position than asked for, "
            "deliberately: rounding the last ticket up would risk more than you asked."
        )
    if filled < count:
        out["alert"] = "split_incomplete"
        out["warning"] = (
            (out.get("warning", "") + " ")
            + f"Only {filled} of {count} tickets filled (retcode "
            f"{results[-1].get('retcode') if results else 'n/a'}: "
            f"{results[-1].get('comment') if results else 'n/a'}). The position is "
            f"{per * filled} lots, not {actual_total}. This is a HALF-OPEN split: "
            "its stop covers fewer tickets than intended, and every ticket already "
            "filled is live right now."
        )
    if args.check_cost and args.sl is not None:
        out["cost_check"] = {
            "deals": count,
            "deals_if_single_position": 1,
            "note": (
                f"{count} deals pay any PER-DEAL commission {count} times where one "
                "1.00-lot deal pays it once. Spread cost is proportional to volume "
                "and is unchanged; slippage and requotes are per deal and are not."
            ),
        }
    code = 0 if ok else 3
    return emit(out, text=out["note"] if ok else out.get("warning", out["note"]), code=code)


def _symbol_pip_and_contract(
    mt5: Any, symbol: str, info: Any = None
) -> tuple[float, float]:
    """``(pip, contract_size)`` for a symbol. The two numbers risk math needs.

    Kept together so every caller that converts a stop distance into money uses
    the SAME pair -- Gold's 0.10 pip with a 100-oz contract and EURUSD's 0.00001
    with 100000 are the reason a money figure cannot be derived from a price
    distance alone.
    """
    pip = _watch_symbol_pips(mt5, symbol)[1]
    if info is None:
        info = mt5.symbol_info(symbol)
    contract = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
    return pip, contract


def _volume_for_risk(
    mt5: Any,
    symbol: str,
    info: Any,
    entry: float,
    sl: float,
    risk_money: float,
) -> tuple[float | None, dict[str, Any], str | None]:
    """The lot size that risks ``risk_money`` at this entry and stop.

    THE POINT: "risk $100 on this idea" is what a trader says, and turning that
    into lots is arithmetic over the stop distance, the pip and the contract size
    -- three places to be wrong, in the one calculation where being wrong costs
    money. Rounded DOWN to the broker's step, because rounding up would risk more
    than the caller asked for.
    """
    pip, contract = _symbol_pip_and_contract(mt5, symbol, info)
    step = float(getattr(info, "volume_step", 0.01) or 0.01)
    minimum = float(getattr(info, "volume_min", 0.01) or 0.01)
    maximum = float(getattr(info, "volume_max", 100.0) or 100.0)
    stop_distance = abs(float(entry) - float(sl))
    detail: dict[str, Any] = {
        "requested_risk_money": round(float(risk_money), 2),
        "stop_distance_price": round(stop_distance, 8),
        "pip": pip,
        "contract_size": contract,
    }
    if not pip or not contract or stop_distance <= 0:
        return None, detail, (
            "cannot size by risk: the pip, the contract size, or the distance "
            "between entry and stop is zero or unknown."
        )
    stop_pips = stop_distance / pip
    per_pip_per_lot = contract * pip
    raw = float(risk_money) / (stop_pips * per_pip_per_lot)
    volume = round(math.floor(raw / step + 1e-9) * step, 8)
    detail.update(
        {
            "stop_pips": round(stop_pips, 1),
            "money_per_pip_per_lot": round(per_pip_per_lot, 6),
            "volume_unrounded": round(raw, 8),
            "volume": volume,
        }
    )
    if volume < minimum:
        return None, detail, (
            f"risking {round(float(risk_money), 2)} with a {round(stop_pips, 1)}-pip "
            f"stop needs {round(raw, 4)} lots at {round(per_pip_per_lot, 4)} per pip "
            f"per lot, below this symbol's minimum of {minimum}. Raise the risk to at "
            f"least {round(minimum * stop_pips * per_pip_per_lot, 2)}, tighten the "
            "stop, or use a symbol where that risk is expressible."
        )
    if volume > maximum:
        return None, detail, (
            f"risking {round(float(risk_money), 2)} with a {round(stop_pips, 1)}-pip "
            f"stop needs {round(raw, 4)} lots, above this symbol's maximum of "
            f"{maximum}. Lower the risk or widen the stop."
        )
    return volume, detail, None


def _entry_type_constant(mt5: Any, side: str, entry_type: str) -> int:
    """The MT5 ``ORDER_TYPE_*`` for a side and an entry style."""
    is_buy = side in ("buy", "long")
    if entry_type == "limit":
        return mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
    if entry_type == "stop":
        return mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP
    return mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL


def _validate_pending_price(
    side: str, entry_type: str, price: float, tick: Any
) -> str | None:
    """Whether a pending price is on the correct side of the market, or why not.

    THE ERROR THIS PREVENTS: a buy limit ABOVE the ask, or a buy stop BELOW it, is
    rejected by the server as retcode 10015 "invalid price" -- which reads like a
    bad number rather than a limit on the wrong side of the market. It is an order
    that CANNOT work, and it is worth catching here instead of on a round trip.
    """
    ask = float(getattr(tick, "ask", 0.0) or 0.0)
    bid = float(getattr(tick, "bid", 0.0) or 0.0)
    is_buy = side in ("buy", "long")
    if entry_type == "limit":
        if is_buy and price >= ask:
            return (
                f"a BUY LIMIT must sit BELOW the market: {price} is at or above the "
                f"ask {ask}. To buy ABOVE the market that is a BUY STOP -- pass "
                "entry_type='stop'."
            )
        if not is_buy and price <= bid:
            return (
                f"a SELL LIMIT must sit ABOVE the market: {price} is at or below the "
                f"bid {bid}. To sell BELOW the market that is a SELL STOP -- pass "
                "entry_type='stop'."
            )
    if entry_type == "stop":
        if is_buy and price <= ask:
            return (
                f"a BUY STOP must sit ABOVE the market: {price} is at or below the "
                f"ask {ask}. To buy BELOW the market that is a BUY LIMIT -- pass "
                "entry_type='limit'."
            )
        if not is_buy and price >= bid:
            return (
                f"a SELL STOP must sit BELOW the market: {price} is at or above the "
                f"bid {bid}. To sell ABOVE the market that is a SELL LIMIT -- pass "
                "entry_type='limit'."
            )
    return None


def cmd_cancel(args: argparse.Namespace) -> int:
    """Remove a pending order that has not triggered yet."""
    mt5, err = require_bridge()
    if err is not None:
        return err

    tickets: list[int] = []
    if getattr(args, "all", False):
        tickets = [int(getattr(o, "ticket", 0)) for o in (mt5.orders_get() or [])]
        if not tickets:
            return emit(
                {
                    "ok": True,
                    "action": "cancel",
                    "cancelled": 0,
                    "note": "there were no pending orders to cancel.",
                }
            )
    else:
        if getattr(args, "ticket", None) is None:
            return fail(
                "cancel needs 'ticket', or all=true to remove every pending order.",
                code=1,
            )
        tickets = [int(args.ticket)]

    results = []
    for ticket in tickets:
        request = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
        payload = _order_send(mt5, request, [mt5.ORDER_FILLING_RETURN])
        results.append({"ticket": ticket, **payload})
    cancelled = [r for r in results if r.get("ok")]
    out: dict[str, Any] = {
        "ok": len(cancelled) == len(tickets),
        "action": "cancel",
        "requested": len(tickets),
        "cancelled": len(cancelled),
        "results": results,
        "note": (
            f"cancelled {len(cancelled)} of {len(tickets)} pending order(s). "
            "Nothing was closed -- a pending order that has not triggered holds no "
            "position."
        ),
    }
    if len(cancelled) != len(tickets):
        out["alert"] = "cancel_incomplete"
    return emit(out, text=out["note"], code=0 if out["ok"] else 3)


def _validate_stop_side(side: str, entry: float, sl: float | None) -> str | None:
    """Whether a stop sits on the losing side of the entry, or why it does not.

    A stop on the WINNING side is not a stop: the server rejects it, and if it
    did not, it would be a target that closes the trade the moment it is open.
    Caught here because the risk arithmetic divides by ``|entry - sl|``, so a
    wrong-side stop silently produces a plausible and meaningless lot size.
    """
    if sl is None:
        return None
    is_buy = side in ("buy", "long")
    if is_buy and float(sl) >= float(entry):
        return (
            f"a BUY stops OUT below the entry: sl {sl} is at or above entry "
            f"{entry}. That is a target, not a stop."
        )
    if not is_buy and float(sl) <= float(entry):
        return (
            f"a SELL stops OUT above the entry: sl {sl} is at or below entry "
            f"{entry}. That is a target, not a stop."
        )
    return None


def _validate_target_side(side: str, entry: float, tp: float | None) -> str | None:
    """Whether a target sits on the winning side of the entry, or why it does not.

    Mirror of ``_validate_stop_side``. A target on the losing side is the same
    server error -- retcode 10016 "Invalid stops" -- which names neither the leg
    nor the direction, so a typo'd tp reads like a broker fault.
    """
    if tp is None:
        return None
    is_buy = side in ("buy", "long")
    if is_buy and float(tp) <= float(entry):
        return (
            f"a BUY takes profit ABOVE the entry: tp {tp} is at or below entry "
            f"{entry}. That is a stop, not a target."
        )
    if not is_buy and float(tp) >= float(entry):
        return (
            f"a SELL takes profit BELOW the entry: tp {tp} is at or above entry "
            f"{entry}. That is a stop, not a target."
        )
    return None


def cmd_order(args: argparse.Namespace) -> int:
    """One order, at the market or resting at a price, sized in lots or in money.

    THREE THINGS THIS DOES THAT A MARKET ORDER ALONE CANNOT:

      * It can REST at a price (``entry_type=limit`` or ``stop``). "Buy the
        breakout above 4300" and "buy the dip at 4270" are the two most common
        instructions a trader gives, and neither is servable at the market --
        sending a market order instead fills at a price the caller did not ask
        for, and the difference is the whole trade.
      * It can be sized by the MONEY at risk (``risk_money`` / ``risk_pct``)
        instead of in lots. "Risk $100 on this" is what a person says; the lot
        size is arithmetic over the stop distance, and doing that arithmetic by
        hand is the error that costs money.
      * It refuses an entry that CANNOT work -- a limit on the wrong side of the
        market, a stop on the winning side of the entry -- before spending a
        round trip on a server rejection.
    """
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
    if side not in ("buy", "sell", "long", "short"):
        return fail("side must be buy or sell", code=1)
    is_buy = side in ("buy", "long")
    entry_type = (getattr(args, "entry_type", None) or "market").lower()
    if entry_type not in ("market", "limit", "stop"):
        return fail("entry_type must be market, limit or stop", code=1)

    # ---- WHERE the order enters -------------------------------------------
    requested_price = getattr(args, "price", None)
    if entry_type == "market":
        if requested_price is not None:
            return fail(
                "entry_type=market fills at the market, so 'price' cannot be "
                "honoured. Use entry_type=limit (to enter better than the market) "
                "or entry_type=stop (to enter on a break through price).",
                code=1,
            )
        price = float(tick.ask if is_buy else tick.bid)
    else:
        if requested_price is None:
            return fail(
                f"entry_type={entry_type} needs 'price' -- that price IS the entry.",
                code=1,
            )
        bad = _validate_pending_price(side, entry_type, float(requested_price), tick)
        if bad:
            return fail(bad, code=1)
        price = float(requested_price)

    # BOTH legs are checked against the entry BEFORE anything is sent, on every
    # path. MEASURED live 2026-09-24 on Deriv-Demo: a buy limit resting at
    # 4282.05 carrying an sl of 4285.05 came back from the broker as retcode
    # 10016 "Invalid stops", which names neither the leg, the direction, nor the
    # entry it should have been measured against.
    for bad in (_validate_stop_side(side, price, args.sl),
                _validate_target_side(side, price, args.tp)):
        if bad:
            return fail(bad, code=1)

    # ---- HOW BIG, in lots or in money -------------------------------------
    risk_money = getattr(args, "risk_money", None)
    risk_pct = getattr(args, "risk_pct", None)
    sizing: dict[str, Any] | None = None
    if args.volume is not None:
        if risk_money is not None or risk_pct is not None:
            return fail(
                "pass volume OR risk_money/risk_pct, not both: two sizes for one "
                "order is two answers to one question.",
                code=1,
            )
        volume = float(args.volume)
    else:
        if risk_money is None and risk_pct is None:
            return fail(
                "order needs a size: 'volume' in lots, or 'risk_money'/'risk_pct' "
                "together with 'sl'.",
                code=1,
            )
        if risk_money is not None and risk_pct is not None:
            return fail("pass risk_money OR risk_pct, not both.", code=1)
        if args.sl is None:
            return fail(
                "sizing by risk needs 'sl': the distance from the entry to the stop "
                "IS the risk. Without a stop the risk is the whole account.",
                code=1,
            )
        if risk_pct is not None:
            account = mt5.account_info()
            equity = float(getattr(account, "equity", 0.0) or 0.0)
            if equity <= 0:
                return fail(
                    "cannot size by percentage: the account equity is unreadable.",
                    code=2,
                )
            risk_money = equity * float(risk_pct) / 100.0
        volume, sizing, sizing_error = _volume_for_risk(
            mt5, symbol, info, price, float(args.sl), float(risk_money)
        )
        if volume is None:
            return fail(
                sizing_error or "cannot size this risk on this symbol",
                code=1,
                sizing=sizing,
            )
        sizing["risk_pct_of_equity"] = None

    # ---- the request ------------------------------------------------------
    pending = entry_type != "market"
    if pending:
        action = mt5.TRADE_ACTION_PENDING
        order_type = _entry_type_constant(mt5, side, entry_type)
    else:
        action = mt5.TRADE_ACTION_DEAL
        order_type = _entry_type_constant(mt5, side, "market")

    fillings = filling_candidates(mt5, info)
    request = {
        "action": action,
        "symbol": symbol,
        "volume": float(volume),
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
    payload["entry_type"] = entry_type
    payload["side"] = side
    payload["symbol"] = symbol
    payload["volume"] = float(volume)
    payload["request_price"] = float(price)
    if sizing is not None:
        # The arithmetic that produced the lot size is reported WITH the order:
        # a size the caller cannot check is a size the caller has to trust.
        payload["sizing"] = sizing
        account = mt5.account_info()
        equity = float(getattr(account, "equity", 0.0) or 0.0)
        if equity > 0:
            sizing["risk_pct_of_equity"] = round(
                sizing["requested_risk_money"] / equity * 100.0, 2
            )
            payload["risk_pct_of_equity"] = sizing["risk_pct_of_equity"]
    if (
        sizing is not None
        and not pending
        and payload["ok"]
        and args.sl is not None
        and payload.get("price") is not None
    ):
        # A market order fills at whatever the other side is when it lands, so
        # the stop distance measured at fill time is not the one the size was
        # derived from. Reporting the price PAID and the risk it implies is what
        # makes the size checkable instead of merely plausible: MEASURED live
        # 2026-09-24, the ask moved 0.18 between the quote and the fill, which
        # turned a 20.0-pip stop into 21.8 pips.
        pip, contract = _symbol_pip_and_contract(mt5, symbol, info)
        fill_pips = abs(float(payload["price"]) - float(args.sl)) / pip
        sizing["fill_stop_pips"] = round(fill_pips, 1)
        sizing["actual_risk_money"] = round(
            fill_pips * contract * pip * float(volume), 2
        )
        payload["risk_pct_of_equity"] = (
            round(sizing["actual_risk_money"] / equity * 100.0, 2)
            if equity > 0
            else None
        )
    if payload["ok"] and pending:
        payload["pending"] = True
        payload["order_ticket"] = payload.get("order")
        payload["note"] = (
            f"{entry_type} order resting at {price}: it holds NO position and risks "
            "nothing until the market reaches it. Cancel it with cancel --ticket."
        )
    elif payload["ok"]:
        payload["filled"] = True
        if payload.get("price") is not None:
            payload["fill_price"] = payload["price"]
    code = 0 if payload["ok"] else 3
    if payload["ok"]:
        note = payload.get("note") or "order filled"
    else:
        note = f"order rejected: {payload.get('comment')}"
    return emit(payload, text=note, code=code)

def _close_one(mt5: Any, pos: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Close one position (or part of it), resolving the symbol's filling mode.

    Split out of ``cmd_close`` so the group path and the single-ticket path send
    byte-identical requests. Two code paths that close positions must not differ
    in how they resolve a filling mode: one of them would start failing with
    retcode 10030 on FOK/IOC-only symbols and the other would not.
    """
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return {"ok": False, "retcode": None, "comment": f"no tick for {pos.symbol}"}
    closing_long = pos.type == mt5.POSITION_TYPE_BUY
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": float(getattr(args, "volume", None) or pos.volume),
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
    return _order_send(
        mt5, request, filling_candidates(mt5, mt5.symbol_info(pos.symbol))
    )


def _positions_in_group(mt5: Any, group: str) -> list[Any]:
    """Every open position whose comment carries ``:<group>:``.

    The tag is matched with its colons because `split` writes
    ``<base>:<group>:<n>of<total>`` and a bare substring match would let a group
    named ``a`` also select a group named ``abc``. The colons make the label a
    field rather than a coincidence.
    """
    needle = f":{group}:"
    return [
        p for p in (mt5.positions_get() or []) if needle in str(getattr(p, "comment", ""))
    ]


def cmd_close(args: argparse.Namespace) -> int:
    mt5, err = require_bridge()
    if err is not None:
        return err

    # Closing PART of a split is the whole point of splitting, so `close` also
    # takes a group: "take 3 of the 10 off" is one call, not three, and doing it
    # as three calls means the price moves between them -- on a book that exists
    # precisely to make exits granular.
    group = _split_group_tag(getattr(args, "group", "") or "")
    if group:
        group_positions = _positions_in_group(mt5, group)
        if not group_positions:
            return fail(f"no open positions carry the split group '{group}'", code=2)
        # Deterministic order so "close 3" is reproducible: FIFO would depend on
        # the broker's sort, and the caller is choosing a NUMBER, not a ticket.
        group_positions.sort(key=lambda p: (str(getattr(p, "comment", "")), int(p.ticket)))
        count = int(getattr(args, "count", 0) or 0)
        chosen = group_positions if count <= 0 else group_positions[:count]
        results = []
        for pos in chosen:
            payload = _close_one(mt5, pos, args)
            results.append({"ticket": pos.ticket, "volume": float(pos.volume), **payload})
        closed = [r for r in results if r.get("ok")]
        ok = len(closed) == len(chosen)
        out = {
            "ok": ok,
            "action": "close_group",
            "group": group,
            "selected": len(chosen),
            "closed": len(closed),
            "still_open": len(group_positions) - len(closed),
            "results": results,
            "note": (
                f"closed {len(closed)} of {len(chosen)} selected from group '{group}'; "
                f"{len(group_positions) - len(closed)} of that group remain open."
            ),
        }
        if not ok:
            out["alert"] = "close_incomplete"
        return emit(out, text=out["note"], code=0 if ok else 3)

    positions = mt5.positions_get(ticket=int(args.ticket))
    if not positions:
        return fail(f"no position with ticket {args.ticket}", code=2)
    payload = _close_one(mt5, positions[0], args)
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

#: Mirrors of the watcher's close-retry constants. They live INSIDE
#: ``_GUARD_WATCH_SOURCE`` (that source runs under the sandbox's Wine python and
#: cannot import anything from here), but the CLI has to be able to say how long
#: a refused close will keep trying, so it needs the numbers too. A test keeps
#: the two copies equal rather than trusting them to stay in step.
GUARD_CLOSE_RETRY_COOLDOWN_SECONDS = 1.5
GUARD_CLOSE_RETRY_DEADLINE_SECONDS = 30.0
GUARD_CLOSE_RETRY_MAX_ATTEMPTS = 20
GUARD_CLOSE_RETRY_PARK_SECONDS = 60.0
#: The first few retries after a refusal go out FAST, then the cadence falls
#: back to ``GUARD_CLOSE_RETRY_COOLDOWN_SECONDS``. MEASURED 2026-09-23: a
#: refusal is usually transient (a requote, a price that moved under a
#: market order), and the flat 1.5 s cooldown made a deal that would have been
#: accepted 300 ms later sit for 1.5 s -- 20 attempts spread over 29.4 s. A fast
#: opening burst costs nothing when the refusal is real, because the burst is
#: bounded and the give-up deadline and park still apply.
GUARD_CLOSE_RETRY_FAST_ATTEMPTS = 4
GUARD_CLOSE_RETRY_FAST_COOLDOWN_SECONDS = 0.25
#: How far back each loop rereads recorded ticks, and the floor on how long a
#: touch has to have lasted before it is worth reporting as a near miss.
GUARD_TICK_SCAN_LOOKBACK_SECONDS = 3.0
GUARD_NEAR_MISS_COOLDOWN_SECONDS = 5.0
#: The ceiling on ONE blocking watch, whatever it is watching: a guard's events
#: (``guard status --wait-seconds``), a live trade (``watch``), or an install
#: (``status --wait-seconds``). All three are one number on purpose.
#:
#: 90 s, not 120 s, because 120 s is the ceiling for a single SANDBOX COMMAND --
#: the whole round trip, not the wait. A caller wants to watch in real time, not
#: to hold a command open until the sandbox kills it, and a killed command
#: returns NO JSON AT ALL, which reads as "the tool is broken" rather than
#: "still waiting". Capping the wait at 90 s leaves room for the bootstrap fetch
#: and, on the two Wine-side paths, the re-exec into Wine, and still lands under
#: the ceiling. Every answer carries ``capped_at_s`` and ``timed_out``, so the
#: model calls again rather than being cut off with nothing to show.
WATCH_MAX_WAIT_SECONDS = 90.0

#: The guard's wait is whichever name a reader looks for; it is the same number
#: and must stay the same number, or one path would be holding a command open
#: past the ceiling the others respect.
GUARD_MAX_WAIT_SECONDS = WATCH_MAX_WAIT_SECONDS

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
#:
#: MEASURED 2026-09-23: the cooldown was INERT for the case it was written for.
#: The rule was only rewritten to the rules file when it fired and was consumed,
#: so a FAILED attempt's ``last_attempt`` was dropped on the next pass (the rules
#: file is re-read every loop) and a persistent rejection re-sent a close to the
#: broker on every 100 ms poll. The retry state below is therefore persisted, and
#: a test asserts the spacing between consecutive refusals.
RETRY_COOLDOWN_SECONDS = 1.5
#: How long a DECIDED close keeps being retried before the watcher says out loud
#: that it cannot get the position out. A close that was refused is retried until
#: the broker ACCEPTS it or this deadline passes -- not merely while the rule
#: happens to stay armed and the price happens to stay beyond the level.
CLOSE_RETRY_DEADLINE_SECONDS = 30.0
#: ...or this many attempts, whichever comes first. 20 attempts x the 1.5 s
#: cooldown is the same 30 s, so whichever the tick rate reaches first is fine.
CLOSE_RETRY_MAX_ATTEMPTS = 20
#: After giving up, the rule is PARKED for this long before a fresh retry window
#: opens. A refusal that does not clear in 30 s is usually a CLOSED MARKET, and a
#: weekend is minutes-to-days away from being closable rather than never -- so the
#: rule keeps its position in the queue instead of abandoning it, at one loud
#: ``close_gave_up`` line per window rather than a refusal storm.
CLOSE_RETRY_PARK_SECONDS = 60.0
#: The opening burst of a retry window: the first few refusals are re-sent on
#: ``CLOSE_RETRY_FAST_COOLDOWN_SECONDS`` before the cadence settles back to
#: ``RETRY_COOLDOWN_SECONDS``. A refusal is usually transient -- a requote, or a
#: price that moved under a market order -- so a flat 1.5 s cooldown sat on a
#: deal the broker would have taken 300 ms later. MEASURED 2026-09-23 with the
#: fast burst in place: attempts 1-4 land ~0.3 s apart and the rest keep the
#: 1.5 s spacing, so a transience that clears immediately is out ~5x sooner
#: while a hard refusal still stops at the same deadline and park.
CLOSE_RETRY_FAST_ATTEMPTS = 4
CLOSE_RETRY_FAST_COOLDOWN_SECONDS = 0.25
#: How far back each loop rereads RECORDED ticks.
#:
#: WHY THIS EXISTS, MEASURED 2026-09-23 on a live box: ``symbol_info_tick``
#: returns ONE tick, and the loop reached it 10 times a second -- so the guard
#: examined at most 10 of however many ticks arrived, and a level touched and
#: reverted inside a 100 ms gap was invisible. HOW MANY it was blind to is not a
#: fixed number: one reading taken that day counted 37131 EURUSD ticks in 60 s
#: (618.85 tick/s) and another, on the same box 30 min later, counted 282 rows
#: over 60.5 s (~4.7 tick/s, with the stream's head level with the live tick).
#: The rate is bursty and CANNOT be assumed. What is constant is the shape of the
#: hole: one price per poll, whatever the feed is doing.
#:
#: NOTE the ceiling this cannot fix, measured the same day: ``symbol_info_tick``
#: inside Wine costs 334.7 us, i.e. ~2988 polls/s is the absolute limit for a
#: Python poll of MT5 -- and no poll rate buys more than one price per call. So
#: the answer to the missed ticks is not to poll faster (which the API cannot),
#: it is to read the ticks that were RECORDED since the last poll and stop
#: discarding them. The interval stays where it is; the blindness goes.
TICK_SCAN_LOOKBACK_SECONDS = 3.0
#: How far back the scan is ever willing to reach, in the TICK clock. The normal
#: window is ``TICK_SCAN_LOOKBACK_SECONDS`` back from the live tick, but when a
#: pass left its watermark further behind than that (a stalled box, a close-retry
#: burst, a paused sandbox resuming) the window starts from the watermark instead,
#: so the ticks recorded in the gap are walked rather than skipped. This bound is
#: what keeps that catch-up finite.
TICK_SCAN_MAX_LOOKBACK_SECONDS = 60.0
#: A touch that reverted between two polls is REPORTED but does NOT fire: the
#: price is already back inside the level, so acting on it would close at a price
#: the user never asked to be out at. It is throttled so a level being grazed
#: repeatedly cannot flood the event log -- one line per rule per this many
#: seconds, carrying the touch price, how long the touch lasted and how long ago.
NEAR_MISS_COOLDOWN_SECONDS = 5.0
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


def recorded_ticks(symbol, since, limit=20000):
    """Ticks RECORDED at or after ``since``, or ``[]`` when the read fails.

    ``symbol_info_tick`` answers "what is the price now" with ONE tick, so a loop
    that calls it every 100 ms never sees the ticks it skipped -- MEASURED
    2026-09-23 on a live box, 10 ticks a second against a feed that carried
    several a second at its quietest and hundreds at its busiest. This reads the
    recorded stream instead, so the loop can name the tick that actually crossed
    a level.

    CALL ``since`` IN THE TICK CLOCK, NOT IN THIS PROCESS'S. MEASURED 2026-09-23
    on a live box: the terminal stamps ticks in the SERVER's time, which ran
    10799 s (~3 h) ahead of the box, so a window derived from ``time.time()``
    missed the present entirely -- it returned 20000 rows of history, none of
    them newer than three hours ago, and every one of them looked stale against
    the live tick. The scan was silently empty. Callers therefore pass a time
    taken from a tick, and the two sides of the comparison share one clock.

    Degrades to an empty list rather than raising: an older terminal, a symbol
    without tick history, or a transient IPC error must cost the rule its
    precision, never its protection. The caller falls back to the poll sample.
    """
    fn = getattr(mt5, "copy_ticks_from", None)
    if fn is None:
        return []
    try:
        ticks = fn(symbol, int(since), int(limit), mt5.COPY_TICKS_ALL)
    except Exception:
        return []
    if ticks is None or len(ticks) == 0:
        return []
    return ticks


def row_price(row, side):
    """The rule's side of one recorded tick (rows index, they are not objects)."""
    try:
        bid = float(row["bid"])
        ask = float(row["ask"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if side == "bid":
        return bid
    if side == "ask":
        return ask
    if bid <= 0.0 or ask <= 0.0:
        return None
    return (bid + ask) / 2.0


def first_crossing(rows, op, level, side):
    """The FIRST recorded tick that satisfies the level, as (msc, price).

    "First" is the point: the reported trigger is the tick that actually crossed,
    not the poll that happened to notice afterwards.
    """
    for row in rows:
        price = row_price(row, side)
        if price is None:
            continue
        if triggered(price, op, level):
            try:
                msc = int(row["time_msc"])
            except (KeyError, IndexError, TypeError, ValueError):
                msc = 0
            return msc, price
    return None, None


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

    # A refused close is retry state on the RULE, so a watcher that is restarted
    # (or one that comes up after a sandbox pause) picks the retry up instead of
    # forgetting a deal the broker already refused once. Said out loud, because
    # "the guard restarted" and "the guard restarted mid-retry" are different
    # things to be told.
    resuming = [
        {"rule_id": r.get("id"), "symbol": r.get("symbol"),
         "attempts": int(r.get("pending_attempts") or 0)}
        for r in read_rules(args.rules)
        if r.get("pending_since")
    ]
    if resuming:
        append_jsonl(args.events, {
            "event": "close_retry_resumed", "ts": time.time(), "rules": resuming,
        })
        note(f"resuming {len(resuming)} refused close(s): {resuming}")

    polls = 0
    exit_reason = "rules_satisfied"
    prices = {}
    #: Symbol -> the moment it stopped pricing; report_pending tracks the ones
    #: already reported, so the event log gets one line per outage, not one per
    #: 100 ms poll.
    unpriced_since = {}
    unpriced_reported = set()
    ever_priced = set()
    #: Symbol -> the ``time_msc`` of the last recorded tick already examined. The
    #: scan is bounded by this rather than by the poll, so a tick is counted once
    #: however many polls land inside the same second, and a slow loop (a stalled
    #: box, a retry burst) still walks every tick it missed instead of skipping
    #: them. ``ticks_seen`` is the running count, which is what lets the status
    #: answer "is this guard actually looking, or merely alive".
    scanned_to_msc = {}
    ticks_seen = {}
    #: Rule id -> when its last near-miss line was written, so a level being
    #: grazed by a fast market writes one line, not one per loop. The matching
    #: ``near_miss_last`` is what the state file publishes, so a caller can see
    #: the touch the guard saw and chose not to act on.
    near_miss_reported = {}
    near_miss_last = {}
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
        #: Symbol -> the recorded ticks this PASS read, so several rules on one
        #: symbol all measure their level against the same ticks instead of each
        #: consuming them in turn. Cleared every pass; the watermark that bounds
        #: the read lives on in ``scanned_to_msc``.
        scan_fresh = {}
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

            # ---- every tick, not just the one we happened to poll ---------------
            # ``price`` above is ONE tick: the feed records several a second and
            # this loop reaches it 10 times a second, so whatever arrived in
            # between was never looked at. Read what was recorded since the last
            # look so the guard can (a) name the tick that actually crossed when
            # it fires, and (b) say out loud when a level was TOUCHED and is
            # already back inside.
            #
            # DELIBERATELY NOT A NEW TRIGGER. A touch that reverted is reported,
            # never acted on: the price is back inside the level, so closing now
            # would fill at a price the caller never asked to be out at. Only the
            # LIVE tick fires, exactly as before -- what changes is that the guard
            # is no longer blind, and its answer is evidence instead of a sample.
            live_hit = triggered(price, op, level)
            scan_msc = None
            scan_price = None
            # THE SCAN RUNS ON THE TICK CLOCK, NOT THIS PROCESS'S. MEASURED
            # 2026-09-23 on a live box: the terminal stamps ticks in the SERVER's
            # time, which ran 10799 s (~3 h) AHEAD of the box. A window built from
            # ``time.time()`` therefore never reached the present -- the first
            # live run of this scan asked for "the last 3 s" and was answered with
            # 20000 rows, none of them newer than three hours ago, so every one of
            # them read as stale against the live tick and ``fresh`` was ALWAYS
            # empty. The scan ran, counted nothing, and looked exactly like a
            # quiet market. Anchoring both the window and the ages on
            # ``tick_msc`` -- the live tick's OWN stamp -- puts the two sides of
            # the comparison in one clock, with nothing to calibrate. Whether the
            # terminal's clock is right is not this code's business; only that it
            # is the SAME clock.
            #
            # ``or int(now * 1000)`` matters: a tick without a millisecond stamp
            # (an older terminal, a stub) would otherwise floor the scan at 0 and
            # the very next pass would treat the whole lookback window as ticks
            # this rule had just seen -- inventing a crossing that happened before
            # the rule existed. Absent a stamp, this process's clock is the only
            # one in evidence and "now" is the honest floor.
            tick_msc = int(getattr(tick, "time_msc", 0) or 0) or int(now * 1000)
            if symbol not in scanned_to_msc:
                # Arm the scan at the tick already visible: ticks recorded BEFORE
                # the rule existed are not this rule's crossing, and backfilling
                # them would invent a touch that never happened while armed.
                scanned_to_msc[symbol] = tick_msc
                fresh = []
            elif symbol in scan_fresh:
                # WHAT THIS PASS ALREADY READ, for every other rule on the same
                # symbol. MEASURED live 2026-09-23, and the bug it fixes: two
                # rules on EURUSD, the first (a far level that never fires) read
                # the recorded stream and advanced the per-symbol watermark to the
                # crossing tick, so by the time the SECOND rule was evaluated --
                # the one that actually fired -- its own read returned nothing
                # newer than the watermark. It closed the position correctly and
                # named no crossing, which is precisely the evidence this scan
                # exists to produce. The ticks belong to the SYMBOL for this pass,
                # not to whichever rule happened to be listed first, so they are
                # read once and every rule measures its own level against them.
                fresh = scan_fresh[symbol]
            else:
                floor = int(scanned_to_msc.get(symbol) or 0)
                # Look back FROM THE LIVE TICK, and fall back to the watermark
                # when the previous pass left it further behind than the lookback:
                # the ticks in that gap are precisely the ones a stalled loop
                # missed, which is the case this scan exists for. Bounded, so a
                # long stall cannot ask for an unbounded window.
                scan_since = tick_msc / 1000.0 - TICK_SCAN_LOOKBACK_SECONDS
                if floor:
                    scan_since = min(scan_since, floor / 1000.0)
                scan_since = max(
                    scan_since, tick_msc / 1000.0 - TICK_SCAN_MAX_LOOKBACK_SECONDS
                )
                rows = recorded_ticks(symbol, scan_since)
                fresh = []
                for row in rows:
                    try:
                        row_msc = int(row["time_msc"])
                    except (KeyError, IndexError, TypeError, ValueError):
                        continue
                    if row_msc > floor:
                        fresh.append(row)
                if fresh:
                    scanned_to_msc[symbol] = max(
                        int(r["time_msc"]) for r in fresh
                    )
                    ticks_seen[symbol] = ticks_seen.get(symbol, 0) + len(fresh)
                scan_fresh[symbol] = fresh
            scan_msc, scan_price = first_crossing(fresh, op, level, used_side)

            #: A rule can be in one of two states:
            #:   * armed and waiting -- it acts only while the level is touched;
            #:   * holding a DECIDED close -- the level was touched and the
            #:     broker refused the deal. That decision stands, so the retry
            #:     does NOT depend on the price still sitting beyond the level.
            #:     MEASURED failure it fixes: a close refused on the touching
            #:     tick, the price then ticking back inside, and the retry
            #:     stopping -- a position left open with a live guard, an armed
            #:     rule, and nothing left to report.
            pending = bool(rule.get("pending_since"))
            if not pending and not live_hit:
                if scan_msc is not None:
                    # TOUCHED AND ALREADY BACK INSIDE. This is the tick the old
                    # loop never saw, and it is the one case where seeing it does
                    # NOT mean acting on it: the price is no longer beyond the
                    # level, so a close now would fill somewhere the caller never
                    # asked to be out at. Report it, do not fire. Throttled, so a
                    # level being grazed by a fast market writes one line rather
                    # than one per 100 ms loop.
                    rule_id = str(rule.get("id"))
                    if now - float(near_miss_reported.get(rule_id) or 0.0) >= \
                            NEAR_MISS_COOLDOWN_SECONDS:
                        near_miss_reported[rule_id] = now
                        # Measured against the LIVE TICK, in the tick clock, for
                        # the same reason the scan window is: ``now`` is this
                        # process's clock and can sit hours from the tick's.
                        age_ms = round(max(0.0, float(tick_msc - scan_msc)), 1)
                        near_miss_last[rule_id] = {
                            "symbol": symbol, "op": op, "level": level,
                            "side": used_side, "touch_price": scan_price,
                            "touch_age_ms": age_ms, "seen_at": now,
                            "back_inside_at": price,
                        }
                        append_jsonl(args.events, {
                            "event": "level_touched_then_reverted", "ts": now,
                            "rule_id": rule_id, "symbol": symbol, "op": op,
                            "level": level, "side": used_side,
                            "touch_price": scan_price, "touch_age_ms": age_ms,
                            "price_now": price,
                            "ticks_scanned": int(ticks_seen.get(symbol, 0)),
                            "detail": (
                                f"{symbol} {op} {level} was touched at "
                                f"{scan_price} ({round(age_ms)} ms ago) and is back "
                                f"at {price}. NOT closing: the level is no longer "
                                "touched, and a close now would fill at a price "
                                "the caller did not ask to be out at. The rule is "
                                "still armed."
                            ),
                        })
                        note(
                            f"{rule_id} NEAR MISS {symbol} {op} {level}: touched "
                            f"{scan_price} {round(age_ms)} ms ago, back at {price} "
                            "-- reported, NOT closed"
                        )
                continue
            # A rule that has just given up is PARKED, pending or not. The park is
            # what stops a broker that refuses every deal from being retried in a
            # tight loop, and it is what reopens the window later -- usually
            # because the market that was closed has opened. Checking it only for
            # pending rules left a hole: the give-up CLEARS the pending state, so
            # the very next poll re-armed a fresh window and the refusal storm was
            # back. (Caught by the behavioural test, 2026-09-23: attempts resumed
            # 1.6 s after the give-up instead of 60 s.)
            if now < float(rule.get("parked_until") or 0.0):
                continue
            # The opening burst of a retry window is FAST, then the cadence
            # settles back to RETRY_COOLDOWN_SECONDS. A refusal is usually
            # transient -- a requote, or a price that moved under a market order
            # -- and a flat 1.5 s cooldown sat on a deal the broker would have
            # taken 300 ms later. The burst is bounded by
            # CLOSE_RETRY_FAST_ATTEMPTS, so a HARD refusal (a closed market, an
            # impossible volume) loses nothing: it still stops at the same
            # deadline and parks for the same 60 s.
            tried = int(rule.get("pending_attempts") or 0)
            cooldown = (
                CLOSE_RETRY_FAST_COOLDOWN_SECONDS
                if 0 < tried < CLOSE_RETRY_FAST_ATTEMPTS
                else RETRY_COOLDOWN_SECONDS
            )
            if now - float(rule.get("last_attempt") or 0.0) < cooldown:
                continue
            rule["last_attempt"] = now
            trigger_ts = time.time()
            positions = matching_positions(rule)
            results = close_positions(rule, positions, args.deviation, args.magic)
            close_ts = time.time()
            ok = all(r.get("ok") for r in results) if results else True
            #: Which close this is: 1 is the trigger itself, 2+ are retries of a
            #: deal the broker had already refused. ``retry_window`` counts the
            #: give-up windows before this one, so attempt 1 of window 2 says so.
            attempt = int(rule.get("pending_attempts") or 0) + 1
            window_started = float(rule.get("pending_since") or now)
            retry_window = int(rule.get("pending_windows") or 0) + 1
            #: The tick that ACTUALLY crossed, named from the recorded stream
            #: rather than inferred from the poll that noticed. ``trigger_price``
            #: stays the price at the decision (it is what the order used); these
            #: say how long the level had really been crossed, which is the
            #: difference between "we saw it" and "we sampled it".
            crossing = (
                {
                    "crossing_msc": scan_msc,
                    "crossing_price": scan_price,
                    "crossing_age_ms": round(max(0.0, float(tick_msc - scan_msc)), 1),
                }
                if scan_msc is not None
                else {}
            )
            append_jsonl(args.events, {
                "event": "fired" if ok else "close_failed",
                "rule_id": rule.get("id"), "symbol": symbol, "op": op, "level": level,
                "side": used_side, "trigger_price": price,
                "bid": float(tick.bid), "ask": float(tick.ask),
                "tick_time": int(getattr(tick, "time", 0) or 0),
                "trigger_ts": trigger_ts, "close_ts": close_ts,
                "latency_ms": round((close_ts - trigger_ts) * 1000.0, 1),
                "positions_matched": len(positions), "results": results, "polls": polls,
                "attempt": attempt, "retry": pending, "retry_window": retry_window,
                "pending_seconds": round(now - window_started, 1) if pending else 0.0,
                "ticks_scanned": int(ticks_seen.get(symbol, 0)),
                **crossing,
            })
            if ok:
                note(
                    f"{rule.get('id')} FIRED {symbol} {op} {level} at {price} "
                    f"({used_side}) -- {len(positions)} position(s), "
                    f"{round((close_ts - trigger_ts) * 1000.0, 1)} ms to fill"
                    + (
                        f" (retry {attempt - 1} of {attempt - 1} after "
                        f"{round(now - window_started, 1)} s: the broker had refused)"
                        if pending
                        else ""
                    )
                )
                # The close is CONFIRMED, so the retry state goes with it. Left
                # behind, it would keep the rule retrying a deal that is done.
                for key in (
                    "pending_since", "pending_attempts", "pending_deadline",
                    "parked_until", "gave_up_at", "gave_up_attempts",
                    "gave_up_retcodes",
                ):
                    if rule.pop(key, None) is not None:
                        changed = True
            else:
                # The refusal is remembered on the RULE, and the rules file is
                # rewritten below, so the retry survives both the next loop pass
                # (which re-reads the file) and a restart of the watcher.
                if not pending:
                    rule["pending_since"] = now
                    rule["pending_deadline"] = now + CLOSE_RETRY_DEADLINE_SECONDS
                rule["pending_attempts"] = attempt
                changed = True
                deadline = float(
                    rule.get("pending_deadline")
                    or (now + CLOSE_RETRY_DEADLINE_SECONDS)
                )
                retcodes = sorted(
                    {r.get("retcode") for r in results if r.get("retcode") is not None}
                )
                note(
                    f"{rule.get('id')} close FAILED at {price} "
                    f"(attempt {attempt}, {round(now - window_started, 1)} s): {results}"
                )
                if attempt >= CLOSE_RETRY_MAX_ATTEMPTS or now >= deadline:
                    # GIVING UP IS AN EVENT, NOT A SILENCE. The position is still
                    # open, so the caller is told in one line with the broker's own
                    # codes and the tickets -- the failure that used to be a
                    # resume-forever retry or an abandoned position, depending on
                    # whether the price stayed beyond the level.
                    still_open = [int(p.ticket) for p in matching_positions(rule)]
                    rule["pending_windows"] = retry_window
                    rule["gave_up_at"] = now
                    rule["gave_up_attempts"] = attempt
                    rule["gave_up_retcodes"] = retcodes
                    rule["parked_until"] = now + CLOSE_RETRY_PARK_SECONDS
                    for key in ("pending_since", "pending_attempts", "pending_deadline"):
                        rule.pop(key, None)
                    append_jsonl(args.events, {
                        "event": "close_gave_up", "ts": now,
                        "rule_id": rule.get("id"), "symbol": symbol,
                        "op": op, "level": level, "trigger_price": price,
                        "attempts": attempt, "retry_window": retry_window,
                        "retry_seconds": round(now - window_started, 1),
                        "retcodes": retcodes, "last_results": results,
                        "still_open_tickets": still_open,
                        "retry_in_s": CLOSE_RETRY_PARK_SECONDS,
                        "detail": (
                            f"the broker refused this close {attempt} time(s) over "
                            f"{round(now - window_started, 1)} s and the position(s) "
                            f"{still_open or 'matched by this rule'} are still open. "
                            "The rule stays armed and opens a fresh retry window "
                            f"after {int(CLOSE_RETRY_PARK_SECONDS)} s (a closed market "
                            "is the usual cause, and that close works when it reopens)."
                        ),
                    })
                    note(
                        f"{rule.get('id')} GAVE UP closing {symbol} after {attempt} "
                        f"attempts / {round(now - window_started, 1)} s -- still open: "
                        f"{still_open}, broker said {retcodes}. Still armed; retries "
                        f"in {int(CLOSE_RETRY_PARK_SECONDS)} s."
                    )
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
        armed_now = read_rules(args.rules)
        write_json(args.state, {
            "pid": os.getpid(), "started_at": started, "status": "running",
            "heartbeat": time.time(), "polls": polls, "interval_ms": args.interval_ms,
            "max_seconds": int(args.max_seconds),
            "prices": prices, "rules": len(armed_now),
            "unpriceable": {s: round(t, 1) for s, t in unpriced_since.items()},
            # A close that is being retried is the difference between "armed" and
            # "armed and already trying to get out", so it is in the live state.
            "retrying": {
                str(r.get("id")): int(r.get("pending_attempts") or 0)
                for r in armed_now if r.get("pending_since")
            },
            "gave_up": [
                str(r.get("id")) for r in armed_now if r.get("gave_up_at")
            ],
            # PROOF THE GUARD IS LOOKING, not merely alive. ``ticks_scanned`` is
            # how many recorded ticks this symbol has had examined, and
            # ``near_miss`` is the last level that was touched and already back
            # inside -- the tick the old 10 Hz sample could not see. Both are in
            # the state so "is it watching?" is answered with a count.
            "ticks_scanned": dict(ticks_seen),
            "near_miss": dict(near_miss_last),
        })
        time.sleep(max(0.01, args.interval_ms / 1000.0))

    # The rules at exit, read once: a watcher can stop with retry state still on
    # them (a crash, a stop request, a re-arm), and that state is the difference
    # between "the guard is gone" and "the guard is gone mid-retry".
    final_rules = read_rules(args.rules)
    write_json(args.state, {
        "pid": os.getpid(), "started_at": started, "status": "finished",
        "exit_reason": exit_reason, "finished_at": time.time(),
        "heartbeat": time.time(), "polls": polls,
        "rules": len(final_rules),
        "retrying": {
            str(r.get("id")): int(r.get("pending_attempts") or 0)
            for r in final_rules if r.get("pending_since")
        },
        "gave_up": [str(r.get("id")) for r in final_rules if r.get("gave_up_at")],
        "ticks_scanned": dict(ticks_seen),
        "near_miss": dict(near_miss_last),
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


def _guard_event_lines() -> list[str]:
    """The event log as raw lines (UTF-8, unlike the UTF-16 Wine logs).

    Read as text rather than through ``_log_line_count`` on purpose: that helper
    exists for the UTF-16LE terminal logs, and counting the events file through
    it would decode a UTF-8 file with the wrong codec and shift every mark.
    """
    try:
        return GUARD_EVENTS_FILE.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return []


def _guard_events_after(mark: int) -> list[dict[str, Any]]:
    """Every event written after the first ``mark`` lines of the event log."""
    out: list[dict[str, Any]] = []
    for line in _guard_event_lines()[max(0, int(mark)):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


#: Events worth ending a blocking wait for, worst first. A wait exists to catch
#: the thing the caller is not there to see, so anything that changes what the
#: caller must do next belongs here.
_GUARD_WATCH_EVENTS = (
    "fired",
    "close_failed",
    "close_gave_up",
    "close_retry_resumed",
    "level_touched_then_reverted",
    "rule_unpriceable",
    "rule_priceable",
    "watcher_stop",
)


def _guard_wait(seconds: float, poll_seconds: float) -> dict[str, Any]:
    """Block, sampling the guard, until something happens or the budget runs out.

    WHY THIS EXISTS: "the guard is monitoring" is a CLAIM until something
    observes it. A status call that returns instantly says what is true now and
    nothing about whether anything happens next, so a caller reports
    "monitoring" and moves on -- and the exit is then only discovered by
    accident, on some later turn, by which time the caller has been describing
    a guard as protection without having seen it do anything.

    This call OBSERVES. It samples the guard every ``poll_seconds`` and returns
    the moment the event log grows (a fire, a refused close, a resumed retry, a
    level touched and reverted, the watcher stopping) -- or when the budget runs
    out, and it says which of the two it was, so "nothing happened" is never
    reported as "something happened".

    Bounded at ``GUARD_MAX_WAIT_SECONDS`` deliberately. A caller wants real-time
    watching, not a sandbox command held open until the command timeout kills
    it mid-wait: the cap is returned in the answer, so the caller polls again
    instead of the wait being cut off with nothing to show.
    """
    budget = max(0.0, min(float(seconds or 0.0), GUARD_MAX_WAIT_SECONDS))
    poll = max(0.2, float(poll_seconds or 1.0))
    started = time.time()
    deadline = started + budget
    mark = len(_guard_event_lines())
    state = _read_guard_state()
    live = _guard_is_live(state)
    samples = 0
    observed: list[dict[str, Any]] = []
    while True:
        remaining = deadline - time.time()
        if remaining <= 0.0:
            break
        time.sleep(min(poll, remaining))
        samples += 1
        observed = _guard_events_after(mark)
        if observed:
            break
        state = _read_guard_state()
        was_live = live
        live = _guard_is_live(state)
        if was_live and not live:
            # The watcher died while this call was watching it. That is an
            # OBSERVATION, not a timeout: the armed levels are now watched by
            # nobody, and a wait that reported "timed out, nothing happened"
            # would be exactly the silence this guard work exists to remove.
            observed = [{
                "event": "watcher_stop",
                "ts": time.time(),
                "exit_reason": (state or {}).get("exit_reason"),
                "detail": "the watcher stopped while this call was watching it",
            }]
            break
    watched_seconds = round(time.time() - started, 2)
    return {
        "observed": observed,
        "observed_event": observed[-1].get("event") if observed else None,
        "waited_s": watched_seconds,
        "samples": samples,
        "poll_seconds": poll,
        "timed_out": not observed,
        "capped_at_s": GUARD_MAX_WAIT_SECONDS,
        "running": _guard_is_live(state),
        "state": state,
        "note": (
            f"nothing happened in {watched_seconds} s of watching ({samples} "
            "samples): no fire, no refused close, no near miss, and the watcher "
            "is still up. This is an observation, not a claim."
            if not observed
            else (
                f"observed '{observed[-1].get('event')}' after "
                f"{watched_seconds} s ({samples} samples)."
            )
        ),
    }


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
                # The refusal does NOT end the attempt: the close is retry state on
                # the rule, the rule is not consumed, and the watcher keeps trying
                # -- so this must not read as "the guard tried once and stopped".
                state_now = _read_guard_state()
                payload["retrying"] = _guard_retry_state()
                payload["guard_live"] = _guard_is_live(state_now)
                payload["message"] = (
                    "The level was already satisfied, the guard fired on the first "
                    "tick, and the CLOSE WAS REJECTED -- the position is still "
                    "open. Read 'close_failed' for the broker's answer. The rule is "
                    "NOT consumed and the watcher retries the close every "
                    f"{GUARD_CLOSE_RETRY_COOLDOWN_SECONDS} s for up to "
                    f"{int(GUARD_CLOSE_RETRY_DEADLINE_SECONDS)} s; poll guard "
                    "action='events' for 'fired' (it got out) or 'close_gave_up' "
                    "(it could not)."
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

        # LIVE, and a close of it was ALREADY refused. The watcher is up and
        # retrying, which is why "armed" alone would be the wrong answer here:
        # the caller asked to be OUT, and is not out. The retry is reported on the
        # same fields the status action uses, so arm and status never disagree.
        retry = _guard_retry_state()
        payload["retrying"] = retry["retrying"]
        payload["gave_up"] = retry["gave_up"]
        if retry["gave_up"] or retry["retrying"]:
            payload["ok"] = False
            payload["alert"] = (
                "close_gave_up" if retry["gave_up"] else "close_retrying"
            )
            payload["recovery"] = (
                "read guard events, then check action='positions'"
                if retry["gave_up"]
                else "nothing to do yet -- the watcher retries on its own"
            )
            payload["warning"] = (
                "the guard is live but has NOT got the position out: "
                + "; ".join(
                    (
                        f"{g['rule_id']} on {g['symbol']} refused {g['attempts']} "
                        f"time(s) (retcodes {g['retcodes']})"
                    )
                    for g in retry["gave_up"]
                )
                + "; ".join(
                    (
                        f"{r['rule_id']} on {r['symbol']} refused {r['attempts']} "
                        f"time(s) so far, next retry in {r['retry_in_s']} s"
                    )
                    for r in retry["retrying"]
                )
                + ". Poll action='guard', guard_action='events' for 'fired' (it got "
                "out) or 'close_gave_up' (it could not), and do not read this level "
                "as covered until the position is gone."
            )
            return emit(payload, text="guard armed, but the close was refused")
        return emit(payload, text=f"guard armed ({len(payload['rules'])} rule(s))")

    if subcommand in ("status", "ensure"):
        state = _read_guard_state()
        live = _guard_is_live(state)
        rules = _read_guard_rules()
        # A WAIT IS DONE FIRST, so everything below is computed from what the
        # guard looks like AFTER it, not before. Watching and then reporting the
        # pre-watch snapshot is the failure mode this ordering avoids: the whole
        # point is that the answer describes the thing that happened.
        watched: dict[str, Any] | None = None
        if subcommand == "status" and float(getattr(args, "wait_seconds", 0.0) or 0.0) > 0:
            watched = _guard_wait(
                float(getattr(args, "wait_seconds", 0.0) or 0.0),
                float(getattr(args, "poll_seconds", 1.0) or 1.0),
            )
            state = watched["state"]
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
        retry = _guard_retry_state(rules)
        alarm = None
        if rules and not live:
            alarm = "guard_not_running"
        elif live and retry["gave_up"]:
            # The guard IS running and could NOT get the position out. Reporting
            # that as a healthy status is the same lie as a dead-while-armed
            # guard reported as fine: a caller who asked to be out is still in.
            alarm = "close_gave_up"
        elif live and retry["retrying"]:
            alarm = "close_retrying"
        elif live and unpriceable:
            alarm = "rule_unpriceable"
        payload = {
            # A refused close is not a healthy status even when the watcher is
            # retrying it: the caller asked to be out and is still in. Everything
            # that is not ok carries the field that says why, and how to recover.
            "ok": alarm is None,
            "running": live,
            "state": state,
            "rules_armed": len(rules),
            "rules": rules,
            "retrying": retry["retrying"],
            "gave_up": retry["gave_up"],
            "interval_ms": (state or {}).get("interval_ms"),
            "polls": (state or {}).get("polls"),
            "prices": (state or {}).get("prices") or {},
            # Every recorded tick the watcher has LOOKED AT, per symbol. This is
            # the difference between "the watcher is alive" and "the watcher is
            # watching": ``polls`` counts loop passes, which is one price sample
            # each, while this counts the recorded ticks behind them.
            "ticks_scanned": (state or {}).get("ticks_scanned") or {},
            # The last level that was TOUCHED and was already back inside when it
            # was looked at. Reported, never acted on -- see the watcher.
            "near_miss": (state or {}).get("near_miss") or {},
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
            payload["recovery"] = {
                "close_gave_up": (
                    "read guard events for close_gave_up, check positions, and close "
                    "the position by hand (action='close') or fix what the broker "
                    "refused; the rule retries on its own"
                ),
                "close_retrying": (
                    "nothing to do yet -- the watcher retries on its own; poll "
                    "guard status (or events) for fired, or for close_gave_up if the "
                    "broker keeps refusing"
                ),
            }.get(alarm, "guard action='ensure' (or arm again)")
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
            elif alarm == "close_gave_up":
                payload["warning"] = (
                    "the guard is running but could NOT close: "
                    + "; ".join(
                        f"{g['rule_id']} on {g['symbol']} refused "
                        f"{g['attempts']} time(s) (retcodes {g['retcodes']}) and "
                        f"{g['gave_up_s_ago']} s ago"
                        for g in retry["gave_up"]
                    )
                    + ". The position(s) are STILL OPEN. The rule stays armed and "
                    f"opens a fresh retry window in {retry['gave_up'][0]['next_window_in_s']} s "
                    "(a closed market is the usual cause); read action='guard' "
                    "guard_action='events' for close_gave_up, and check "
                    "action='positions' before believing this level is covered."
                )
            elif alarm == "close_retrying":
                payload["warning"] = (
                    "the guard fired and the broker REFUSED the close: "
                    + "; ".join(
                        f"{r['rule_id']} on {r['symbol']}, {r['attempts']} attempt(s) "
                        f"over {r['trying_for_s']} s, next in {r['retry_in_s']} s"
                        for r in retry["retrying"]
                    )
                    + ". It keeps retrying until the broker accepts or "
                    f"{int(GUARD_CLOSE_RETRY_DEADLINE_SECONDS)} s passes, then logs "
                    "close_gave_up. The position(s) are not out yet."
                )
            else:
                payload["warning"] = (
                    "the watcher is running but cannot price "
                    + ", ".join(sorted(unpriceable))
                    + f" (dark for {max(unpriceable.values())} s at last beat). A "
                    "rule on an unpriced symbol can never fire."
                )
        if watched is not None:
            payload["watched"] = watched
            # An observed fire is not a failure and an observed timeout is not
            # one either -- but a refused close or a watcher that died WHILE
            # being watched are, and the caller must not be told "ok" because
            # the earlier snapshot happened to look healthy.
            if watched["observed_event"] in (
                "close_failed", "close_gave_up", "watcher_stop",
            ):
                payload["ok"] = False
                payload.setdefault("alert", watched["observed_event"])
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
        # The last fire is read from the WHOLE log, not from the returned window.
        # MEASURED 2026-09-23 (live): after a guard fired and closed its position,
        # ``guard events --lines 1`` answered ``last_latency_ms: null`` because the
        # single newest line was the watcher_stop that followed the fire. That
        # number is the headline answer to "why was the close late", so it is the
        # last fire's, never the window's.
        all_fired = [e for e in _guard_events(500) if e.get("event") == "fired"]
        return emit(
            {
                "ok": True,
                "count": len(events),
                "events": events,
                "last_latency_ms": (
                    all_fired[-1].get("latency_ms") if all_fired
                    else (fired[-1].get("latency_ms") if fired else None)
                ),
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
    p.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help=(
            "instead of answering instantly, WATCH the install for this many "
            "seconds and return the moment it moves: the stage changes, the "
            "installer writes more output, or the installer process exits. A "
            f"finished install returns immediately without waiting. Capped at "
            f"{int(WATCH_MAX_WAIT_SECONDS)} s. A timeout is a normal answer and is "
            "reported as one, in 'watched', so 'still installing' is an "
            "observation rather than a claim."
        ),
    )
    p.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="status --wait-seconds: how often to sample the install (default 2 s)",
    )
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

    p = sub.add_parser(
        "watch",
        help="watch a live trade: positions + live prices + guard, in one frame",
    )
    # Repeatable and optional: with no symbol the symbols of the OPEN POSITIONS
    # are watched, which is the set that carries risk. Naming symbols is for a
    # market the caller cares about but has not traded yet.
    p.add_argument("--symbol", action="append", default=[])
    p.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help=(
            "block and sample for this many seconds, returning the moment "
            "something happens (a rule fires, a close is refused, a level is "
            "touched and reverted, the watcher stops, the set of open positions "
            f"changes). Capped at {int(WATCH_MAX_WAIT_SECONDS)} s. Use it to "
            "actually observe a live trade instead of reporting that it is being "
            "'monitored'."
        ),
    )
    p.add_argument(
        "--poll-seconds",
        type=float,
        default=1.0,
        help="watch --wait-seconds: how often to sample (default 1 s)",
    )
    p.add_argument("--lines", type=int, default=20, help="guard events to return")
    p.add_argument(
        "--session",
        default="",
        help=(
            "continue ONE observation across calls. Every watch carrying the same "
            "name folds its samples into a ledger on disk and returns "
            "session.price_path_total (the whole session's high/low/drift, not this "
            "call's) plus session.since_last_call. Watching a long trade in "
            f"{int(WATCH_MAX_WAIT_SECONDS)}-second calls without this reports a "
            "different trade every call; with it, the calls are one timeline and "
            "you can think between them."
        ),
    )
    p.set_defaults(func=cmd_watch)

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

    p = sub.add_parser(
        "order",
        help="send an order: at the market, or RESTING at a price, sized in lots or in money",
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell", "long", "short"])
    p.add_argument(
        "--volume",
        type=float,
        default=None,
        help=(
            "lots to trade. Omit it when passing --risk-money/--risk-pct, which "
            "derive the lots from the stop distance instead"
        ),
    )
    p.add_argument(
        "--entry-type",
        default="market",
        choices=["market", "limit", "stop"],
        help=(
            "market = fill now at the current price. limit = rest at --price and "
            "fill only BETTER than the market (buy below, sell above). stop = rest "
            "at --price and fill only when the market BREAKS THROUGH it (buy above, "
            "sell below). A limit/stop order holds no position until it fills"
        ),
    )
    p.add_argument(
        "--price",
        type=float,
        default=None,
        help=(
            "the entry price for --entry-type limit/stop. Not allowed with "
            "market, which fills at the current price"
        ),
    )
    p.add_argument(
        "--risk-money",
        type=float,
        default=None,
        help=(
            "size the order so that a stop-out costs this much in account "
            "currency. Needs --sl. Prefer this over --volume when the instruction "
            "is 'risk $100 on this'"
        ),
    )
    p.add_argument(
        "--risk-pct",
        type=float,
        default=None,
        help="as --risk-money, but as a percentage of account equity. Needs --sl",
    )
    p.add_argument("--sl", type=float, default=None)
    p.add_argument("--tp", type=float, default=None)
    p.add_argument("--deviation", type=int, default=20)
    p.add_argument("--magic", type=int, default=20240919)
    p.add_argument("--comment", default="powerx-mt5")
    p.set_defaults(func=cmd_order)

    p = sub.add_parser(
        "cancel",
        help="remove a pending order that has not triggered yet",
    )
    p.add_argument("--ticket", type=int, default=None, help="the pending order ticket")
    p.add_argument("--all", action="store_true", help="remove every pending order")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser(
        "split",
        help="open ONE idea as N equal positions at one price (split trading)",
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell", "long", "short"])
    p.add_argument(
        "--volume",
        type=float,
        required=True,
        help="TOTAL lots for the idea, divided across the tickets",
    )
    p.add_argument(
        "--splits",
        type=int,
        default=10,
        help=(
            "how many positions to open (2..50, default 10). One idea risking $100 "
            "becomes 10 tickets of 0.10 rather than one of 1.00: same direction, "
            "same stop, same TOTAL risk, but the exits stop being all-or-nothing"
        ),
    )
    p.add_argument(
        "--group",
        default="",
        help=(
            "label the tickets so they can be closed as a set later "
            "(close --group NAME --count 3)"
        ),
    )
    p.add_argument("--sl", type=float, default=None)
    p.add_argument("--tp", type=float, default=None)
    p.add_argument("--deviation", type=int, default=20)
    p.add_argument("--magic", type=int, default=20240919)
    p.add_argument("--comment", default="powerx-split")
    p.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="stop sending tickets after the first rejection (default: try them all)",
    )
    p.add_argument(
        "--check-cost",
        action="store_true",
        help="report the per-deal cost of N tickets vs one position",
    )
    p.set_defaults(func=cmd_split)

    p = sub.add_parser(
        "close",
        help="close a position, or PART of a split by --group",
    )
    p.add_argument("--ticket", type=int, default=None)
    p.add_argument("--volume", type=float, default=None)
    p.add_argument(
        "--group",
        default="",
        help=(
            "close tickets of a split by its group label instead of one ticket. "
            "Combine with --count to take only part of it off"
        ),
    )
    p.add_argument(
        "--count",
        type=int,
        default=0,
        help="close only this many of the group (oldest comment first); 0 = all of it",
    )
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
    p.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help=(
            "status only: instead of answering instantly, WATCH the guard for "
            "this many seconds and return the moment something happens (a fire, "
            "a refused close, a resumed retry, a level touched and reverted, the "
            f"watcher stopping). Capped at {int(GUARD_MAX_WAIT_SECONDS)} s. "
            "Timing out is a normal answer and is reported as one, so "
            "'monitoring' is an observation rather than a claim."
        ),
    )
    p.add_argument(
        "--poll-seconds",
        type=float,
        default=1.0,
        help="status --wait-seconds: how often to sample the guard (default 1 s)",
    )
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
        "history", "symbol", "symbols", "order", "split", "close", "close_all",
        # `cancel` sends TRADE_ACTION_REMOVE to the terminal, so it is a Wine
        # action like the rest of the trade path.
        "cancel",
        "run",
        # `watch` samples ticks and positions every poll, so it MUST run under
        # Wine: on the Linux python each sample would be a fresh re-exec into
        # Wine (seconds each) and a 1 Hz watch would sample the market at
        # roughly one frame per call instead of one per second. Under Wine a
        # tick read is ~335 us, which is what makes the price PATH (not just the
        # latest price) affordable inside one call.
        "watch",
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
    # ONE SANDBOX PER INVOCATION. These paths used to be fixed
    # (``C:\mt5tmp\run.bat`` + ``C:\mt5tmp\stdout.txt``), so two bridge actions
    # running at the same time clobbered each other. MEASURED 2026-09-24 on a
    # live box: an ``order`` issued while a ``watch`` was sampling made the watch
    # report the ORDER's JSON as its own result -- the order deleted the file the
    # watch was about to read, and the watch's answer was lost with it.
    #
    # That is not a corner case any more. The whole point of ``watch`` is to sit
    # on a live trade while the caller acts on it, so ``watch`` + ``order`` /
    # ``close`` / ``positions`` overlapping is the intended usage, not an
    # accident. A private directory per invocation makes the overlap harmless.
    tmp_dir = drive_c / "mt5tmp" / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    win_tmp = f"C:\\mt5tmp\\{tmp_dir.name}"

    out_linux = tmp_dir / "stdout.txt"
    if out_linux.exists():
        out_linux.unlink()

    # Quote every argument for cmd.exe (double quotes, escape inner quotes).
    win_args = " ".join('"' + a.replace('"', '\\"') + '"' for a in argv)
    bat = tmp_dir / "run.bat"
    bat.write_text(
        "@echo off\r\n"
        # THE ROOTS ARE PASSED THROUGH, and they must be. MEASURED 2026-09-23 on
        # a live box: this child resolved ``Path.home()`` as ``C:\users\user``
        # (Wine's USERPROFILE, not the Linux home), so ``MT5_ROOT`` came out as
        # ``C:\users\user\.mt5`` -- a DIFFERENT, empty directory. Every bridge
        # action therefore read the guard's state and rules from nowhere:
        # ``positions`` answered ``guard: {live: false, rules_armed: 0}`` while a
        # guard was running, armed, and retrying a refused close. The tool turned
        # that into "No tick-level guard is running right now, so a guard rule is
        # NOT currently protecting anything" -- the exact class of lie this guard
        # work exists to remove, told by the one call that reads open risk.
        #
        # The Wine ``Z:`` form is used because it does not depend on the child's
        # current drive; it is the same directory either way.
        f'set "MT5_ROOT={_to_wine_path(MT5_ROOT)}"\r\n'
        f'set "WINE_PREFIX={_to_wine_path(WINE_PREFIX)}"\r\n'
        f'"{_to_wine_path(winpy)}" "{_to_wine_path(Path(__file__).resolve())}" '
        f"{win_args} > \"{win_tmp}\\stdout.txt\" 2>&1\r\n",
        encoding="utf-8",
    )

    env = wine_env()
    env["MT5_UNDER_WINE"] = "1"
    try:
        subprocess.run(
            [wine_bin(), "cmd", "/c", f"{win_tmp}\\run.bat"],
            env=env,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("MT5_WINE_TIMEOUT", "900")),
        )
    except subprocess.TimeoutExpired:
        return fail("timed out waiting for the MT5 bridge inside Wine", code=2)

    try:
        if out_linux.exists():
            # CP1252 is the default console codepage Wine uses; fall back safely.
            raw = out_linux.read_bytes()
            for enc in ("utf-8", "cp1252", "latin-1"):
                try:
                    sys.stdout.write(raw.decode(enc))
                    break
                except UnicodeDecodeError:
                    continue
    finally:
        # The directory is private to this invocation, so it is dead the moment
        # the answer has been read. Left behind, one per bridge call, it would
        # grow the prefix without bound.
        shutil.rmtree(tmp_dir, ignore_errors=True)
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