"""MetaTrader 5 trading sandbox: compile, log-dive, and trade from the command line.

WHY THIS TOOL EXISTS (and why it is shaped like this)
----------------------------------------------------
The user wants the agent to install, compile MQL5, read terminal/Experts logs,
and place trades through a *small* MT5 terminal running under Wine. Running any
of that on the application host would be reckless: the Wine prefix alone is
~500 MB, the MT5 terminal is an amd64 GUI app that needs an X display, and the
whole stack is per-user state. On a Northflank/Render gateway that would burn
the instance's CPU/RAM and could OOM the shared gateway for every user.

So this tool is deliberately a *thin forwarder*. It never imports Wine, never
installs anything locally, and never runs a terminal on the host. It resolves
the already-configured execution sandbox tool (``novita_sandbox`` — the same
plumbing ``arduino_verify`` uses) and runs every MT5 operation inside it:

    1. bootstrap  curl the CLI + installer from the repo into the sandbox
    2. install    Wine + Xvfb + winbind + MT5 + the ``MetaTrader5`` bridge
    3. run        ``python3 ~/.mt5/bin/mt5_cli.py <action> ...`` in the sandbox
    4. parse      the CLI's single-JSON-object stdout back into the answer

Everything the model needs — quotes, candles, positions, order results, compile
errors, log tails — comes back as structured JSON, so the agent can read logs
and fix errors in a loop without a human in the middle.

Safety model
------------
* Trading commands (``order``, ``close``, ``close_all``) are gated behind
  ``MT5_ALLOW_TRADING`` (default **off**). A trading tool that can fire live
  orders by accident is worse than no tool, so live orders require an explicit
  opt-in on the deployment. Read-only actions always work.
* Credentials are passed as environment variables into the sandbox command, are
  never written to the workspace, and are never echoed back in the result.
* Optional ``dry_run`` builds and validates the order request without sending.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext

#: Raw GitHub base for the two sandbox-side scripts. The sandbox has internet
#: access (the Novita tool creates boxes with ``allow_internet_access=True``), so
#: bootstrapping by URL avoids shipping megabytes of tooling in the image.
#:
#: NOTE: this is only the LAST-RESORT source. The sandbox's egress path caches by
#: URL *path* and ignores both query strings and ``Cache-Control: no-cache``, so
#: a branch-name URL can serve a revision that is several pushes old. See
#: ``bootstrap_command`` for the ordered, verified source list.
_RAW_BASE = os.getenv(
    "MT5_SCRIPT_RAW_BASE",
    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts",
)

#: Owner/repo used to resolve ``main`` to a commit SHA before downloading.
_REPO = os.getenv("MT5_SCRIPT_REPO", "Arinze-eng/powerx")

#: Version of the sandbox-side CLI this tool requires.
#:
#: MUST be kept equal to ``CLI_VERSION`` in ``scripts/mt5_cli.py``. The bootstrap
#: refuses a download that does not carry this exact marker, which is what stops
#: a cached/stale revision from being executed silently: instead of debugging
#: code that is no longer running, the caller gets a loud warning and a retry
#: against a different source. Bump BOTH constants together whenever the CLI's
#: contract with this tool changes.
_CLI_VERSION = "2026-09-24.11"

#: Where the CLI and the Wine prefix live inside the sandbox.
_MT5_HOME = "$HOME/.mt5"
_CLI_PATH = f"{_MT5_HOME}/bin/mt5_cli.py"
_INSTALLER_PATH = f"{_MT5_HOME}/bin/install_mt5_sandbox.sh"
#: Where the bootstrap's stderr is captured so its warnings can be surfaced.
_BOOTSTRAP_LOG = f"{_MT5_HOME}/bin/.bootstrap.log"

#: The execution sandbox caps every command at 900 s, but a full Wine + MT5 +
#: bridge install genuinely takes longer. ``install`` therefore starts the
#: installer *detached* and returns immediately; progress is polled through
#: ``status``. Keeping this in sync with the sandbox ceiling matters: if the
#: install were run inline it would be killed mid-prefix-build.
_MAX_SANDBOX_COMMAND_TIMEOUT = 900
_INSTALL_COMMAND_TIMEOUT = 120

#: Actions that move money. Blocked unless explicitly enabled.
#:
#: ``modify`` belongs here because it changes where a LIVE position exits, and
#: ``guard`` because arming a guard is arming a close. Their safe halves --
#: reading the guard, reading its event log, stopping it -- are exempted in
#: execute() (see _GUARD_SAFE_SUBACTIONS), so an operator can always inspect or
#: disarm protection even with trading disabled.
_TRADING_ACTIONS = frozenset(
    {"order", "split", "close", "close_all", "cancel", "modify", "guard", "limits"}
)

#: ``guard`` sub-actions that place no order. These stay available without
#: MT5_ALLOW_TRADING: refusing to report the state of a live guard would leave
#: protection running with no way to look at it.
_GUARD_SAFE_SUBACTIONS = frozenset({"status", "stop", "events", "clear"})

#: The same reasoning for the account's own limits: ``show`` changes nothing and
#: is the one call most worth having when trading is switched off, because it is
#: what says whether there is any protection at all.
_LIMITS_SAFE_SUBACTIONS = frozenset({"show"})

#: Actions that only read state. These never require the trading opt-in.
_READ_ONLY_ACTIONS = frozenset(
    {
        "status", "doctor", "account", "quote", "candles", "positions", "orders",
        "history", "symbol", "symbols", "logs", "experts", "run",
        # `watch` reads positions, prices and the guard's files and changes
        # nothing, so it must never be gated behind the trading opt-in: the
        # caller most in need of watching a live trade is the one who has just
        # been told trading is off.
        "watch",
        # `plan` is pure arithmetic over numbers the caller supplied -- it
        # reaches no terminal, places no order and needs no sandbox at all. It
        # is also the one action that must keep working when the sandbox is
        # gone, because it is what tells the caller what the stop and target
        # *would* have been.
        "plan",
        # `risk` reads the book, the account and today's closed deals and
        # changes nothing. Gating it behind the trading opt-in would hide the
        # account's own exposure from the caller who most needs to see it.
        "risk",
    }
)

#: Actions answered entirely on the host, with no bridge call and no sandbox.
#: They are dispatched before the sandbox is looked for, so they still work in a
#: deployment whose sandbox is unreachable -- which is exactly when a caller is
#: most likely to be asking "what is my stop supposed to be?".
_HOST_ONLY_ACTIONS = frozenset({"plan"})

_ALL_ACTIONS = sorted(
    _READ_ONLY_ACTIONS
    | _TRADING_ACTIONS
    | {"install", "start", "stop", "login", "compile"}
)

#: Generous per-action timeouts. Installing Wine + MT5 genuinely takes minutes,
#: so ``install`` only ever kick-starts a detached process (see above) and
#: readiness is reported by ``status``.
#:
#: These ceilings are deliberately high. A sandbox command that times out returns
#: NO JSON at all, and a model given an empty failure consistently invents a
#: reason — historically "the mt5_sandbox tool was not responding because the
#: MT5/Wine container was not initialized" — and hands the .mq5 back to the user.
#: The first MetaEditor run inside a cold prefix legitimately takes minutes, so
#: ``compile`` gets nearly the full 900 s sandbox cap rather than 360 s.
_TIMEOUTS: dict[str, int] = {
    "install": _INSTALL_COMMAND_TIMEOUT,
    "status": 120,
    "start": 120,
    "stop": 120,
    "doctor": 120,
    "compile": 120,
    "candles": 120,
    "symbols": 120,
    "history": 120,
    "run": 120,
    # `modify` is one TRADE_ACTION_SLTP per position through the Wine bridge;
    # `guard` only writes files and spawns the watcher (which keeps running
    # after this call returns), so both fit well inside the ceiling.
    "modify": 120,
    "guard": 120,
    # `watch` measures the market while it waits, so like `guard` its ceiling is
    # raised to cover the wait (see execute()).
    "watch": 120,
    # `split` is N order sends inside ONE bridge invocation: the Wine re-exec is
    # paid once rather than N times, which is the whole reason `--splits 10` is
    # not ten calls. Still bounded well inside the ceiling.
    "split": 120,
    # `cancel` is one TRADE_ACTION_REMOVE per pending order through the bridge.
    "cancel": 120,
    # `risk` reads positions + account + today's deals; `limits` writes one JSON
    # file, so neither is anywhere near the ceiling.
    "risk": 120,
    "limits": 120,
}
_DEFAULT_TIMEOUT = 120


def _sandbox_tool(ctx: ToolContext | None) -> Any:
    """Find the configured execution sandbox tool, mirroring arduino_verify.

    Reusing the sandbox tool means MT5 inherits whatever backend the deployment
    already chose (Novita by default) plus its per-session sandbox reuse, sizing,
    and lifecycle. This tool therefore adds no new infrastructure.
    """
    if ctx is None:
        return None
    registry = getattr(ctx, "tool_registry", None) or getattr(ctx, "tools", None)
    if registry is None:
        return None

    # The registry is a ToolRegistry, not a dict. Resolve by name first — that is
    # the only guaranteed API — then fall back to iterating tools defensively.
    # Relying on iteration alone used to raise TypeError (the registry had no
    # __iter__/values), and because this function swallows exceptions it returned
    # None, which reached the model as "No execution sandbox is configured" even
    # when the sandbox was fully configured.
    for name in ("novita_sandbox", "vps_exec", "runloop_sandbox", "daytona_sandbox"):
        getter = getattr(registry, "get", None)
        if callable(getter):
            try:
                tool = getter(name)
            except Exception:  # pragma: no cover - defensive
                tool = None
            if tool is not None:
                return tool

    try:
        if isinstance(registry, dict):
            items: Any = registry.values()
        elif callable(getattr(registry, "values", None)):
            items = registry.values()
        else:
            items = registry
        for tool in items:
            name = getattr(tool, "name", "")
            if name in ("novita_sandbox", "vps_exec", "runloop_sandbox", "daytona_sandbox"):
                return tool
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def _missing_from_text(text: str) -> list[str]:
    """Recover the missing-chain list from a text-only refusal.

    When the sandbox drops stdout we still get the human-readable error line, which
    enumerates what is absent. Parsing it back keeps the auto-provision report
    informative instead of blank.
    """
    marker = "is missing:"
    idx = text.find(marker)
    if idx == -1:
        return []
    tail = text[idx + len(marker):].strip().rstrip(".")
    head = tail.split(" Do NOT ")[0]
    return [piece.strip() for piece in head.split(",") if piece.strip()]


def bootstrap_command() -> str:
    """Idempotently fetch the CLI + installer into the sandbox."""
    # The cache-buster is load-bearing, not decoration.
    #
    # MEASURED FAILURE (2026-09-21): raw.githubusercontent.com served a stale
    # ``mt5_cli.py`` for several minutes after a push — the sandbox kept fetching
    # the previous revision, so a fix that was already on main (and covered by
    # tests) still produced the OLD failure live. That is a uniquely expensive
    # trap here: the whole point of this bootstrap is "a fixed CLI ships without
    # rebuilding the sandbox", and a cached edge response makes it silently
    # untrue, sending the agent off to debug code that is no longer running.
    # A unique query string gives the CDN a URL it has never cached.
    # Double quotes, NOT single: the command is interpreted by the sandbox's
    # shell, and a single-quoted ``$(date +%s)`` reaches curl literally (and curl
    # then rejects the URL, while ``|| true`` hides it — leaving the STALE file in
    # place, i.e. the exact bug this is meant to prevent).
    return _BOOTSTRAP_TEMPLATE.format(
        home=_MT5_HOME,
        cli=_CLI_PATH,
        installer=_INSTALLER_PATH,
        repo=_REPO,
        raw_base=_RAW_BASE,
        version=_CLI_VERSION,
    )


#: Bootstrap shell. ``{...}`` placeholders are filled by ``bootstrap_command``.
#:
#: MEASURED FAILURE (2026-09-21) — the reason this is not a one-line curl:
#:
#: The point of bootstrapping by URL is that a fixed CLI ships without rebuilding
#: the sandbox. That silently stopped being true: the sandbox's egress path
#: caches ``raw.githubusercontent.com`` responses **by path**, so
#: ``.../main/scripts/mt5_cli.py`` kept returning a revision several pushes old.
#: Neither a unique ``?ts=`` query string nor ``Cache-Control: no-cache`` helped
#: (both were measured). The effect was maximally confusing: a fix that was on
#: main, covered by tests and verified from the host still produced the OLD
#: failure live, so the fix looked wrong when it was simply not running.
#:
#: What was measured to work:
#:   * a commit-pinned raw URL (``/<sha>/scripts/...``) — never cached, because
#:     that exact URL had never been requested before, and
#:   * the GitHub API.
#:
#: So: resolve ``main`` to a SHA through the API, download the pinned URL, and
#: VERIFY the result carries the ``CLI_VERSION`` this tool requires. Only if that
#: fails do we fall back to the branch URL — and a version mismatch on every
#: source is reported loudly instead of executing unknown code.
_BOOTSTRAP_TEMPLATE = """\
mkdir -p {home}/bin
_want='{version}'
_fetch() {{ curl -fsSL --retry 2 "$1" -o "$2" 2>/dev/null && grep -q "CLI_VERSION = [\\"']$_want[\\"']" "$2"; }}
_sha=$(curl -fsSL 'https://api.github.com/repos/{repo}/commits/main' 2>/dev/null \
  | python3 -c "import sys,json;print((json.load(sys.stdin) or {{}}).get('sha',''))" 2>/dev/null)
_ok=''
for _base in "https://raw.githubusercontent.com/{repo}/$_sha/scripts" "{raw_base}"; do
  if _fetch "$_base/mt5_cli.py" {cli}; then
    _ok=1
    curl -fsSL --retry 2 "$_base/install_mt5_sandbox.sh" -o {installer} 2>/dev/null
    break
  fi
done
chmod +x {cli} {installer} 2>/dev/null
if [ -z "$_ok" ]; then
  echo "WARNING: could not fetch mt5_cli.py version $_want (a cached copy of an" >&2
  echo "older revision may be in use). Retry, or set MT5_SCRIPT_RAW_BASE." >&2
else
  echo "mt5_cli.py $_want ready" >&2
fi
python3 {cli} doctor\
"""


def _sh(value: Any) -> str:
    return shlex.quote(str(value))


#: A broker-branded MT5 installer URL, as published on the broker's own
#: "Download MT5" page. Exness is ``exness.technologies.ltd``.
#:
#: WHY THIS IS VALIDATED AT ALL: the slug is easy to get subtly wrong, and
#: getting it wrong is *silent*. A missing TLD (``exness.technologies``) still
#: looks like a perfectly good URL, and the CDN answers a flat 404 — so the
#: install burns two minutes on Wine, then dies with "could not download the MT5
#: installer", which reads like a network fault rather than a typo. Reproduced
#: 2026-09-22: an agent-supplied URL with the ``.ltd`` dropped 404'd five times
#: in a row while the correct URL returned 200 and 5,156,488 bytes.
_BROKER_URL_RE = re.compile(
    r"^https://download\.mql5\.com/cdn/web/(?P<slug>[A-Za-z0-9][A-Za-z0-9.\-]*)/mt5/(?P<name>[A-Za-z0-9._\-]+)\.exe$"
)

#: Broker slugs verified to work, so a legitimate slug is never second-guessed.
#: Exness is the one this deployment uses; the value is the full slug.
_KNOWN_GOOD_SLUGS = frozenset({"exness.technologies.ltd"})

#: A slug's final label should look like a public suffix. This is what catches the
#: real failure: ``exness.technologies`` and ``exness.technologies.ltd`` both have
#: dots, so a naive "does it have a dot" test passes the broken one. The last label
#: is what differs — ``ltd`` is suffix-shaped, ``technologies`` is not.
_SUFFIX_RE = re.compile(r"^[A-Za-z]{2,12}$")

#: Multi-label public suffixes where the second-to-last label is generic, e.g.
#: ``example.co.uk`` / ``broker.com.br``. Not exhaustive — it only needs to avoid
#: false rejections, since the fallback is a warning-free pass for known slugs.
_GENERIC_SLDS = frozenset({"co", "com", "org", "net", "gov", "ac", "edu"})

#: Stages that mean "stop waiting" — the installer will not progress further.
#: ``done`` is success; ``failed`` is a real install error with a log tail.
_TERMINAL_STAGES = frozenset({"done", "failed"})


def validate_broker_installer_url(url: str) -> str | None:
    """Return an error message if ``url`` is not a usable broker installer URL.

    Catches the failure *before* the sandbox is touched, so a typo costs zero
    install time. An empty URL is fine — that selects the generic terminal.
    """
    candidate = (url or "").strip()
    if not candidate:
        return None
    if not candidate.startswith("https://"):
        return (
            f"broker_installer_url must be https:// (got {candidate!r}). "
            "MT5 installers are only served over TLS."
        )

    match = _BROKER_URL_RE.match(candidate)
    if not match:
        return (
            f"broker_installer_url is not a recognised MT5 broker installer URL: "
            f"{candidate!r}. Expected "
            "https://download.mql5.com/cdn/web/<broker-slug>/mt5/<name>setup.exe "
            "(e.g. https://download.mql5.com/cdn/web/exness.technologies.ltd/"
            "mt5/exness5setup.exe). Copy the link from the broker's own "
            "'Download MT5' page."
        )

    slug = match.group("slug")
    if slug in _KNOWN_GOOD_SLUGS:
        return None

    labels = slug.split(".")
    if len(labels) < 2:
        return (
            f"broker_installer_url slug {slug!r} has no TLD, which usually means a "
            "truncated slug (Exness is 'exness.technologies.ltd', not 'exness'). "
            "Verify the link on the broker's own 'Download MT5' page."
        )

    last = labels[-1]
    second_last = labels[-2] if len(labels) >= 2 else ""
    if not _SUFFIX_RE.match(last) or (
        len(last) > 6 and second_last.lower() not in _GENERIC_SLDS
    ):
        return (
            f"broker_installer_url slug {slug!r} looks truncated: its final label "
            f"{last!r} is not TLD-shaped. This is the classic dropped-TLD typo — "
            "Exness is 'exness.technologies.ltd', and 'exness.technologies' returns "
            "a permanent 404. Copy the exact link from the broker's own "
            "'Download MT5' page and retry. If the slug really is correct, pass the "
            "known-good value and it will not be second-guessed."
        )
    return None


#: Server-name prefixes that resolve to a broker build WITHOUT asking the user for
#: their broker's download link. Mirrors ``BROKER_BUILDS`` in ``scripts/mt5_cli.py``
#: (the CLI is authoritative about what it installs; this only decides whether the
#: tool can finish an install on its own). MetaQuotes' own demo servers resolve on
#: the generic build, which needs no broker URL at all.
_KNOWN_SERVER_PREFIXES = ("metaquotes", "exness", "deriv")


def _server_is_known(server: str | None) -> bool:
    name = (server or "").strip().lower()
    return bool(name) and any(name.startswith(prefix) for prefix in _KNOWN_SERVER_PREFIXES)


def build_guard_rule(kwargs: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Turn tool kwargs into ONE guard rule, or explain what is missing.

    Kept apart from the command builder so the mapping is unit-testable without a
    sandbox, and so the model only ever passes plain fields -- a caller that has
    to hand-write JSON is a caller that will produce JSON the watcher cannot read.
    """
    symbol = str(kwargs.get("symbol") or "").strip()
    if not symbol:
        return None, "guard action='arm' requires 'symbol'."
    if kwargs.get("trigger_price") is None:
        return None, (
            "guard action='arm' requires 'trigger_price' -- the level to act on. "
            "For 'close when EURUSD hits 1.1650' that is 1.1650."
        )
    try:
        level = float(kwargs["trigger_price"])
    except (TypeError, ValueError):
        return None, "trigger_price must be a number."

    op = str(kwargs.get("trigger_op") or "").strip()
    if op and op not in (">=", "<="):
        return None, (
            f"trigger_op must be '>=' (at or above the level) or '<=' (at or below "
            f"it); got {op!r}. Omit it and the direction is inferred from the live "
            "price."
        )

    side = str(kwargs.get("trigger_side") or "mid").strip().lower() or "mid"
    if side not in ("bid", "ask", "mid"):
        return None, f"trigger_side must be bid, ask or mid; got {side!r}."

    rule: dict[str, Any] = {
        "symbol": symbol,
        "price": level,
        "side": side,
        "action": "close",
    }
    if op:
        rule["op"] = op
    if kwargs.get("ticket"):
        rule["ticket"] = int(kwargs["ticket"])
    elif kwargs.get("all_positions"):
        rule["scope"] = {"all": True}
    if kwargs.get("volume") is not None:
        rule["volume"] = float(kwargs["volume"])
    if kwargs.get("max_seconds") is not None:
        rule["max_seconds"] = int(kwargs["max_seconds"])
    return rule, None


def _host_plan(kwargs: dict[str, Any]) -> str:
    """``action='plan'``: the playbook's order for the numbers given.

    Pure arithmetic, no bridge. The caller supplies the entry (or a live price
    it already fetched) and gets back the stop, the target, the lots, the
    document's range sub-levels, and every way the setup breaks the playbook's
    own rules -- a 20-pip stop at 1:7, entered at a level.

    It deliberately does NOT place the order, and it deliberately does NOT
    pretend the setup is good. A 1:7 target is a claim about the market; all
    this can be sure of is the arithmetic and the rule compliance, and the
    result says which is which.
    """
    from nanobot.trading.gold_strategy import plan as build_plan

    entry = kwargs.get("entry")
    side = str(kwargs.get("side") or "").strip().lower()
    if entry is None:
        return ToolResult.error(
            "action=plan requires 'entry' (the price to build the stop and target "
            "from). Get one from action=quote -- pass the ask for a buy and the bid "
            "for a sell -- or pass the level you intend to enter at."
        )
    if side not in ("buy", "sell"):
        return ToolResult.error("action=plan requires 'side' ('buy' or 'sell').")
    try:
        result = build_plan(
            side=side,
            entry=float(entry),
            volume=float(kwargs.get("volume") or 0.1),
            symbol=str(kwargs.get("symbol") or "XAUUSD"),
            equity=float(kwargs["equity"]) if kwargs.get("equity") else None,
            sl_pips=float(kwargs.get("sl_pips") or 20.0),
            rr=float(kwargs.get("rr") or 7.0),
            low=float(kwargs["range_low"]) if kwargs.get("range_low") is not None else None,
            high=float(kwargs["range_high"]) if kwargs.get("range_high") is not None else None,
            spread=float(kwargs["spread"]) if kwargs.get("spread") is not None else None,
        )
    except (TypeError, ValueError) as exc:
        return ToolResult.error(f"action=plan could not build the setup: {exc}")
    return json.dumps(result)


def build_cli_command(action: str, kwargs: dict[str, Any]) -> str:
    """Translate tool kwargs into an ``mt5_cli.py`` invocation."""
    parts = ["python3", _CLI_PATH, action]

    if action == "install":
        # A broker-branded installer is REQUIRED for any real broker account:
        # the generic MetaQuotes terminal ships no broker server list, cannot
        # resolve a broker server name, and silently never authorizes (see the
        # installer's MT5_BROKER_INSTALLER_URL note). Passed as an env prefix
        # because that is how the installer already reads it.
        broker_url = kwargs.get("broker_installer_url")
        if broker_url:
            parts.insert(0, f"MT5_BROKER_INSTALLER_URL={_sh(broker_url)}")
        broker_dir = kwargs.get("broker_dir_name")
        if broker_dir:
            parts.insert(0, f"MT5_BROKER_DIR_NAME={_sh(broker_dir)}")
        # The SERVER decides which build is installed, so passing it here is what
        # makes the FIRST install correct for any broker instead of paying for a
        # re-install after a refused login. The CLI resolves it against the broker
        # registry and sets MT5_BROKER_INSTALLER_URL / _DIR_NAME / _KEY itself, so
        # one flag covers every registered broker.
        server = kwargs.get("server")
        if server:
            parts += ["--server", _sh(server)]
        parts += ["--script", _INSTALLER_PATH]
        if kwargs.get("timeout"):
            parts += ["--timeout", str(int(kwargs["timeout"]))]
        if kwargs.get("foreground"):
            # Only for callers whose own command ceiling exceeds the install
            # duration; the sandbox default must stay detached.
            parts += ["--foreground"]
    elif action == "status":
        parts += ["--lines", str(int(kwargs.get("lines") or 25))]
        # A WATCH, not a snapshot. With wait_seconds set this call blocks and
        # samples the install every poll_seconds, returning the moment it moves
        # -- stage change, more installer output, or the installer exiting -- so
        # "still installing" is something the caller OBSERVED and has fresh log
        # to reason about, rather than an assertion it repeats each turn.
        # Timing out is reported as `watched.timed_out`, so "nothing moved yet"
        # can never be read as "something moved".
        if float(kwargs.get("wait_seconds") or 0.0) > 0:
            parts += [
                "--wait-seconds", str(float(kwargs["wait_seconds"])),
                "--poll-seconds", str(float(kwargs.get("poll_seconds") or 2.0)),
            ]
    elif action == "start":
        # The wait MUST fit inside this action's own command ceiling: a command
        # killed by the sandbox returns no JSON at all, which reads as "the tool is
        # broken" rather than "the terminal was still authorizing". A login that
        # outlives it is not lost -- the terminal keeps authorizing in the
        # background, so the next account/start call sees the account.
        parts += ["--wait", str(min(int(kwargs.get("wait") or 90), 100))]
        # Credentials can be supplied here so the terminal auto-connects on boot,
        # which is the only way to get IPC without a GUI login.
        for flag in ("login", "password", "server"):
            if kwargs.get(flag) not in (None, ""):
                parts += [f"--{flag}", _sh(kwargs[flag])]
        # A seeded login is only honoured in portable mode.
        if kwargs.get("login") or kwargs.get("portable"):
            parts += ["--portable"]
    elif action == "login":
        for flag in ("login", "password", "server"):
            if kwargs.get(flag) not in (None, ""):
                parts += [f"--{flag}", _sh(kwargs[flag])]
        if kwargs.get("path"):
            parts += ["--path", _sh(kwargs["path"])]
    elif action == "quote":
        symbols = kwargs.get("symbols") or kwargs.get("symbol") or []
        if isinstance(symbols, str):
            symbols = [s for s in re.split(r"[,\s]+", symbols) if s]
        parts += [_sh(s) for s in symbols]
    elif action == "candles":
        parts += [
            "--symbol", _sh(kwargs.get("symbol") or ""),
            "--timeframe", _sh(kwargs.get("timeframe") or "M15"),
            "--count", str(int(kwargs.get("count") or 200)),
        ]
    elif action == "history":
        parts += ["--days", str(int(kwargs.get("days") or 7))]
    elif action == "symbols":
        if kwargs.get("filter"):
            parts += ["--filter", _sh(kwargs["filter"])]
        if kwargs.get("tradable"):
            parts += ["--tradable"]
        if kwargs.get("limit"):
            parts += ["--limit", str(int(kwargs["limit"]))]
    elif action == "symbol":
        parts += [_sh(kwargs.get("symbol") or "")]
    elif action == "order":
        parts += [
            "--symbol", _sh(kwargs.get("symbol") or ""),
            "--side", _sh(kwargs.get("side") or ""),
        ]
        # `--volume` is emitted ONLY when the caller gave one: sending
        # `--volume 0.0` alongside `--risk-money` would be a second, zero answer
        # to the same question and the CLI refuses two sizes for one order.
        if kwargs.get("volume") is not None:
            parts += ["--volume", str(float(kwargs["volume"]))]
        # The entry style. Passed through explicitly rather than defaulted here so
        # the CLI, which validates the price against the live tick, is the one
        # place that decides whether a limit/stop is on the correct side.
        entry_type = str(kwargs.get("entry_type") or "market").strip().lower()
        if entry_type != "market":
            parts += ["--entry-type", _sh(entry_type)]
        if kwargs.get("price") is not None:
            parts += ["--price", str(float(kwargs["price"]))]
        for flag in ("risk_money", "risk_pct"):
            if kwargs.get(flag) is not None:
                parts += [f"--{flag.replace('_', '-')}", str(float(kwargs[flag]))]
        for flag in ("sl", "tp"):
            if kwargs.get(flag) is not None:
                parts += [f"--{flag}", str(float(kwargs[flag]))]
        if kwargs.get("allow_no_stop"):
            parts += ["--allow-no-stop"]
        if kwargs.get("deviation") is not None:
            parts += ["--deviation", str(int(kwargs["deviation"]))]
        if kwargs.get("comment"):
            parts += ["--comment", _sh(kwargs["comment"])]
    elif action == "cancel":
        # One ticket, or every pending order. `--all` is the sweep; the CLI has no
        # "cancel the order I just placed" heuristic, so it takes the ticket.
        if kwargs.get("cancel_all"):
            parts += ["--all"]
        elif kwargs.get("ticket") is not None:
            parts += ["--ticket", str(int(kwargs["ticket"]))]
    elif action == "split":
        parts += [
            "--symbol", _sh(kwargs.get("symbol") or ""),
            "--side", _sh(kwargs.get("side") or ""),
            "--volume", str(float(kwargs.get("volume") or 0)),
            "--splits", str(int(kwargs.get("splits") or 10)),
        ]
        for flag in ("sl", "tp", "group"):
            if kwargs.get(flag) not in (None, ""):
                parts += [f"--{flag}", _sh(kwargs[flag])]
        if kwargs.get("deviation") is not None:
            parts += ["--deviation", str(int(kwargs["deviation"]))]
        if kwargs.get("comment"):
            parts += ["--comment", _sh(kwargs["comment"])]
        for flag in ("stop_on_failure", "check_cost", "allow_no_stop"):
            if kwargs.get(flag):
                parts += [f"--{flag.replace('_', '-')}"]
    elif action == "close":
        # Closing PART of a split: `--group` targets the tickets the split
        # labelled, and `--count` is what makes it partial (three off, seven
        # left). A group close must NOT also emit `--ticket 0`, which the CLI
        # would read as a ticket, so the two are mutually exclusive here.
        if kwargs.get("group"):
            parts += ["--group", _sh(kwargs["group"])]
            if kwargs.get("count") is not None:
                parts += ["--count", str(int(kwargs["count"]))]
        else:
            parts += ["--ticket", str(int(kwargs.get("ticket") or 0))]
        if kwargs.get("volume") is not None:
            parts += ["--volume", str(float(kwargs["volume"]))]
        if kwargs.get("deviation") is not None:
            parts += ["--deviation", str(int(kwargs["deviation"]))]
    elif action == "modify":
        # `--ticket` is repeatable, so both the single and list spellings are
        # accepted; a caller that has one position should not have to wrap it.
        for ticket in list(kwargs.get("tickets") or []):
            parts += ["--ticket", str(int(ticket))]
        if kwargs.get("ticket"):
            parts += ["--ticket", str(int(kwargs["ticket"]))]
        if kwargs.get("symbol"):
            parts += ["--symbol", _sh(kwargs["symbol"])]
        if kwargs.get("all_positions"):
            parts += ["--all"]
        if kwargs.get("exit_at") is not None:
            parts += ["--exit-at", str(float(kwargs["exit_at"]))]
        for flag in ("sl", "tp"):
            if kwargs.get(flag) is not None:
                parts += [f"--{flag}", str(float(kwargs[flag]))]
    elif action == "guard":
        sub_action = str(kwargs.get("guard_action") or "status").strip().lower()
        parts += [_sh(sub_action)]
        if sub_action == "arm":
            rule, rule_error = build_guard_rule(kwargs)
            if rule is not None:
                # Serialised here (not by the caller) so a quoted level can never
                # be mangled by shell splitting on the way into the sandbox.
                parts += [
                    "--rule",
                    _sh(json.dumps(rule)),
                    "--interval-ms",
                    str(int(kwargs.get("interval_ms") or 100)),
                ]
                # Only restate the budget when the caller set one: the CLI's own
                # default is NO limit, because an exit at a price has to be held
                # for however long the price takes. Forcing 3600 here is what used
                # to stop a guard after an hour with the level still untouched.
                if kwargs.get("max_seconds"):
                    parts += ["--max-seconds", str(int(kwargs["max_seconds"]))]
                # Arming on a symbol the CLI cannot price is refused by default:
                # the guard would poll in silence and look exactly like
                # protection. This is the caller's explicit way to say "the
                # symbol prices later, watch for it".
                if kwargs.get("guard_allow_unpriceable"):
                    parts += ["--allow-unpriceable"]
            elif rule_error:  # pragma: no cover - validated in execute()
                raise ValueError(rule_error)
        elif sub_action == "ensure":
            # Restarting the watcher inherits how it was armed, so the wait and
            # the tick interval can be restated; the rules themselves come from
            # the rules file the stopped watcher left behind.
            parts += ["--interval-ms", str(int(kwargs.get("interval_ms") or 100))]
            # Same rule as arm: an omitted budget means "inherit what it was
            # armed with", so a re-arm never downgrades an unlimited guard to an
            # hour or silently extends a deliberately bounded one.
            if kwargs.get("max_seconds"):
                parts += ["--max-seconds", str(int(kwargs["max_seconds"]))]
        elif sub_action == "status":
            # A WATCH, not a snapshot. With wait_seconds set, this call blocks and
            # samples the guard every poll_seconds, returning the moment the event
            # log grows or the budget ends -- so "the guard is monitoring" is
            # something the caller has OBSERVED rather than asserted. Timing out
            # is reported as `watched.timed_out`, so "nothing happened yet" can
            # never be read as "something happened".
            wait = float(kwargs.get("wait_seconds") or 0.0)
            if wait > 0:
                parts += [
                    "--wait-seconds", str(wait),
                    "--poll-seconds",
                    str(float(kwargs.get("poll_seconds") or 1.0)),
                ]
        elif sub_action == "events":
            parts += ["--lines", str(int(kwargs.get("lines") or 20))]
    elif action == "watch":
        # Symbols are optional: with none, the CLI watches the symbols of the
        # OPEN POSITIONS, which is the set that carries risk. `symbols` is
        # accepted as well as `symbol` so the field that already means "the
        # instruments I care about" on action=quote works here too.
        raw = kwargs.get("symbol") or kwargs.get("symbols") or []
        if isinstance(raw, str):
            raw = [s for s in re.split(r"[,\s]+", raw) if s]
        for sym in raw:
            parts += ["--symbol", _sh(str(sym))]
        wait = float(kwargs.get("wait_seconds") or 0.0)
        if wait > 0:
            parts += [
                "--wait-seconds", str(wait),
                "--poll-seconds", str(float(kwargs.get("poll_seconds") or 1.0)),
            ]
        parts += ["--lines", str(int(kwargs.get("lines") or 20))]
        # One observation, many calls. Passed through unchanged so consecutive
        # watches fold into a single timeline instead of reporting a fresh
        # "first price" every time.
        if kwargs.get("watch_session"):
            parts += ["--session", _sh(kwargs["watch_session"])]
    elif action == "risk":
        # No arguments. The whole point of the call is that the answer is the
        # report: what is open, what it risks, what today has cost, and the room
        # left. Anything that had to be passed in would be arithmetic the caller
        # was already doing by hand.
        pass
    elif action == "limits":
        sub_action = str(kwargs.get("limits_action") or "show").strip().lower()
        parts += [_sh(sub_action)]
        # Emitted ONLY when the caller set one, so a `set` merges into what is
        # already in force instead of silently clearing the limits it did not
        # mention.
        for flag in (
            "max_daily_loss_money",
            "max_positions",
            "max_total_risk_money",
            "max_total_risk_pct",
        ):
            if kwargs.get(flag) is None:
                continue
            # A count is not a money value: the CLI reads --max-positions as an
            # int, so "2.0" would be rejected as a bad argument rather than
            # silently rounded.
            value = (
                str(int(kwargs[flag]))
                if flag == "max_positions"
                else str(float(kwargs[flag]))
            )
            parts += [f"--{flag.replace('_', '-')}", value]
    elif action == "compile":
        parts += ["--file", _sh(kwargs.get("file") or "")]
        if kwargs.get("include"):
            parts += ["--include", _sh(kwargs["include"])]
    elif action in ("logs", "experts"):
        parts += ["--lines", str(int(kwargs.get("lines") or 100))]
    elif action == "run":
        parts += ["--code", _sh(kwargs.get("code") or "")]

    return " ".join(parts)


def _trading_enabled() -> bool:
    return os.getenv("MT5_ALLOW_TRADING", "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_payload(rendered: str) -> dict[str, Any] | None:
    """Pull the CLI's JSON object out of the sandbox command output.

    The sandbox wrapper appends ``[exit_code=N]`` and may interleave log lines,
    so the last balanced ``{...}`` block is the reliable extraction target.
    """
    text = rendered or ""
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


class MT5SandboxTool(Tool):
    """Install, compile, log, and trade MT5 entirely inside the user's sandbox."""

    config_key = "mt5_sandbox"
    _scopes = {"core", "subagent"}

    def __init__(self, ctx: ToolContext | None = None) -> None:
        # The ToolContext is retained so the sandbox tool can be resolved at
        # execute() time (the registry is not available during construction).
        self._ctx: ToolContext | None = ctx

    @classmethod
    def create(cls, ctx: ToolContext) -> "MT5SandboxTool":
        """Carry the tool context so the sandbox tool can be resolved later."""
        return cls(ctx)

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        """Always register the tool; availability is decided at execute() time.

        WHY NOT GATE ON THE SANDBOX HERE: ``enabled()`` is evaluated by the loader
        while it iterates the tool classes, i.e. BEFORE the registry is populated.
        ``_sandbox_tool()`` therefore always returns None at this point and the
        tool was silently dropped from the schema — the model never saw
        ``mt5_sandbox`` exist and kept telling users "the sandbox cannot compile
        MQL5 / mql.exe is Windows-native, compile it yourself".

        The tool is harmless when no sandbox is configured: ``execute()``
        resolves the sandbox per call and returns a clear error instead of
        falling back to the host. Advertising the capability is what matters.
        """
        return True

    @property
    def name(self) -> str:
        return "mt5_sandbox"

    @property
    def description(self) -> str:
        return (
            "MetaTrader 5 in the user's execution sandbox. NEVER runs Wine or the MT5 "
            "terminal on the application host. "
            "MANDATORY FIRST STEP: any MT5/MQL5 work — including compiling an .mq5 the "
            "user just gave you — MUST begin with action='install' (starts a DETACHED "
            "Wine + Xvfb + MT5 + python bridge install inside the sandbox). action="
            "'install' then WAITS for the install itself and returns only when it "
            "reaches a terminal stage (~2-25 min) — so you do NOT need to tell the "
            "user to check back later, and you must NEVER ask them whether to check "
            "again: one call finishes the job. If it still reports stage='installing' "
            "after the wait budget, the install is progressing normally — poll "
            "action='status' yourself, in a loop, until stage='done', without asking "
            "the user anything. "
            "Only then call action='start' WITH login/password/server — all three in "
            "ONE call; that writes the terminal's /config: credentials file. A "
            "terminal left running from the install (status shows "
            "terminal_has_credentials=false) is reclaimed and relaunched automatically, "
            "so never conclude the credentials are unusable: if ok=false, read the "
            "hint — it names the real cause (broker server mismatch, or the account "
            "not existing on that server). "
            "'compile' REFUSES with stage='not_installed' "
            "until the chain exists — that refusal is NOT a source-code error: never try "
            "to fix or compile the .mq5 by any other means, just install first. "
            "Read/fix loop: use 'compile' to build an .mq5 with MetaEditor (returns the "
            "compiler errors), and 'logs'/'experts' to tail the terminal and Experts "
            "journal. "
            "BEFORE trading or fetching candles for an instrument, call action='symbols' "
            "with tradable=true: brokers offer only part of the MT5 universe (a "
            "MetaQuotes-Demo account carries NO crypto, so BTCUSD/ETHUSD fail with "
            "'symbol not found' — that is not a bridge fault). Pick a symbol whose "
            "market_open is true; if none is, every instrument on that server is closed "
            "right now and an order will come back as retcode 10018 'Market closed'. "
            f"Actions: {', '.join(_ALL_ACTIONS)}. "
            "INSTANT EXITS AT A PRICE -- read before promising anything: a position "
            "only closes when SOMETHING sends the close. Polling quote in a loop is "
            "not that something: each poll costs a sandbox round trip plus an agent "
            "turn, so a level touched between polls is missed entirely, and the "
            "position stays open while the user is told it is being watched. So when "
            "the user wants an exit at a price (\"close when it hits X\", \"take "
            "profit at X\", \"get me out if it drops to X\"), arm it in ONE call "
            "instead of watching: action=\"modify\" with exit_at=X puts the level "
            "on the broker server (instant, and it survives this sandbox being "
            "paused or killed) -- use it for an open position. Use action=\"guard\" "
            "when the broker cannot hold the condition: close every position on a "
            "symbol at a level, exit a basket, partial closes at a price. guard "
            "action=\"arm\" returns as soon as the watcher is alive and the watcher "
            "keeps running on its own -- do NOT poll it in a loop; read "
            "guard_action=\"events\" later for the measured trigger->fill latency "
            "(latency_ms), and use guard_action=\"status\" only when asked whether "
            "the exit is still armed. A position you leave with no SL/TP has no "
            "server-side exit at all, so offer to arm one. "
            "DEFAULT PLAYBOOK (Gold, and the default for every trade until told "
            "otherwise): 20-pip stop, 1:7 reward-to-risk, entered AT a range level. "
            "Call action='plan' with entry/side/volume/equity BEFORE order: it returns "
            "the exact sl and tp, the lots, the range sub-levels (25/50/62.5/75/87.5/"
            "100/150%), and every rule the setup breaks. On Gold a pip is 0.10 -- NOT "
            "the 0.01 point a 2-digit quote advertises -- so a 20-pip stop is $2.00. "
            "Never state a stop or target from memory: action='plan' computes it, and "
            "action='order' should be given the sl/tp it returned. "
            "ENTERING AT A PRICE (action='order' with entry_type) -- the two most common "
            "instructions a trader gives are 'buy the dip at X' and 'buy the breakout "
            "above X', and neither is a market order. entry_type='limit' RESTS at "
            "'price' and fills only BETTER than the market (buy below the ask, sell "
            "above the bid); entry_type='stop' rests at 'price' and fills only when the "
            "market BREAKS THROUGH it (buy above the ask, sell below the bid). Both "
            "hold NO position and risk nothing until they fill -- a resting order is "
            "not an open trade, so do not go on to manage or watch it as one. A "
            "limit/stop on the wrong side of the market is refused here with the "
            "corrected wording (the server would only answer retcode 10015). Read "
            "resting orders with action='orders' and remove one with action='cancel' "
            "(ticket=, or cancel_all=true). "
            "EVERY ORDER CARRIES A STOP (sl) unless allow_no_stop=true says otherwise, "
            "and the refusal is not a formality: without a stop the BROKER holds no "
            "exit at all, so the only thing that could close the position is "
            "something looking at the price -- and you are not looking between "
            "calls. A stop is also what lets the position survive this sandbox "
            "being paused or the run ending. If a trade genuinely should not have "
            "one, say so out loud when you report it. A modify that leaves a "
            "position with neither sl nor tp comes back as "
            "alert=position_left_without_a_stop. "
            "SIZING BY MONEY AT RISK (risk_money / risk_pct) -- pass these instead of "
            "'volume' and the lots are derived from the stop distance, the pip and the "
            "contract size, rounded DOWN so the order never risks more than asked. "
            "'Risk $100 on Gold with a 20-pip stop' is a size the caller should not "
            "have to compute: give risk_money=100 and sl=<price>. risk_pct is the same "
            "against account equity. Both need 'sl'. "
            "POLLING A LIVE TRADE -- do it, and keep doing it: while a position is "
            "open you watch it in REAL TIME instead of setting a cron and walking "
            "away, and you think between calls. Loop action='watch' with "
            "wait_seconds=90 and ALWAYS pass watch_session=<one name for this "
            "trade>. Each call returns the moment something happens (a rule fires, a "
            "close is refused, a level is touched and reverted, the watcher dies, the "
            "position set changes), and session.price_path_total plus "
            "session.since_last_call carry the WHOLE trade across calls -- so call "
            "after call is one continuous observation, not unrelated snapshots. A "
            "watch that returns watched.timed_out=true is a normal result: nothing "
            "happened in that 90 s, the trade is still open, and the right next move "
            "is to watch again. Stop looping only when the position is closed, the "
            "user says stop, or you have something to report to the user. Cron and "
            "scheduled tasks are for things that must happen with nobody watching; "
            "a trade you are following is not one of them. YOU ARE THE MANAGEMENT: "
            "no EA and no cron runs between your calls, so nothing moves a stop or "
            "takes a profit unless you call for it. Each call returns trade_state -- "
            "r_multiple, pips_to_sl, pips_to_tp, breakeven_price, risk_money, "
            "risk_pct_of_equity, and trade_state.notes in plain words -- so you are "
            "deciding, not calculating. Act on it: when a ticket is at 1R or more "
            "and breakeven_due is set, move its stop to breakeven_price or take part "
            "of it off; when a position reports alert=no_stop, set a stop before "
            "watching anything else. session.trade_path carries best_r/worst_r "
            "across every call, so a trade that was +3R an hour ago and is flat now "
            "is a decision you can see. "
            "SPLIT TRADING (action='split') -- one idea as N equal tickets at one "
            "price instead of one position: same direction, same stop, and the SAME "
            "TOTAL RISK, but the exits become granular. Take 3 off into a run and "
            "leave 7 working with action='close', group=<label>, count=3. Give every "
            "ticket the same sl, or the split multiplies risk instead of exits. It "
            "fires once at one price and never adds tickets as the price moves "
            "against you. Adding tickets as the price falls is a grid/martingale, "
            "which empties accounts, and it is not this action: if the price goes "
            "against a split, the answer is the stop, never more tickets. A split "
            "needs a HEDGING account: on a NETTING account only one position per "
            "symbol can exist, so N tickets would net into one oversized trade, "
            "and split refuses rather than doing that. It also refuses when the "
            "free margin cannot cover every ticket, because a half-filled split "
            "leaves a stop covering fewer tickets than planned. Check "
            "action=account for margin_mode and free margin BEFORE sizing a split. "
            "THE ACCOUNT CIRCUIT BREAKER (action='limits', and action='risk' to "
            "see the book it applies to) -- set these before the next order, not "
            "after the loss. A stop caps ONE trade; these cap the ACCOUNT, which is "
            "the thing that runs out, and no single ticket's stop does it: five "
            "'small' positions each risking 2% is 10% on the table. max_total_risk_"
            "money is the most the whole book may lose if every stop is hit at once, "
            "max_positions caps how many tickets may exist (a split counts as its "
            "full N), and max_daily_loss_money ends the day once the REALISED loss "
            "reaches it (deposits and withdrawals are excluded; it refuses new "
            "entries, it does not liquidate, so close what is open yourself). They "
            "are enforced at the point an order is sent, so they bind every caller "
            "and not only whoever remembers them: a refused order comes back with "
            "ok=false, the breaches and their numbers, and NOTHING was sent. Call "
            "action='risk' BEFORE sizing anything -- it is ONE call for what is "
            "open, what each position loses at its stop, what today has already "
            "cost, and the headroom left. Read its totals, then size the order to "
            "the headroom. A position with no stop has UNKNOWN risk rather than zero "
            "and is listed separately, because it is the one that makes a total-risk "
            "limit meaningless -- fix it before trusting the total. "
            "Trading actions (order, close, close_all, modify, guard, limits) require "
            "MT5_ALLOW_TRADING to be "
            "enabled and return the broker retcode; a rejected order is reported with "
            "code 3 and its reason rather than raising. "
            "After an order, verify it: retcode 10009 means the broker executed it, "
            "'positions' then lists the open position and 'history' the closed deals "
            "(newest first, window anchored to the BROKER's clock — tick time is "
            "server time, so never compare it to the sandbox clock). "
            "All output is JSON: read it, fix errors, retry."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": _ALL_ACTIONS},
                "login": {"type": "integer", "description": "MT5 account number (action=login, or action=start to connect on boot)."},
                "password": {"type": "string", "description": "MT5 password (action=login, or action=start). Pass login+password+server together or none of them."},
                "server": {"type": "string", "description": "Broker server the account lives on, e.g. 'Exness-MT5Trial9' or 'MetaQuotes-Demo'. Pass it to action=install as well as start/login: it selects the matching MT5 build, because a terminal can only resolve server names its own build carries."},
                "path": {"type": "string", "description": "Explicit terminal64.exe path override (action=login)."},
                "symbol": {"type": "string", "description": "Trading symbol, e.g. EURUSD (quote/candles/symbol/order)."},
                "filter": {"type": "string", "description": "action=symbols: case-insensitive substring to match symbol names."},
                "tradable": {"type": "boolean", "description": "action=symbols: only list symbols that are enabled AND have a fresh tick (market open now)."},
                "limit": {"type": "integer", "description": "action=symbols: max rows to return (default 60)."},
                "symbols": {"type": "string", "description": "Space/comma separated symbols for action=quote, and for action=watch (where it is optional -- omit it and the symbols of the OPEN POSITIONS are watched)."},
                "timeframe": {"type": "string", "description": "M1..MN1 (action=candles)."},
                "count": {"type": "integer", "description": "action=candles: number of bars. action=close with group: close only this many of the group, oldest comment first. This is 'take 3 of the 10 off, leave 7 running'. 0 or omitted closes the whole group."},
                "days": {"type": "integer", "description": "History window in days (action=history)."},
                "side": {"type": "string", "enum": ["buy", "sell"], "description": "Order direction (action=order)."},
                "volume": {"type": "number", "description": "Lots (action=order/close). action=order: OMIT it when passing risk_money/risk_pct, which derive the lots from the stop instead."},
                "entry_type": {"type": "string", "enum": ["market", "limit", "stop"], "description": "action=order: where the order enters. 'market' (default) fills now at the current price. 'limit' rests at 'price' and fills only BETTER than the market (buy below, sell above) -- 'buy the dip at X'. 'stop' rests at 'price' and fills only when the market BREAKS THROUGH it (buy above, sell below) -- 'buy the breakout above X'. Both rest server-side holding no position until they fill. A limit/stop on the wrong side of the market is refused with the corrected wording."},
                "price": {"type": "number", "description": "action=order: the entry price when entry_type is 'limit' or 'stop'. That price IS the entry. Not allowed with entry_type='market', which fills at the current market price."},
                "allow_no_stop": {"type": "boolean", "description": "action=order/split: open with NO stop at all. Refused by default. Without a stop the broker holds no exit, so nothing but something actively looking at the price can close the position -- and nothing looks between your calls. Only pass it for a deliberately unprotected trade, and never because a stop was inconvenient."},
                "risk_money": {"type": "number", "description": "action=order: size the order so a stop-out costs this much in account currency, instead of naming lots. Needs 'sl' (the entry-to-stop distance IS the risk) and is mutually exclusive with 'volume' and with risk_pct. The lots are rounded DOWN to the symbol's step, so the real risk is never above the number passed."},
                "risk_pct": {"type": "number", "description": "action=order: as risk_money, but as a percentage of account EQUITY. Needs 'sl'. Mutually exclusive with 'volume' and with risk_money."},
                "sl": {"type": "number", "description": "Stop loss price (action=order)."},
                "tp": {"type": "number", "description": "Take profit price (action=order)."},
                "deviation": {"type": "integer", "description": "Max slippage in points."},
                "ticket": {"type": "integer", "description": "Position ticket (action=close, or the single target of action=modify/guard). action=cancel: the PENDING ORDER ticket to remove -- read the tickets from action=orders first."},
                "cancel_all": {"type": "boolean", "description": "action=cancel: remove every pending order this terminal has. Use it to clear resting orders before the session ends; it cancels nothing that has already filled."},
                "tickets": {"type": "array", "items": {"type": "integer"}, "description": "action=modify: several position tickets at once."},
                "all_positions": {"type": "boolean", "description": "action=modify: every open position."},
                "exit_at": {"type": "number", "description": "action=modify: the price to exit this position at. The SL/TP side is chosen from the position direction and the level is nudged outside the broker's minimum stop distance. This is the instant, broker-held exit -- prefer it over watching the price yourself."},
                "guard_action": {"type": "string", "enum": ["arm", "status", "stop", "clear", "events", "ensure"], "description": "action=guard: \"arm\" starts the detached tick-level watcher that closes at trigger_price; \"status\" reports whether it is alive, the rules armed, the price it is seeing and whether any rule cannot be priced; \"events\" returns its log including the measured trigger->fill latency_ms; \"stop\" ends it; \"clear\" drops the rules; \"ensure\" restarts the watcher when rules are still armed but nothing is running, and reports how long the levels went unwatched. Defaults to status. A stopped guard with rules still armed is reported as an ALERT, not a healthy status."},
                "guard_allow_unpriceable": {"type": "boolean", "description": "action=guard (arm): arm even if a rule's symbol has no tick right now. Off by default: a guard on a symbol the CLI cannot price polls in silence and looks exactly like protection, so it is refused unless you know the symbol prices later (e.g. a market that has not opened yet)."},
                "trigger_price": {"type": "number", "description": "action=guard (arm): the price level to act on, e.g. 1.1650 in \"close when EURUSD hits 1.1650\"."},
                "trigger_op": {"type": "string", "enum": [">=", "<="], "description": "action=guard (arm): \">=\" fires at or above the level, \"<=\" at or below. Omit it and the direction is inferred from the live price."},
                "trigger_side": {"type": "string", "enum": ["mid", "bid", "ask"], "description": "action=guard (arm): which price is compared to the level (default mid = (bid+ask)/2, which is what \"the price\" usually means)."},
                "interval_ms": {"type": "integer", "description": "action=guard (arm): how often the watcher reads the tick stream, in milliseconds (default 100). Lowering it does NOT make the guard see more of the market: MEASURED 2026-09-23, one symbol_info_tick call inside Wine costs 334.7 us (~2988/s is the absolute ceiling for a Python poll) and each call returns ONE tick, while the recorded feed carries several a second at its quietest (MEASURED, same day: 282 rows over 60.5 s; another reading counted 618.85 tick/s -- the rate is bursty). The watcher already reads every tick that was RECORDED since the last loop and reports the one that truly crossed, so the interval is how often it decides, not how much it sees."},
                "wait_seconds": {"type": "number", "description": "BLOCKS instead of returning a snapshot, and is how a long-running thing is WATCHED rather than assumed. action=watch: watch the live trade for this many seconds and return the moment something happens (a rule fires, a close is refused, a level is touched and reverted, the watcher stops, the set of open positions changes). action=guard, guard_action=status: same, on the guard. action=status: watch the INSTALL and return the moment it moves (the stage changes, the installer writes more output, or the installer process exits); a finished install returns immediately. Capped at 90 s on every path, so the whole command still fits the 120 s ceiling for one sandbox command. Use it to actually observe an install/exit/live trade instead of reporting that it is 'monitoring' -- every answer carries watched.timed_out and watched.observed_event, so 'nothing happened yet' is never read as 'something happened'."},
                "poll_seconds": {"type": "number", "description": "action=watch/guard/status with wait_seconds: how often to sample while waiting (default 1 s; 2 s for status)."},
                "max_seconds": {"type": "integer", "description": "action=guard (arm/ensure): how long the guard may keep watching before it stops itself. Omit it (the default) and it holds the level for as long as it takes -- there is no time limit. Only set it for a deliberately bounded run; a guard that stops is reported by guard status as alert=guard_not_running, never as protection."},
                "rule": {"type": "object", "description": "action=guard (arm): advanced -- an explicit rule object instead of trigger_* fields. Normally omit it and pass symbol/trigger_price/trigger_op."},
                "comment": {"type": "string", "description": "Order comment."},
                "file": {"type": "string", "description": "Absolute .mq5/.mqh path inside the sandbox (action=compile)."},
                "include": {"type": "string", "description": "MetaEditor include dir (action=compile)."},
                "lines": {"type": "integer", "description": "Log lines to tail (action=logs/experts/status)."},
                "code": {"type": "string", "description": "Raw python using the MetaTrader5 module (action=run)."},
                "wait": {"type": "integer", "description": "Seconds to wait for terminal IPC (action=start)."},
                "timeout": {"type": "integer", "description": "Command timeout override in seconds."},
                "foreground": {"type": "boolean", "description": "action=install: run inline instead of detached. Only safe when no command-timeout ceiling applies."},
                "portable": {"type": "boolean", "description": "action=start: launch the terminal in portable mode so a seeded config/login is used."},
                "broker_installer_url": {"type": "string", "description": "action=install: URL of the BROKER's branded MT5 installer (e.g. https://download.mql5.com/cdn/web/<broker-slug>/mt5/<name>setup.exe). Use it for a broker that 'server' does not already resolve. MetaQuotes' generic terminal ships no broker server list, so broker logins silently never happen (zero 'Network' log lines, then '-10005 IPC timeout' from the bridge)."},
                "broker_dir_name": {"type": "string", "description": "action=install: install directory name the branded installer creates, e.g. 'MetaTrader 5 EXNESS'. Pair with broker_installer_url."},
                "dry_run": {"type": "boolean", "description": "For order/close: validate inputs and report the planned request without sending."},
                "entry": {"type": "number", "description": "action=plan: the entry price to build the stop and target from. The playbook's stop and target are derived from it -- 20 pips below a buy, 140 above -- so the same entry always gives the same plan."},
                "equity": {"type": "number", "description": "action=plan: account equity, so the plan can report the risk in percent of the account instead of only in dollars. Read it from action=account (equity, not balance)."},
                "sl_pips": {"type": "number", "description": "action=plan: stop distance in pips. Default 20, which is the playbook's Gold stop and should not be changed without saying why."},
                "rr": {"type": "number", "description": "action=plan: reward-to-risk target. Default 7 (the playbook's 20-pip stop / 140-pip target). Lower it only deliberately: it is the whole source of the edge in a 20-pip-stop strategy, which loses most of the time by construction."},
                "range_low": {"type": "number", "description": "action=plan: the low of the session range. With range_high it adds the playbook's range sub-levels (25/50/62.5/75/87.5/100/150%) and says which level the entry sits on -- the strategy enters AT a level, so an entry between levels is reported as a violation."},
                "range_high": {"type": "number", "description": "action=plan: the high of the session range. See range_low."},
                "spread": {"type": "number", "description": "action=plan: live spread in price, from action=quote. A 20-pip Gold stop is only ~10x a typical spread, so the plan flags a spread that eats more than a quarter of the stop."},
                "splits": {"type": "integer", "description": "action=split: how many positions to open (2..50, default 10). SPLIT TRADING -- one idea as N equal tickets at one price instead of one big position: same direction, same stop, and the SAME TOTAL RISK, but the exits stop being all-or-nothing. It is how you take 3 off into a run and leave 7 working. It is NOT a grid: it fires once, at one price, with one stop, and never adds tickets as the price goes against you."},
                "group": {"type": "string", "description": "action=split: a label for the tickets so the set can be addressed later (action=close with group/count). action=close: close the tickets of this split group instead of one ticket."},
                "stop_on_failure": {"type": "boolean", "description": "action=split: stop sending tickets after the first rejection (default: try them all and report which filled). Either way a partial split is reported as alert=split_incomplete, never as a filled position."},
                "check_cost": {"type": "boolean", "description": "action=split: report the per-deal cost of N tickets against one position. Commission charged per deal is paid N times, and so are slippage and requotes; spread cost is proportional to volume and is not affected."},
                "limits_action": {"type": "string", "enum": ["show", "set", "clear"], "description": "action=limits: 'show' (default) reports the account's limits in force; 'set' writes them (it MERGES -- limits you do not mention keep their value); 'clear' removes them all. THE ACCOUNT CIRCUIT BREAKER: a stop limits what ONE trade can lose, these limit what the ACCOUNT can lose, which is the thing that actually runs out. Five 'small' positions each risking 2% is 10% on the table and no single ticket's stop prevents it. The limits are enforced at the point an order is sent, so they apply to every caller rather than to whoever remembers them. A refused order reports 'breaches' with the reason and the numbers, and nothing is sent."},
                "max_daily_loss_money": {"type": "number", "description": "action=limits: stop opening new positions for the rest of the day once the day's REALISED loss reaches this much (account currency). Measured from closed deals since midnight BROKER time; deposits and withdrawals are excluded, because a funding transfer is not a trading result. Close what is open by hand -- this refuses entries, it does not liquidate."},
                "max_positions": {"type": "integer", "description": "action=limits: the most open positions the account may hold. A call that would cross it is refused before anything is sent, and a split counts as its full number of tickets, not one."},
                "max_total_risk_money": {"type": "number", "description": "action=limits: the most the WHOLE BOOK may lose if every stop is hit at once, in account currency. This is the number that decides whether a bad day is survivable, and it is not any single position's risk. It is measured from the stops themselves (|entry - stop| x contract x lots), and an order with no stop cannot be counted against it, so such an order is refused while this limit is in force."},
                "max_total_risk_pct": {"type": "number", "description": "action=limits: as max_total_risk_money, but as a percentage of account EQUITY. Needs the account to be readable: with no equity the percentage cannot be checked and is reported as such rather than assumed to pass."},
                "watch_session": {"type": "string", "description": "action=watch: continue ONE observation across calls. Every watch carrying the same name folds its samples into a ledger on the box and returns session.price_path_total (the WHOLE session's high/low/drift, not this call's) plus session.since_last_call (the move since you last looked) and session.elapsed_s. USE THIS WHENEVER YOU FOLLOW A LIVE TRADE: without it each 90-second call reports a different trade's first price and drift, so 'is it working?' cannot be answered across calls. With it the calls are one timeline and you can think between them."},
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult | str:  # type: ignore[override]
        action = str(kwargs.get("action") or "").strip().lower()
        if action not in _ALL_ACTIONS:
            return ToolResult.error(
                f"Unknown action '{action}'. Valid actions: {', '.join(_ALL_ACTIONS)}"
            )

        # --- host-only actions --------------------------------------------- #
        # Answered before the sandbox is even looked for: `plan` is arithmetic
        # over the caller's own numbers, so requiring a live terminal to tell
        # someone what their stop should be would be a worse tool for no gain.
        if action in _HOST_ONLY_ACTIONS:
            return _host_plan(kwargs)

        # --- trading gate -------------------------------------------------- #
        # dry_run is evaluated BEFORE the gate on purpose: previewing the exact
        # command that *would* be sent is a safety feature, so it must work even
        # when live trading is disabled.
        if kwargs.get("dry_run") and action in _TRADING_ACTIONS:
            return json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "action": action,
                    "would_run": build_cli_command(action, kwargs),
                }
            )

        limits_sub = str(kwargs.get("limits_action") or "show").strip().lower()
        if action == "limits" and limits_sub not in ("show", "set", "clear"):
            return ToolResult.error(
                f"Unknown limits_action '{limits_sub}'. Use show, set or clear."
            )

        guard_sub = str(kwargs.get("guard_action") or "status").strip().lower()
        if action == "guard" and guard_sub not in (
            "arm",
            "status",
            "stop",
            "events",
            "clear",
            "ensure",
        ):
            return ToolResult.error(
                f"Unknown guard_action '{guard_sub}'. Use arm, status, stop, clear, "
                "events or ensure."
            )

        if action in _TRADING_ACTIONS and not _trading_enabled():
            # Reading, stopping or clearing a guard places no order, and refusing
            # it would strand a live guard with no way to inspect or disarm it.
            if not (
                (action == "guard" and guard_sub in _GUARD_SAFE_SUBACTIONS)
                or (action == "limits" and limits_sub in _LIMITS_SAFE_SUBACTIONS)
            ):
                return ToolResult.error(
                    f"action='{action}' moves real money and is disabled. Set "
                    "MT5_ALLOW_TRADING=1 in the deployment environment to enable live "
                    "trading, then retry."
                )

        if action == "limits" and limits_sub == "set":
            wanted = [
                flag
                for flag in (
                    "max_daily_loss_money",
                    "max_positions",
                    "max_total_risk_money",
                    "max_total_risk_pct",
                )
                if kwargs.get(flag) is not None
            ]
            if not wanted:
                return ToolResult.error(
                    "action=limits with limits_action='set' needs at least one of "
                    "max_daily_loss_money, max_positions, max_total_risk_money, "
                    "max_total_risk_pct. Use limits_action='clear' to remove them."
                )
            bad = [f for f in wanted if float(kwargs[f]) <= 0]
            if bad:
                return ToolResult.error(
                    "a limit must be positive: "
                    + ", ".join(bad)
                    + ". A limit of zero or less is not a limit. To stop trading, "
                    "close what is open."
                )

        if action == "order":
            if not kwargs.get("symbol") or not kwargs.get("side"):
                return ToolResult.error("action=order requires 'symbol' and 'side'.")
            # A size is required, but it can be lots OR money-at-risk. Sizing by
            # risk is the point of `risk_money`/`risk_pct`: "risk $100 on this"
            # is what a trader says, and the lots are derived from the stop.
            risk_money = kwargs.get("risk_money")
            risk_pct = kwargs.get("risk_pct")
            if kwargs.get("volume") is None and risk_money is None and risk_pct is None:
                return ToolResult.error(
                    "action=order needs a size: 'volume' in lots, or 'risk_money' / "
                    "'risk_pct' together with 'sl'."
                )
            if kwargs.get("volume") is not None and (
                risk_money is not None or risk_pct is not None
            ):
                return ToolResult.error(
                    "action=order: pass 'volume' OR 'risk_money'/'risk_pct', not both "
                    "-- two sizes for one order is two answers to one question."
                )
            if risk_money is not None and risk_pct is not None:
                return ToolResult.error(
                    "action=order: pass 'risk_money' OR 'risk_pct', not both."
                )
            if (risk_money is not None or risk_pct is not None) and kwargs.get("sl") is None:
                return ToolResult.error(
                    "action=order sized by risk needs 'sl': the distance from the "
                    "entry to the stop IS the money at risk."
                )
            entry_type = str(kwargs.get("entry_type") or "market").strip().lower()
            if entry_type not in ("market", "limit", "stop"):
                return ToolResult.error(
                    "action=order: entry_type must be 'market', 'limit' or 'stop'."
                )
            if entry_type != "market" and kwargs.get("price") is None:
                return ToolResult.error(
                    f"action=order with entry_type='{entry_type}' needs 'price' -- "
                    "that price IS the entry."
                )
            if entry_type == "market" and kwargs.get("price") is not None:
                return ToolResult.error(
                    "action=order: entry_type='market' fills at the market and cannot "
                    "honour 'price'. Use entry_type='limit' to enter better than the "
                    "market, or entry_type='stop' to enter on a break through it."
                )
            if not kwargs.get("allow_no_stop") and kwargs.get("sl") is None:
                return ToolResult.error(
                    "action=order with no 'sl' opens a position the BROKER cannot "
                    "close: no server-side exit would exist, so the only thing that "
                    "could close it is something looking at the price -- and nothing "
                    "looks between your calls. Pass sl=<price>, or "
                    "allow_no_stop=true to open it deliberately."
                )
        if action == "cancel":
            if not kwargs.get("cancel_all") and kwargs.get("ticket") is None:
                return ToolResult.error(
                    "action=cancel needs 'ticket' (the pending order to remove), or "
                    "cancel_all=true to remove every pending order. Read them with "
                    "action=orders first."
                )
        if action == "close" and not (kwargs.get("ticket") or kwargs.get("group")):
            return ToolResult.error(
                "action=close requires 'ticket', or 'group' to close part of a split "
                "(with 'count' for a partial)."
            )
        if action == "split":
            if not kwargs.get("symbol") or not kwargs.get("side"):
                return ToolResult.error("action=split requires 'symbol' and 'side'.")
            if not kwargs.get("volume"):
                return ToolResult.error(
                    "action=split requires 'volume' -- the TOTAL lots for the idea, "
                    "which is divided across the tickets."
                )
            splits = int(kwargs.get("splits") or 10)
            if splits < 2:
                return ToolResult.error(
                    "action=split with splits < 2 is just action=order. Use splits=2 "
                    "or more, or use order."
                )
            if not kwargs.get("allow_no_stop") and kwargs.get("sl") is None:
                return ToolResult.error(
                    "action=split with no 'sl' opens a position the BROKER cannot "
                    "close: no server-side exit would exist, so the only thing that "
                    "could close it is something looking at the price -- and nothing "
                    "looks between your calls. Pass sl=<price>, or "
                    "allow_no_stop=true to open it deliberately."
                )
        if action == "modify":
            if not any(
                kwargs.get(key) for key in ("ticket", "tickets", "symbol", "all_positions")
            ):
                return ToolResult.error(
                    "action=modify needs a target: 'ticket', 'tickets', 'symbol' (all "
                    "positions on it), or all_positions=true."
                )
            if not any(
                kwargs.get(key) is not None for key in ("exit_at", "sl", "tp")
            ):
                return ToolResult.error(
                    "action=modify needs the new exit: 'exit_at' (a price to exit at -- "
                    "the side is chosen for you), or explicit 'sl'/'tp'."
                )
        if action == "guard" and guard_sub == "arm":
            _, rule_error = build_guard_rule(kwargs)
            if rule_error:
                return ToolResult.error(rule_error)
        if action == "compile" and not kwargs.get("file"):
            return ToolResult.error("action=compile requires 'file' (absolute path in the sandbox).")
        if action in ("quote", "candles", "symbol") and not (
            kwargs.get("symbol") or kwargs.get("symbols")
        ):
            return ToolResult.error(f"action={action} requires 'symbol'.")

        # Validate the broker installer URL BEFORE touching the sandbox. A mistyped
        # slug (e.g. a dropped TLD) is a permanent 404, and discovering it only
        # after a ~2-minute Wine install wastes the run and misreports the cause as
        # a download failure. Rejecting it here makes the typo the error message.
        if action == "install":
            url_error = validate_broker_installer_url(
                str(kwargs.get("broker_installer_url") or "")
            )
            if url_error:
                return ToolResult.error(url_error)

        sandbox = _sandbox_tool(getattr(self, "_ctx", None))
        if sandbox is None:
            return ToolResult.error(
                "No execution sandbox is configured. MT5 must run inside a sandbox "
                "(novita/vps/runloop/daytona) — this tool never installs Wine or a "
                "terminal on the application host."
            )

        command = build_cli_command(action, kwargs)
        # Always refresh the CLI before using it so a fixed bridge ships without
        # rebuilding the sandbox or the image.
        #
        # Stdout is discarded (the bootstrap's last step prints the doctor JSON,
        # which must not be mistaken for this action's result) but the bootstrap's
        # STDERR is kept and echoed at the end: it carries the "could not fetch
        # the expected CLI version" warning, and that warning is exactly what a
        # "no JSON result" failure needs to be interpretable. Trailing stderr
        # cannot confuse the parser, which scans for the first balanced JSON.
        full_command = (
            f"{bootstrap_command()} >/dev/null 2>{_BOOTSTRAP_LOG} || true; "
            f"{command}; tail -c 400 {_BOOTSTRAP_LOG} 1>&2"
        )
        timeout = int(kwargs.get("timeout") or _TIMEOUTS.get(action, _DEFAULT_TIMEOUT))
        # A WATCH runs for as long as the caller asked -- on the guard, on a live
        # trade (``watch``), or on an install (``status``) -- so the command
        # timeout has to cover the wait. Otherwise the wait is killed mid-flight
        # by the sandbox and returns no JSON at all, which is the one outcome a
        # watch exists to avoid. The wait is capped CLI-side at
        # WATCH_MAX_WAIT_SECONDS / GUARD_MAX_WAIT_SECONDS (90 s either way), so
        # ``wait + 30`` lands exactly on the 120 s ceiling for one sandbox
        # command -- and the slack is for the bootstrap fetch plus, on the two
        # Wine-side paths, the re-exec into Wine.
        if action in ("guard", "status", "watch"):
            wait = float(kwargs.get("wait_seconds") or 0.0)
            if wait > 0:
                timeout = max(timeout, int(wait) + 30)

        try:
            rendered = await sandbox.execute(
                action="run",
                command=full_command,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            # Novita raises for any non-zero exit status and drops stdout. The CLI
            # now always exits 0, but an older cached CLI (or a hard transport
            # failure) can still land here. If the exception text carries our
            # refusal, honour it and auto-provision rather than surfacing a
            # traceback the model can only guess at.
            detail = f"{type(exc).__name__}: {exc}"
            if "not_installed" in detail or "requires the installed MT5 chain" in detail:
                return await self._auto_provision(
                    sandbox,
                    action,
                    {"missing": [], "error": detail},
                )
            logger.warning("mt5_sandbox: sandbox call failed ({})", exc)
            return ToolResult.error(
                f"MT5 sandbox call failed: {detail}. This is a transport error, not a "
                "problem with the MQL5 source: do NOT ask the user to compile the .mq5 "
                "locally. Retry, or call action='status' to see provisioning state."
            )

        rendered_text = str(rendered)
        payload = _parse_payload(rendered_text)

        if payload is None:
            # novita_sandbox converts a non-zero exit into a plain text error and
            # DROPS stdout, so the CLI's JSON can be missing even though the run
            # produced one. If that text is our chain refusal, treat it as the
            # structured result it was and provision — otherwise the model receives
            # "produced no JSON result" and concludes the compiler is broken.
            lowered = rendered_text.lower()
            if (
                "requires the installed mt5 chain" in lowered
                or "not_installed" in lowered
                or "is missing: wine" in lowered
            ):
                return await self._auto_provision(
                    sandbox,
                    action,
                    {"missing": _missing_from_text(rendered_text), "error": rendered_text[-400:]},
                )
            # The CLI prints JSON last; if nothing parsed, the command itself blew
            # up (no sandbox python, curl failure, ...). Hand the raw tail back
            # with the bootstrap hint so the model can self-correct.
            tail = rendered_text[-2000:]
            return ToolResult.error(
                "MT5 command produced no JSON result. Raw output tail:\n"
                f"{tail}\n\nHint: run action='install' first, then action='start'. "
                "This is a transport problem, not an MQL5 source error — do NOT tell "
                "the user to compile the .mq5 locally."
            )

        # Never echo a password back, even if a broker/library logged it.
        if "password" in payload:
            payload["password"] = "***"

        # A position with no SL and no TP has no server-side exit at all: the only
        # way it can ever close is a model turn calling `close`, which is the exact
        # reason a "close at 1.1650" instruction was not honoured instantly. The
        # hint is attached where the model is already looking at its position.
        if action == "positions" and isinstance(payload.get("positions"), list):
            unprotected = [
                int(p.get("ticket") or 0)
                for p in payload["positions"]
                if not float(p.get("sl") or 0.0) and not float(p.get("tp") or 0.0)
            ]
            payload["positions_without_a_server_side_exit"] = unprotected
            # Whether ANYTHING is watching a price is part of reading open risk,
            # so it is answered in the same payload rather than left to a second
            # call the model may never make. `guard` comes from the CLI (read from
            # the watcher's own state file); "none" is a fact, not an absence.
            guard = payload.get("guard") if isinstance(payload.get("guard"), dict) else {}
            watched = bool(guard.get("live")) and int(guard.get("rules_armed") or 0) > 0
            payload["protection"] = {
                "server_side_exit": [
                    int(p.get("ticket") or 0)
                    for p in payload["positions"]
                    if float(p.get("sl") or 0.0) or float(p.get("tp") or 0.0)
                ],
                "guard_live": bool(guard.get("live")),
                "guard_rules_armed": int(guard.get("rules_armed") or 0),
                "guard_alert": guard.get("alert"),
                # A fired rule whose close the broker REFUSED is not covered
                # ground: the level was touched, the guard did its job, and the
                # position is still open while the watcher retries. Carried in the
                # same payload as the positions, because it changes what the
                # caller should do next.
                "guard_retrying_close": guard.get("retrying") or [],
                "guard_gave_up_close": guard.get("gave_up") or [],
                "unprotected_tickets": unprotected,
            }
            if unprotected:
                payload["hint"] = (
                    "These positions have no SL/TP, so nothing but an agent turn can "
                    "close them -- the broker will NOT exit them at a price while you "
                    "are not calling tools. To honour 'close when it hits X', arm a "
                    "server-side exit now: action='modify' with exit_at=X (the broker "
                    "then holds the level and fires it instantly)"
                    + (
                        "; a tick-level guard IS running and covers the levels armed "
                        "in it."
                        if watched
                        else ", or action='guard' for a condition the broker cannot "
                        "hold. No tick-level guard is running right now, so a guard "
                        "rule is NOT currently protecting anything."
                    )
                )
            if guard.get("alert") == "guard_not_running":
                payload["warning"] = (
                    f"GUARD NOT WATCHING: {guard.get('rules_armed')} guard rule(s) are "
                    "armed but the watcher is not running"
                    + (
                        f" (it stopped: {guard.get('exit_reason')})"
                        if guard.get("exit_reason")
                        else ""
                    )
                    + ". Nothing will close at those levels until it is restarted. "
                    "Restart it with action='guard', guard_action='ensure', then "
                    "re-read positions before trusting those levels."
                )
            elif guard.get("alert") == "close_gave_up":
                payload["warning"] = (
                    "GUARD COULD NOT CLOSE: the level was touched, the watcher sent "
                    "the close, and the broker refused it repeatedly -- these "
                    "position(s) are STILL OPEN. "
                    + "; ".join(
                        f"{g.get('rule_id')} on {g.get('symbol')}: "
                        f"{g.get('attempts')} refusal(s), retcodes {g.get('retcodes')}"
                        for g in (guard.get("gave_up") or [])
                    )
                    + f". The rule stays armed and retries in "
                    f"{(guard.get('gave_up') or [{}])[0].get('next_window_in_s')} s "
                    "(a closed market is the usual cause). Read action='guard' "
                    "guard_action='events' for close_gave_up, and close by hand "
                    "(action='close') if you need out now."
                )
            elif guard.get("alert") == "close_retrying":
                payload["warning"] = (
                    "GUARD RETRYING A REFUSED CLOSE: the watcher is up and the "
                    "broker refused its exit, so the position(s) are NOT out yet. "
                    + "; ".join(
                        f"{r.get('rule_id')} on {r.get('symbol')}: "
                        f"{r.get('attempts')} attempt(s) over {r.get('trying_for_s')} s, "
                        f"next in {r.get('retry_in_s')} s"
                        for r in (guard.get("retrying") or [])
                    )
                    + ". It keeps retrying until the broker accepts or the deadline "
                    "passes; poll guard events for 'fired' or 'close_gave_up'."
                )
            elif guard.get("alert") == "rule_unpriceable":
                payload["warning"] = (
                    "GUARD BLIND: the watcher is running but cannot price one of its "
                    "symbols, so that rule can never fire. Re-read action='guard' "
                    "guard_action='events' for which symbol went dark."
                )

        # action='install' is detached and reports "installing" immediately. Rather
        # than handing that back and hoping the model comes back to poll (the stall
        # this fixes), wait for a terminal stage here so one call does the whole job.
        if action == "install" and str(payload.get("stage") or "") == "installing":
            waited = await self._wait_for_install(sandbox)
            if waited:
                if "password" in waited:
                    waited["password"] = "***"
                payload = waited

        if payload.get("ok") is False:
            # The chain-not-installed case must not be returned as a plain error.
            # Given an error, models consistently "helpfully" hand the .mq5 back to
            # the user to compile locally — the exact failure being fixed. So the
            # tool PROVISIONS for them: kick off the detached install in the same
            # call and report that it started, which leaves "install first" as the
            # only forward path and nothing to negotiate around.
            if payload.get("stage") == "not_installed" and action != "install":
                return await self._auto_provision(sandbox, action, payload)
            # The OTHER silent dead-end: a terminal that cannot resolve the account's
            # server name. MT5 does not error, it just never attempts the connection,
            # so without this the run blocks on the bridge's IPC timeout and looks
            # frozen. The CLI now detects the mismatch in under a second and names the
            # build to install, so the tool installs it and replays the action.
            if (
                payload.get("failure") == "server_not_in_terminal"
                and action in ("start", "login")
            ):
                return await self._auto_install_for_broker(
                    sandbox, action, kwargs, payload
                )
            return ToolResult.error(json.dumps(payload))

        return json.dumps(payload)

    async def _wait_for_install(self, sandbox: Any) -> dict[str, Any]:
        """Poll ``status`` until provisioning reaches a terminal stage.

        WHY THIS EXISTS: ``install`` is detached, so the tool used to hand the model
        an "installing, go poll" instruction and stop there. A model that then asked
        the user "should I check again?" produced exactly the stall this fixes — the
        work was still progressing, but nothing advanced it without a human nudge.
        Polling here keeps the loop closed inside one tool call: the agent owns the
        wait instead of delegating it back to the operator.

        Bounded on purpose: a full install is ~2-25 min, and this must never hang a
        run forever. ``MT5_INSTALL_WAIT_SECONDS`` (default 1500 s / 25 min) caps it.
        """
        budget = int(os.getenv("MT5_INSTALL_WAIT_SECONDS", "1500") or 1500)
        interval = int(os.getenv("MT5_INSTALL_POLL_SECONDS", "30") or 30)
        deadline = time.monotonic() + budget
        last: dict[str, Any] = {}

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                rendered = await sandbox.execute(
                    action="run",
                    command=(
                        f"{bootstrap_command()} >/dev/null 2>&1 || true; "
                        f"{build_cli_command('status', {})}"
                    ),
                    timeout=min(_TIMEOUTS["status"], max(30, int(remaining))),
                )
            except Exception as exc:  # noqa: BLE001 - keep polling on transport blips
                logger.warning("mt5_sandbox: install poll failed ({})", exc)
                await asyncio.sleep(interval)
                continue

            payload = _parse_payload(str(rendered))
            if payload:
                last = payload
                stage = str(payload.get("stage") or "")
                if stage in _TERMINAL_STAGES:
                    return payload
            await asyncio.sleep(interval)

        # Out of budget but not failed — report the last stage honestly so the model
        # knows this is "still working", not "broken".
        if last:
            last = dict(last)
            last["poll_timeout"] = True
            last["message"] = (
                f"Install still running after {budget}s (last stage: "
                f"{last.get('stage')!r}). It is progressing, not failed — poll "
                "action='status' again rather than restarting the install."
            )
            return last
        # No poll completed at all (a zero budget, or every attempt failing): the
        # install is still the only thing that can be true, so say "installing"
        # rather than "unknown" -- "unknown" reads as "nothing is happening".
        return {
            "stage": "installing",
            "poll_timeout": True,
            "message": (
                "The install was started and no status poll completed yet. It is "
                "progressing, not failed -- poll action='status' again."
            ),
        }

    async def _auto_install_for_broker(
        self,
        sandbox: Any,
        action: str,
        kwargs: dict[str, Any],
        refusal: dict[str, Any],
    ) -> str:
        """Install the MT5 build that can resolve this server, then replay the action.

        WHY THIS EXISTS: the server a user gives decides which broker build is needed,
        and MT5 refuses to say so -- an unresolvable server name is skipped silently,
        so the bridge blocks on its IPC timeout and the account call looks frozen.
        Measured 2026-09-22: a MetaQuotes-Demo login against the Exness terminal sat
        there for a minute with zero ``Network`` lines in the terminal log.

        The CLI now detects that mismatch in under a second and returns
        ``failure: server_not_in_terminal`` with the exact install to run, so the tool
        runs it (once) and replays the original action instead of bouncing the user
        back to "try installing your broker's terminal".
        """
        remedy = refusal.get("remedy") or {}
        server = kwargs.get("server") or remedy.get("server")
        install_kwargs: dict[str, Any] = {"server": server}
        for key in ("broker_installer_url", "broker_dir_name"):
            value = remedy.get(key)
            # The placeholder URL in a refusal means "this broker is not registered
            # yet" -- passing it through would install nothing useful.
            if value and "<broker-slug>" not in str(value):
                install_kwargs[key] = value

        if not install_kwargs.get("broker_installer_url") and not _server_is_known(server):
            return ToolResult.error(
                json.dumps(
                    {
                        **refusal,
                        "message": (
                            f"The sandbox terminal cannot resolve server {server!r} and "
                            "this broker is not in the agent's built-in registry, so the "
                            "installer URL has to come from the broker's own "
                            "'Download MT5' page (it is in the link to their MT5 "
                            "download), or the deployment can register it once with "
                            "MT5_BROKER_BUILDS='<server-prefix>|<url>|<install dir>'."
                        ),
                        "next": (
                            "Ask the user for their broker's MT5 download link, then "
                            f"retry action='install' with server={server!r} and "
                            "broker_installer_url=<that link>."
                        ),
                    }
                )
            )

        kick = build_cli_command("install", install_kwargs)
        try:
            await sandbox.execute(
                action="run",
                command=f"{bootstrap_command()} >/dev/null 2>&1 || true; {kick}",
                timeout=_TIMEOUTS["install"],
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            logger.warning("mt5_sandbox: broker install failed ({})", exc)
            return ToolResult.error(
                f"Installing the MT5 build for server {server!r} failed to start: "
                f"{type(exc).__name__}: {exc}. Retry action='install' with "
                f"server={server!r}."
            )

        result = await self._wait_for_install(sandbox)
        stage = str(result.get("stage") or "")
        if stage != "done":
            return ToolResult.error(
                json.dumps(
                    {
                        "ok": False,
                        "stage": stage or "installing",
                        "failure": "broker_install_incomplete",
                        "requested_server": server,
                        "message": (
                            f"The terminal this sandbox has cannot resolve server "
                            f"{server!r}, so the matching MT5 build is being installed. "
                            f"It is still running (last stage: {stage!r})."
                        ),
                        "next": (
                            "Poll mt5_sandbox(action='status') until stage='done', then "
                            f"retry action='{action}' with server={server!r}."
                        ),
                    }
                )
            )

        # Replay the original action against the terminal that now matches the server.
        replay_kwargs = dict(kwargs)
        retry_command = (
            f"{bootstrap_command()} >/dev/null 2>&1 || true; "
            f"{build_cli_command(action, replay_kwargs)}"
        )
        try:
            rendered = await sandbox.execute(
                action="run",
                command=retry_command,
                timeout=int(kwargs.get("timeout") or _TIMEOUTS.get(action, _DEFAULT_TIMEOUT)),
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            return ToolResult.error(
                f"The matching MT5 build is installed, but retrying action='{action}' "
                f"failed: {type(exc).__name__}: {exc}. Retry it now — no further install "
                "is needed."
            )

        payload = _parse_payload(str(rendered)) or {}
        if "password" in payload:
            payload["password"] = "***"
        payload["installed_build_for_server"] = server
        payload["auto_installed_broker_terminal"] = True
        payload.setdefault(
            "message",
            f"The sandbox had a terminal that could not resolve server {server!r}; the "
            "tool installed the matching MT5 build itself and retried the action. No "
            "user action was needed.",
        )
        if payload.get("ok") is False:
            return ToolResult.error(json.dumps(payload))
        return json.dumps(payload)

    async def _auto_provision(
        self, sandbox: Any, action: str, refusal: dict[str, Any]
    ) -> str:
        """Start the detached install, then WAIT for it and continue.

        Handing back an error is what produced the "the compiler is unavailable,
        please compile this locally" refusals: a model offered a concrete
        alternative always takes it. So the tool does the required first step
        itself. It also now polls to a terminal stage instead of returning
        "installing" and relying on the model (or the user) to come back — that
        hand-off is what caused runs to stall mid-provision.
        """
        missing = ", ".join(refusal.get("missing") or []) or "the MT5 chain"
        kick = build_cli_command("install", {})
        kick_command = f"{bootstrap_command()} >/dev/null 2>&1 || true; {kick}"
        try:
            await sandbox.execute(
                action="run", command=kick_command, timeout=_TIMEOUTS["install"]
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            logger.warning("mt5_sandbox: auto-provision failed ({})", exc)
            return ToolResult.error(
                f"MT5 is not installed ({missing}) and starting the install failed: "
                f"{type(exc).__name__}: {exc}. Retry action='install'. Do NOT ask the "
                "user to compile the .mq5 by hand — the sandbox can build it."
            )

        # Wait for a terminal stage before answering, so the caller does not have to
        # re-prompt to make progress.
        result = await self._wait_for_install(sandbox)
        stage = str(result.get("stage") or "")

        if stage == "done":
            return json.dumps(
                {
                    "ok": True,
                    "stage": "done",
                    "auto_provisioned": True,
                    "requested_action": action,
                    "was_missing": refusal.get("missing") or [],
                    "message": (
                        f"MT5 was not installed ({missing}). The tool installed the "
                        f"Wine + MetaTrader 5 + MetaEditor chain itself and it is now "
                        f"ready — no user action was needed."
                    ),
                    "next": f"Retry action='{action}' now.",
                }
            )

        if stage == "failed":
            return ToolResult.error(
                f"MT5 install failed in the sandbox: "
                f"{result.get('message') or 'unknown error'}. Provisioning started "
                "automatically; the failure is a real install error, not a missing "
                "step. Read the log tail before retrying action='install'."
            )

        # Still running after the budget (or an unrecognised stage): report honestly
        # and tell the model to keep polling rather than restart anything.
        return json.dumps(
            {
                "ok": False,
                "stage": stage or "installing",
                "auto_provisioned": True,
                "requested_action": action,
                "was_missing": refusal.get("missing") or [],
                "message": (
                    f"MT5 was not installed ({missing}), so the tool started the "
                    "Wine + MetaTrader 5 + MetaEditor install and has been polling it. "
                    f"It is still running after the wait budget (last stage: {stage!r})."
                ),
                "next": (
                    "Poll mt5_sandbox(action='status') until stage='done', then retry "
                    f"action='{action}'. Do NOT edit, rewrite, or hand back the .mq5 "
                    "while provisioning is in progress, and do NOT tell the user to "
                    "compile it locally — this sandbox compiles it."
                ),
                "do_not": [
                    "ask the user to compile in a local MetaEditor",
                    "return 'corrected' .mq5 source instead of compiling it",
                    "claim the compiler is unavailable",
                    "re-run action='install' (already running)",
                ],
            }
        )