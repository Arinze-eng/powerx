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

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext

#: Raw GitHub base for the two sandbox-side scripts. The sandbox has internet
#: access (the Novita tool creates boxes with ``allow_internet_access=True``), so
#: bootstrapping by URL avoids shipping megabytes of tooling in the image.
_RAW_BASE = os.getenv(
    "MT5_SCRIPT_RAW_BASE",
    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts",
)

#: Where the CLI and the Wine prefix live inside the sandbox.
_MT5_HOME = "$HOME/.mt5"
_CLI_PATH = f"{_MT5_HOME}/bin/mt5_cli.py"
_INSTALLER_PATH = f"{_MT5_HOME}/bin/install_mt5_sandbox.sh"

#: Actions that move money. Blocked unless explicitly enabled.
_TRADING_ACTIONS = frozenset({"order", "close", "close_all"})

#: Actions that only read state. These never require the trading opt-in.
_READ_ONLY_ACTIONS = frozenset(
    {
        "doctor", "account", "quote", "candles", "positions", "orders",
        "history", "symbol", "logs", "experts", "run",
    }
)

_ALL_ACTIONS = sorted(
    _READ_ONLY_ACTIONS | _TRADING_ACTIONS | {"install", "start", "stop", "login", "compile"}
)

#: Generous per-action timeouts. Installing Wine + MT5 genuinely takes minutes;
#: everything else should be quick once the terminal is warm.
_TIMEOUTS: dict[str, int] = {
    "install": 2400,
    "start": 300,
    "stop": 60,
    "doctor": 120,
    "compile": 360,
    "candles": 180,
    "history": 180,
    "run": 180,
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
    try:
        items = registry.values() if isinstance(registry, dict) else registry
        for tool in items:
            name = getattr(tool, "name", "")
            if name in ("novita_sandbox", "vps_exec", "runloop_sandbox", "daytona_sandbox"):
                return tool
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def bootstrap_command() -> str:
    """Idempotently fetch the CLI + installer into the sandbox."""
    return (
        f"mkdir -p {_MT5_HOME}/bin && "
        f"curl -fsSL {_RAW_BASE}/mt5_cli.py -o {_CLI_PATH} && "
        f"curl -fsSL {_RAW_BASE}/install_mt5_sandbox.sh -o {_INSTALLER_PATH} && "
        f"chmod +x {_CLI_PATH} {_INSTALLER_PATH} && "
        f"python3 {_CLI_PATH} doctor"
    )


def _sh(value: Any) -> str:
    return shlex.quote(str(value))


def build_cli_command(action: str, kwargs: dict[str, Any]) -> str:
    """Translate tool kwargs into an ``mt5_cli.py`` invocation."""
    parts = ["python3", _CLI_PATH, action]

    if action == "install":
        parts += ["--script", _INSTALLER_PATH]
        if kwargs.get("timeout"):
            parts += ["--timeout", str(int(kwargs["timeout"]))]
    elif action == "start":
        parts += ["--wait", str(int(kwargs.get("wait") or 180))]
        # Credentials can be supplied here so the terminal auto-connects on boot,
        # which is the only way to get IPC without a GUI login.
        for flag in ("login", "password", "server"):
            if kwargs.get(flag) not in (None, ""):
                parts += [f"--{flag}", _sh(kwargs[flag])]
        if kwargs.get("portable"):
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
        """Enabled only when an execution sandbox is available.

        Without a sandbox there is nowhere safe to run Wine/MT5, and this tool
        refuses to fall back to the host by design.
        """
        return _sandbox_tool(ctx) is not None

    @property
    def name(self) -> str:
        return "mt5_sandbox"

    @property
    def description(self) -> str:
        return (
            "MetaTrader 5 in the user's execution sandbox. NEVER runs Wine or the MT5 "
            "terminal on the application host. "
            "Workflow: action='install' (Wine + Xvfb + MT5 + python bridge inside the "
            "sandbox, takes a few minutes) -> action='start' -> action='login' -> then "
            "quotes/candles/orders. "
            "Read/fix loop: use 'compile' to build an .mq5 with MetaEditor (returns the "
            "compiler errors), and 'logs'/'experts' to tail the terminal and Experts "
            "journal. "
            f"Actions: {', '.join(_ALL_ACTIONS)}. "
            "Trading actions (order, close, close_all) require MT5_ALLOW_TRADING to be "
            "enabled and return the broker retcode; a rejected order is reported with "
            "code 3 and its reason rather than raising. "
            "All output is JSON: read it, fix errors, retry."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": _ALL_ACTIONS},
                "login": {"type": "integer", "description": "MT5 account number (action=login)."},
                "password": {"type": "string", "description": "MT5 password (action=login)."},
                "server": {"type": "string", "description": "Broker server, e.g. 'ICMarkets-Demo' (action=login)."},
                "path": {"type": "string", "description": "Explicit terminal64.exe path override (action=login)."},
                "symbol": {"type": "string", "description": "Trading symbol, e.g. EURUSD (quote/candles/symbol/order)."},
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
                "lines": {"type": "integer", "description": "Log lines to tail (action=logs/experts)."},
                "code": {"type": "string", "description": "Raw python using the MetaTrader5 module (action=run)."},
                "wait": {"type": "integer", "description": "Seconds to wait for terminal IPC (action=start)."},
                "timeout": {"type": "integer", "description": "Command timeout override in seconds."},
                "portable": {"type": "boolean", "description": "action=start: launch the terminal in portable mode so a seeded config/login is used."},
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
        full_command = f"{bootstrap_command()} >/dev/null 2>&1 || true; {command}"
        timeout = int(kwargs.get("timeout") or _TIMEOUTS.get(action, _DEFAULT_TIMEOUT))

        try:
            rendered = await sandbox.execute(
                action="run",
                command=full_command,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            logger.warning("mt5_sandbox: sandbox call failed ({})", exc)
            return ToolResult.error(f"MT5 sandbox call failed: {type(exc).__name__}: {exc}")

        rendered_text = str(rendered)
        payload = _parse_payload(rendered_text)

        if payload is None:
            # The CLI prints JSON last; if nothing parsed, the command itself blew
            # up (no sandbox python, curl failure, ...). Hand the raw tail back
            # with the bootstrap hint so the model can self-correct.
            tail = rendered_text[-2000:]
            return ToolResult.error(
                "MT5 command produced no JSON result. Raw output tail:\n"
                f"{tail}\n\nHint: run action='install' first, then action='start'."
            )

        # Never echo a password back, even if a broker/library logged it.
        if "password" in payload:
            payload["password"] = "***"

        if payload.get("ok") is False:
            return ToolResult.error(json.dumps(payload))

        return json.dumps(payload)