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
_CLI_VERSION = "2026-09-22.6"

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
_TRADING_ACTIONS = frozenset({"order", "close", "close_all"})

#: Actions that only read state. These never require the trading opt-in.
_READ_ONLY_ACTIONS = frozenset(
    {
        "status", "doctor", "account", "quote", "candles", "positions", "orders",
        "history", "symbol", "symbols", "logs", "experts", "run",
    }
)

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
_KNOWN_SERVER_PREFIXES = ("metaquotes", "exness")


def _server_is_known(server: str | None) -> bool:
    name = (server or "").strip().lower()
    return bool(name) and any(name.startswith(prefix) for prefix in _KNOWN_SERVER_PREFIXES)


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
            "--volume", str(float(kwargs.get("volume") or 0)),
        ]
        for flag in ("sl", "tp"):
            if kwargs.get(flag) is not None:
                parts += [f"--{flag}", str(float(kwargs[flag]))]
        if kwargs.get("deviation") is not None:
            parts += ["--deviation", str(int(kwargs["deviation"]))]
        if kwargs.get("comment"):
            parts += ["--comment", _sh(kwargs["comment"])]
    elif action == "close":
        parts += ["--ticket", str(int(kwargs.get("ticket") or 0))]
        if kwargs.get("volume") is not None:
            parts += ["--volume", str(float(kwargs["volume"]))]
        if kwargs.get("deviation") is not None:
            parts += ["--deviation", str(int(kwargs["deviation"]))]
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
            "Trading actions (order, close, close_all) require MT5_ALLOW_TRADING to be "
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
                "symbols": {"type": "string", "description": "Space/comma separated symbols for action=quote."},
                "timeframe": {"type": "string", "description": "M1..MN1 (action=candles)."},
                "count": {"type": "integer", "description": "Number of bars (action=candles)."},
                "days": {"type": "integer", "description": "History window in days (action=history)."},
                "side": {"type": "string", "enum": ["buy", "sell"], "description": "Order direction (action=order)."},
                "volume": {"type": "number", "description": "Lots (action=order/close)."},
                "sl": {"type": "number", "description": "Stop loss price (action=order)."},
                "tp": {"type": "number", "description": "Take profit price (action=order)."},
                "deviation": {"type": "integer", "description": "Max slippage in points."},
                "ticket": {"type": "integer", "description": "Position ticket (action=close)."},
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
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult | str:  # type: ignore[override]
        action = str(kwargs.get("action") or "").strip().lower()
        if action not in _ALL_ACTIONS:
            return ToolResult.error(
                f"Unknown action '{action}'. Valid actions: {', '.join(_ALL_ACTIONS)}"
            )

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

        if action in _TRADING_ACTIONS and not _trading_enabled():
            return ToolResult.error(
                f"action='{action}' moves real money and is disabled. Set "
                "MT5_ALLOW_TRADING=1 in the deployment environment to enable live "
                "trading, then retry."
            )

        if action == "order":
            if not kwargs.get("symbol") or not kwargs.get("side"):
                return ToolResult.error("action=order requires 'symbol' and 'side'.")
            if not kwargs.get("volume"):
                return ToolResult.error("action=order requires a positive 'volume'.")
        if action == "close" and not kwargs.get("ticket"):
            return ToolResult.error("action=close requires 'ticket'.")
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