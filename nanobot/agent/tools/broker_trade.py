"""Broker trading tool -- the "Brokers" method, separate from MT5/Wine.

WHY THIS EXISTS AS A SEPARATE TOOL
==================================
There are two ways to reach a broker from nanobot, and they are deliberately
different code paths:

``mt5_sandbox``
    The original method. It provisions a Wine + MT5 + bridge chain inside the
    execution sandbox and is driven by the deployment's own broker
    configuration. Its behaviour is unchanged by this module.

``broker_trade`` (this file)
    The method a *user* sets up themselves: they type their own account number,
    password and server in Settings -> Brokers, those credentials are stored
    encrypted per user (see ``nanobot.trading.broker_credentials``), and this
    tool drives that account. Nothing here reads or writes the MT5 sandbox
    tool's state, and turning this method off does not touch that one.

It is a *thin forwarder*, exactly like ``media`` and ``engineering_draw``: the
only thing it adds over ``scripts/mt5_cli.py`` is credential resolution and the
per-call feature gate. The command construction, the bootstrap and the JSON
extraction are imported from ``mt5_sandbox`` rather than copied, because two
copies of that plumbing would drift and the sandbox contract is subtle (the CLI
is fetched into the sandbox on every call so a fixed bridge ships without an
image rebuild).
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from nanobot.access_policy import access_policy
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext


def _sandbox_tool(ctx: ToolContext | None) -> Any:
    """Find the configured execution sandbox tool.

    Delegated to ``mt5_sandbox``'s resolver rather than copied: the subtlety in
    it (the registry is not a dict, so a name lookup has to come first or a
    fully wired deployment is reported as "no sandbox") is already solved
    there, and a second copy would drift.
    """
    try:
        from nanobot.agent.tools.mt5_sandbox import _sandbox_tool as _resolve

        return _resolve(ctx)
    except Exception:  # noqa: BLE001 - no sandbox is a reported refusal, not a crash
        return None

#: The CLI actions this tool exposes, and the tool action each maps to. A
#: caller may also pass ``cli_action`` to reach an action not listed here.
_ACTIONS = (
    "connect",
    "account",
    "positions",
    "order",
    "close",
    "quote",
    "symbols",
    "install",
    "status",
    "doctor",
)

#: Which stored account a call applies to when the caller names none.
_DEFAULT_ACCOUNT = ""


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": (
                    "'connect' attaches the terminal to the user's broker account and "
                    "reports the account (use for 'connect my broker', 'log in to my "
                    "account'); 'account' shows balance/equity/margin; 'positions' lists "
                    "open positions; 'order' places a market or pending order; 'close' "
                    "closes a position by ticket or symbol; 'quote' reads current prices; "
                    "'symbols' lists tradable symbols; 'install' provisions the broker's "
                    "own MT5 build for the account's server (needed once per broker); "
                    "'status' reports terminal/install state; 'doctor' reports the "
                    "sandbox-side MT5 chain health."
                ),
            },
            "symbol": {
                "type": "string",
                "description": "Instrument, e.g. XAUUSD, EURUSD, BTCUSD. Required for order/close/quote.",
            },
            "side": {
                "type": "string",
                "enum": ["buy", "sell"],
                "description": "Order direction (for 'order').",
            },
            "volume": {
                "type": "number",
                "description": "Lot size for 'order'. Omit to let the broker's minimum be used.",
            },
            "ticket": {
                "type": "integer",
                "description": "Position ticket (for 'close').",
            },
            "login": {
                "type": "string",
                "description": (
                    "Which stored account to use, by account number. Omit to use the "
                    "user's default broker account."
                ),
            },
            "sl": {
                "type": "number",
                "description": "Stop loss price for 'order'.",
            },
            "tp": {
                "type": "number",
                "description": "Take profit price for 'order'.",
            },
            "comment": {
                "type": "string",
                "description": "Order comment.",
            },
            "wait_seconds": {
                "type": "number",
                "description": (
                    "For 'connect': how long to wait for authorization. Defaults to 90; "
                    "capped by the sandbox command ceiling."
                ),
            },
        },
        "required": ["action"],
    }
)
class BrokerTradeTool(Tool):
    """Trade a user's own broker account with credentials they stored themselves."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "broker_trade"

    @property
    def description(self) -> str:
        return (
            "Trade the user's own MT5 broker account using the credentials they saved in "
            "Settings -> Brokers (their account number, password and server, e.g. Exness or "
            "Deriv). Use this when the user asks to trade, check their balance, list "
            "positions, or connect their broker account. This is separate from the "
            "mt5_sandbox method: it needs no per-deployment broker setup, because the "
            "account's own server decides which terminal build is used. If the user has "
            "not saved a broker account yet, tell them to add one in Settings -> Brokers."
        )

    @classmethod
    def enabled(cls, ctx: Any = None) -> bool:
        """Whether the deployment offers the Brokers method at all.

        Checked at startup, so it only answers for a switch that was already
        off when the gateway booted; ``execute`` re-checks live. The gate is the
        same policy the admin page writes, so one switch covers both.
        """
        return access_policy().brokers().allowed

    # -- identity ---------------------------------------------------------- #

    @staticmethod
    def _current_user_key() -> str:
        """The nanobot user key for the turn that is running, or ``""``.

        Mirrors the key shape the access policy uses (see
        ``nanobot.access_policy.turn_user_key``) so one identity resolves both
        the feature gate and the credential row. An empty answer is a real
        answer: without it there is no account to trade, and the tool refuses
        rather than falling back to a shared deployment account.
        """
        try:
            from nanobot.access_policy import turn_user_key
            from nanobot.agent.tools.context import current_request_context

            ctx = current_request_context()
            if ctx is None:
                return ""
            metadata = getattr(ctx, "metadata", None) or {}
            attributes = getattr(ctx, "attributes", None) or {}
            channel = metadata.get("channel") or attributes.get("channel")
            chat_id = (
                metadata.get("chat_id")
                or metadata.get("telegram_user_id")
                or metadata.get("user_id")
                or attributes.get("chat_id")
                or attributes.get("telegram_user_id")
            )
            return turn_user_key(
                channel=str(channel or ""),
                chat_id=chat_id,
                metadata=metadata,
            )
        except Exception:  # noqa: BLE001 - an unidentified caller is not a crash
            logger.debug("broker_trade: could not resolve the calling user key")
            return ""

    async def _account_credentials(self, login: str = "") -> dict[str, str] | None:
        """The stored credentials for the calling user, or None if none exist."""
        user_key = self._current_user_key()
        if not user_key:
            return None
        try:
            from nanobot.trading.broker_credentials import BrokerCredentialStore

            store = BrokerCredentialStore()
            if not store.enabled:
                return None
            return await store.get_credentials(user_key, login=login)
        except Exception as exc:  # noqa: BLE001 - a store outage is a refusal, not a 500
            logger.warning("broker_trade: credential lookup failed ({})", exc)
            return None

    # -- execution --------------------------------------------------------- #

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        # Checked per call, not only in enabled(): an operator can flip the
        # Brokers switch while the gateway is up, and a turn already in flight
        # must honour the switch it was flipped to.
        decision = access_policy().brokers()
        if not decision.allowed:
            return ToolResult.error(
                json.dumps(
                    {
                        "ok": False,
                        "error": "brokers_disabled",
                        "message": decision.reason,
                        "next": (
                            "Broker trading is switched off for this deployment. "
                            "An administrator can turn it back on in Settings -> Admin."
                        ),
                    }
                )
            )

        action = str(kwargs.get("action") or "").strip()
        if action not in _ACTIONS:
            return ToolResult.error(
                f"Unknown action: {action or '(none)'}. Valid actions: {', '.join(_ACTIONS)}."
            )

        credentials = await self._account_credentials(str(kwargs.get("login") or ""))
        if credentials is None:
            return ToolResult.error(
                json.dumps(
                    {
                        "ok": False,
                        "error": "broker_not_connected",
                        "message": (
                            "No broker account is saved for this user, or the credential "
                            "store is unavailable."
                        ),
                        "next": (
                            "Ask the user to open Settings -> Brokers and add their broker "
                            "account number, password and server. Do NOT ask them for the "
                            "password in chat."
                        ),
                    }
                )
            )

        sandbox = _sandbox_tool(getattr(self, "_ctx", None))
        if sandbox is None:
            return ToolResult.error(
                "No execution sandbox is configured. Broker trading runs the MT5 terminal "
                "inside a sandbox (novita/vps/runloop/daytona); nothing is installed on "
                "the application host."
            )

        cli_action, cli_kwargs = self._cli_invocation(action, kwargs, credentials)
        try:
            from nanobot.agent.tools.mt5_sandbox import (
                _DEFAULT_TIMEOUT,
                _parse_payload,
                _with_bootstrap,
                build_cli_command,
            )
        except Exception as exc:  # noqa: BLE001 - a missing CLI module is a hard stop
            logger.exception("broker_trade: could not load the MT5 CLI plumbing")
            return ToolResult.error(f"Broker trading is unavailable: {exc}")

        command = _with_bootstrap(build_cli_command(cli_action, cli_kwargs))
        timeout = int(kwargs.get("timeout") or _DEFAULT_TIMEOUT)
        if cli_action in ("start", "login"):
            wait = float(kwargs.get("wait_seconds") or 0.0)
            if wait > 0:
                timeout = max(timeout, min(int(wait), 100) + 20)

        try:
            rendered = await sandbox.execute(
                action="run",
                command=command,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("broker_trade: sandbox call failed ({})", detail)
            return ToolResult.error(
                f"Broker sandbox call failed: {detail}. This is a transport error. "
                "Retry, or call action='status' to see the terminal state."
            )

        rendered_text = str(rendered)
        payload = _parse_payload(rendered_text)
        if payload is None:
            return ToolResult.error(
                "The broker command produced no JSON result. Raw output tail:\n"
                f"{rendered_text[-1200:]}"
            )
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

    @staticmethod
    def _cli_invocation(
        action: str,
        kwargs: dict[str, Any],
        credentials: dict[str, str],
    ) -> tuple[str, dict[str, Any]]:
        """Map a tool action onto an ``mt5_cli.py`` action and its arguments.

        The stored password is injected here and nowhere else -- it never
        appears in the tool result, the tool schema, or an error message.
        """
        login = credentials.get("login") or ""
        password = credentials.get("password") or ""
        server = credentials.get("server") or ""

        if action == "connect":
            # A seeded login is only honoured in portable mode, and the wait
            # has to fit the sandbox's per-command ceiling, so it is capped the
            # same way mt5_sandbox caps its own start.
            return (
                "start",
                {
                    "login": login,
                    "password": password,
                    "server": server,
                    "portable": True,
                    "wait": min(int(kwargs.get("wait_seconds") or 90), 100),
                },
            )
        if action == "install":
            # The server is what selects the build, so the broker's own
            # installer is fetched without this tool having to know the slug.
            return (
                "install",
                {
                    "server": server,
                    "timeout": int(kwargs.get("timeout") or 120),
                },
            )
        if action == "account":
            # `account` is served by the CLI's status/account read; the login
            # state is already established by a prior connect.
            return ("account", {})
        if action == "positions":
            return ("positions", {})
        if action == "order":
            return (
                "order",
                {
                    "symbol": kwargs.get("symbol") or "",
                    "side": kwargs.get("side") or "",
                    **({"volume": kwargs["volume"]} if kwargs.get("volume") is not None else {}),
                    **({"sl": kwargs["sl"]} if kwargs.get("sl") is not None else {}),
                    **({"tp": kwargs["tp"]} if kwargs.get("tp") is not None else {}),
                    **({"comment": kwargs["comment"]} if kwargs.get("comment") else {}),
                },
            )
        if action == "close":
            return (
                "close",
                {
                    **({"ticket": kwargs["ticket"]} if kwargs.get("ticket") else {}),
                    **({"symbol": kwargs["symbol"]} if kwargs.get("symbol") else {}),
                },
            )
        if action == "quote":
            return ("quote", {"symbols": kwargs.get("symbol") or ""})
        if action == "symbols":
            return ("symbols", {})
        if action == "status":
            return ("status", {"lines": 25})
        if action == "doctor":
            return ("doctor", {})
        return (action, {})
