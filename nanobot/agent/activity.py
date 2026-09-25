"""What the agent is doing, as terminal-shaped lines the WebUI can print.

WHY THIS IS NOT THE WEBUI'S JOB.

The Live screen's terminal feed started as a pure WebUI concern: the panel folded
the tool events it already received into rows and rendered them. That works only
for as long as the WebUI understands every tool, and the WebUI is the wrong place
to keep that knowledge — it cannot see which tools exist, it ships to clients that
cannot be redeployed, and a tool added tomorrow renders as a bare name until
somebody remembers to update a TypeScript set. So the *knowledge* lives here, next
to the tools, and the WebUI is reduced to printing what it is given.

WHAT A ROW IS.

One tool call, at the granularity the agent itself reasons at. A shell pipeline
inside one ``exec`` is one row, because that is one decision. A browser that
navigates, finds a node and clicks it is three rows, because it was three calls.

THE PRIVACY BOUNDARY, AND WHY IT IS HERE.

Tool events reach every WebUI client and are persisted in the chat transcript, so
anything left in ``arguments`` is durable, broadcast and retained. A shell command
routinely carries a token; a browser login carries a password in a form fill. So
the feed is served *bounded* arguments: secrets are dropped outright, every other
value is length-capped, and the whole rewrite is reversible with one env var for
an operator debugging a tool contract. Dropping the field instead would be simpler
and would be wrong: the panel's whole value is showing the command, and a panel
that shows a tool name and nothing else is the static screen again with more
chrome.
"""

from __future__ import annotations

import os
from typing import Any

#: Tools that run something in — or about — the sandbox.
#:
#: Deliberately a superset of the obvious ``exec``. A hosted sandbox is driven
#: through its own tool on some deployments, and a feed that showed ``exec`` while
#: dropping ``novita_sandbox`` would read as "the agent did nothing", which is
#: worse than no feed. Adding a tool here is the one line required to make it
#: appear; nothing else needs to know it exists.
SANDBOX_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "exec",
        "shell",
        "bash",
        # Hosted sandbox handles, one per backend the repo supports.
        "novita_sandbox",
        "sandbox",
        "runloop_sandbox",
        "daytona_sandbox",
        "vps_sandbox",
        "upstash_sandbox",
        "vercel_sandbox",
        # Products built on top of a sandbox.
        "mt5_sandbox",
        "python_code",
        "run_cli_app",
        "spawn",
        "long_task",
        "arduino_verify",
        "pine_chart",
        "pine_script",
        "workspace_bridge",
        "exec_session",
        # Media processing and downloads both land in a sandbox.
        "media_sandbox",
        # Compiles on a hosted runner rather than in this sandbox, but it is the
        # same class of thing the user is waiting on, and a build that takes four
        # minutes is exactly what a live feed is for.
        "build_artifact",
    }
)

#: Tools that put a page in front of the agent.
#:
#: A separate family from :data:`SANDBOX_TOOL_NAMES` because "the agent is
#: browsing" is a different thing an operator wants to watch than "the agent ran
#: a command", and a panel that showed one and not the other would be half a
#: window. Both feed the same renderer; nothing downstream branches on which set
#: a tool came from, so adding to either is one line.
#:
#: ``media_sandbox`` is deliberately *not* here even though it can take a URL: it
#: is a sandbox tool that sometimes fetches, so it is classified by what the call
#: actually carries — a URL makes the row a navigation, anything else makes it a
#: sandbox row. Listing it as a browsing tool would label every local transcode as
#: browsing.
BROWSING_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "browser",
        "human_browser",
        "browser_open",
        "browser_use",
        "browser_navigate",
        "playwright",
        "web_fetch",
        "web_dev",
        "firecrawl",
    }
)

#: Argument keys that carry a destination.
#:
#: Checked on *any* tool rather than only the ones registered in
#: :data:`NAVIGATION_ARGUMENTS`, so a tool added later that happens to take a
#: ``url`` is classified correctly with nobody having to remember to register it.
#: ``target`` is deliberately absent: on both browser tools it is a CSS selector
#: or visible text, not an address, and reading it as one would print a selector
#: in the destination slot of every click.
URL_ARGUMENTS: frozenset[str] = frozenset(
    {"url", "starting_url", "target_url", "page_url", "page_urls", "broker_installer_url"}
)

#: Argument keys that carry something to run.
COMMAND_ARGUMENTS: frozenset[str] = frozenset({"command", "cmd", "shell_command", "script"})

#: Tools that put a page in front of the agent, mapped to the argument key that
#: carries the destination.
#:
#: The key differs per tool because the tools were written by different people for
#: different backends: the CDP browser takes a ``url``, the Agent-E style browser
#: takes a ``starting_url``, MT5's installer action takes a ``broker_installer_url``.
#: Naming them here is what lets one feed render all of them without the WebUI
#: knowing any of them.
NAVIGATION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "browser": ("url",),
    "human_browser": ("url",),
    "browser_open": ("url",),
    "browser_use": ("url", "starting_url"),
    "browser_navigate": ("url",),
    "playwright": ("url",),
    "web": ("url",),
    "web_dev": ("url",),
    "web_fetch": ("url",),
    "firecrawl": ("url",),
    "media_sandbox": ("url",),
    "novita_sandbox": ("url",),
    "mt5_sandbox": ("broker_installer_url", "page_urls"),
}

#: MT5 actions that change the broker's book. Mirrors ``_TRADING_ACTIONS`` in
#: ``mt5_sandbox``; a trade row is the one line an operator must never miss, so it
#: is classified rather than guessed at.
MT5_TRADING_ACTIONS: frozenset[str] = frozenset(
    {"order", "split", "close", "close_all", "cancel", "modify", "guard", "limits"}
)

#: Argument keys whose VALUE is a secret and must never leave the process.
_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "auth",
        "cookie",
        "cookies",
        "credentials",
        "private_key",
        "session_key",
        "signature",
        "otp",
    }
)

#: Keys always kept regardless of length, because they identify the call rather
#: than describe it, and losing one makes a row unreadable.
_ALWAYS_KEEP: frozenset[str] = frozenset(
    {"action", "command", "cmd", "symbol", "side", "volume", "entry_type", "method", "url"}
)

#: Per-string cap. Long enough for a full shell command and a full URL, short
#: enough that a pasted file body cannot become a broadcast payload.
MAX_ARGUMENT_CHARS = 400

#: Cap on how many keys survive the rewrite; a tool with 40 parameters is a tool
#: whose 40 parameters are not all worth streaming.
MAX_ARGUMENT_KEYS = 24

ACTIVITY_ARGUMENTS_ENV = "NANOBOT_ACTIVITY_ARGUMENTS"

#: How a call ended: it failed, or it was *refused*.
#:
#: The distinction matters and is easy to lose. An agent that tried to trade on an
#: account with live trading switched off produces a tool result that looks like a
#: failure and is nothing of the sort — the guard worked. Colouring that red next to
#: a real broker rejection trains an operator to ignore red, so a refusal gets its
#: own outcome and its own colour.
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_REFUSED = "refused"

#: What a row is, which decides how the client colours and groups it.
#:
#: The client renders by these and never by tool name. That is the point: the
#: WebUI cannot see which tools exist, it ships to clients that cannot be
#: redeployed in step with the server, and a tool added tomorrow would render as a
#: bare name until somebody remembered to update a TypeScript set. So the tool set
#: and the classification live here, next to the tools, and the WebUI is reduced to
#: a printer.
KIND_COMMAND = "command"
KIND_TRADE = "trade"
KIND_NAV = "nav"
KIND_SANDBOX = "sandbox"

#: Phrases that mean "a guard said no", not "this went wrong".
#:
#: Drawn from the refusals this codebase actually emits: the MT5 live-trading gate
#: (``MT5_ALLOW_TRADING``), the workspace path guard, a deployment with no sandbox
#: configured, and the workspace-access scope check. Matched case-insensitively
#: against the tool's own error text.
_REFUSAL_MARKERS: tuple[str, ...] = (
    "allow_trading",
    "not permitted here",
    "not allowed",
    "is refused",
    "refused:",
    "permission denied",
    "access denied",
    "not authorized",
    "no execution sandbox is configured",
    "no execution backend is configured",
    "is not configured",
    "outside the workspace",
    "path guard",
    "sandbox is not configured",
    "requires the",
    "is switched off",
    "is disabled",
)


def classify_outcome(phase: str, error: Any = None) -> str:
    """Reduce a finished tool event to one of the three outcomes.

    A refusal is only ever decided on a failed call: a success cannot have been
    prevented, and treating one as refused would hide a call that actually ran.
    """
    if phase != "error" and phase != "failed":
        return OUTCOME_OK
    text = error if isinstance(error, str) else str(error or "")
    lowered = text.lower()
    if any(marker in lowered for marker in _REFUSAL_MARKERS):
        return OUTCOME_REFUSED
    return OUTCOME_ERROR


def activity_arguments_enabled() -> bool:
    """Whether tool events carry bounded arguments.

    Default on. An operator chasing a tool-contract bug sets this to ``0`` and
    gets the arguments exactly as the model sent them.
    """
    raw = os.environ.get(ACTIVITY_ARGUMENTS_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _present_keys(arguments: dict[str, Any]) -> set[str]:
    """The keys of *arguments* that carry something, ignoring empty placeholders.

    A tool call routinely arrives with every optional parameter set to ``null``,
    and a browser click that carries only a selector must not be classified as a
    navigation because ``url`` was present-and-empty.
    """
    return {
        str(key)
        for key, value in arguments.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def classify_kind(name: str, arguments: Any = None) -> str | None:
    """What row a call to *name* should print as, or ``None`` for no row.

    ``None`` is the common answer and the important one: searching, sending a
    message and reading a file all arrive on the same event stream, and a feed
    that listed them would bury the line that matters under the lines that do
    not. The two families that do print are a machine the agent is driving and a
    page it is reading, because those are the two things a desktop panel can
    actually stand in for.
    """
    tool = (name or "").strip()
    if not tool:
        return None
    args = arguments if isinstance(arguments, dict) else {}
    present = _present_keys(args)

    # MT5 is classified by its action, never by its arguments: its installer
    # action takes a URL, and a download is not the agent browsing.
    if tool == "mt5_sandbox":
        action = args.get("action")
        if isinstance(action, str) and action in MT5_TRADING_ACTIONS:
            return KIND_TRADE
        return KIND_SANDBOX

    if tool in BROWSING_TOOL_NAMES:
        return KIND_NAV

    if tool in SANDBOX_TOOL_NAMES:
        if present & (URL_ARGUMENTS | set(navigation_keys(tool))):
            return KIND_NAV
        if present & COMMAND_ARGUMENTS:
            return KIND_COMMAND
        return KIND_SANDBOX

    # An unknown tool is printed only when it announces something to run, which is
    # the shape a new shell-ish tool almost always has. Guessing wider than this
    # would print tools nobody has thought about on a panel meant to stay legible.
    if present & COMMAND_ARGUMENTS:
        return KIND_COMMAND
    return None


def tool_reaches_sandbox(name: str) -> bool:
    """True when *name* is a tool whose calls run something on a machine."""
    return (name or "").strip() in SANDBOX_TOOL_NAMES


def navigation_keys(name: str) -> tuple[str, ...]:
    """The argument keys that carry a destination for *name*, if any."""
    return NAVIGATION_ARGUMENTS.get((name or "").strip(), ())


def tool_navigates(name: str) -> bool:
    """True when *name* is a tool that can be pointed at a page."""
    return bool(navigation_keys(name))


def tool_is_feed_worthy(name: str) -> bool:
    """True when the terminal feed should print a call to *name*.

    The union of the two families: a machine the agent is driving, or a page it is
    reading. Everything else — searching, messaging, reading a file — is not
    something a desktop panel can show anyway.
    """
    tool = (name or "").strip()
    return tool in SANDBOX_TOOL_NAMES or tool in BROWSING_TOOL_NAMES


def feed_tool_names() -> frozenset[str]:
    """Every tool name the feed renders. Exists so a client can be told the set."""
    return SANDBOX_TOOL_NAMES | BROWSING_TOOL_NAMES


def _bound(value: Any, chars: int = MAX_ARGUMENT_CHARS) -> Any:
    """Recursively cap every string in *value*, dropping secrets as it goes.

    Depth-bounded rather than recursive-forever: a tool argument that nests
    deeper than a handful of levels is a payload, not a parameter, and the
    summarising is not worth the pathological case.
    """
    if isinstance(value, str):
        return value if len(value) <= chars else value[: chars - 1] + "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in _SECRET_KEYS:
                continue
            out[str(key)] = _bound(item, chars)
        return out
    if isinstance(value, (list, tuple)):
        return [_bound(item, chars) for item in value[:MAX_ARGUMENT_KEYS]]
    return value


def tool_event_arguments(name: str, arguments: Any) -> Any:
    """Bound the arguments of a tool event, for sandbox and browsing tools only.

    Returns *arguments* untouched for every other tool. That is deliberate: the
    chat transcript renders tool arguments for its own reasons, and this module
    has no business rewriting a tool it does not understand. Only the calls the
    terminal feed renders — commands and pages — are bounded, and for those the
    rewrite is a strict narrowing: keys are dropped only when they are secrets, and
    values are shortened only when they are long.

    The gate is :func:`classify_kind`, not the tool-name sets, so that an unknown
    tool whose call carries a command is bounded too. Gating on the sets would
    have left exactly that case — the tool nobody has registered yet — as the one
    place a command with a token in it reached every client unbounded.
    """
    if not activity_arguments_enabled():
        return arguments
    if not isinstance(arguments, dict):
        return arguments
    if classify_kind(name, arguments) is None:
        return arguments

    keep = _ALWAYS_KEEP
    # A navigation tool's destination key is kept even when it is not one of the
    # well-known ones, because it is the whole point of the row.
    extra = set(navigation_keys(name))

    bounded = _bound(arguments)
    if not isinstance(bounded, dict):  # pragma: no cover - _bound preserves dicts
        return arguments

    ordered = sorted(
        bounded.items(),
        key=lambda item: (0 if item[0] in keep or item[0] in extra else 1, item[0]),
    )
    return {key: value for key, value in ordered[:MAX_ARGUMENT_KEYS]}


__all__ = [
    "ACTIVITY_ARGUMENTS_ENV",
    "BROWSING_TOOL_NAMES",
    "MAX_ARGUMENT_CHARS",
    "MAX_ARGUMENT_KEYS",
    "MT5_TRADING_ACTIONS",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "OUTCOME_REFUSED",
    "NAVIGATION_ARGUMENTS",
    "SANDBOX_TOOL_NAMES",
    "activity_arguments_enabled",
    "classify_outcome",
    "feed_tool_names",
    "navigation_keys",
    "tool_event_arguments",
    "tool_is_feed_worthy",
    "tool_navigates",
    "tool_reaches_sandbox",
]
