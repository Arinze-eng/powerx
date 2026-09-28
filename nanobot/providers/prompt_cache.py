"""Prompt-cache policy for OpenAI-compatible endpoints.

There are two ways an OpenAI-compatible gateway can serve a cached prefix, and
they are not interchangeable:

``auto``
    Implicit/prefix caching. The gateway hashes the stable leading bytes of the
    request (system message, tool schema, history) and bills a repeat request
    against the stored copy. Nothing is added to the request, so it is always
    safe; a hit is only visible in the usage block (``cached_tokens``). Measured
    live on Kyma/qwen3.7-flash: a repeated 9k-token prefix reported
    ``cached_tokens=8960`` (99% hit) and cost 0.000373 -> 0.000083, a 78% cut.

``markers``
    Explicit Anthropic-style breakpoints (``cache_control: {"type": "ephemeral"}``
    on content blocks). Only correct for endpoints that document the field.
    Some OpenAI-compatible proxies accept the field and then quietly discard
    the block it sits on: measured against the live gemini-proxy, a marked
    system message stopped reaching the model entirely (``prompt_tokens`` fell
    4234 -> 14 and the model could no longer recite a fact that was only in the
    system prompt). A rejected request raises; a *swallowed* one does not, so
    this mode is opt-in per provider and the admin page's cache test verifies
    the system prompt still arrives before anyone relies on it.

``off``
    Send nothing cache-related, including no markers for Claude.

In every other mode the provider additionally sends ``prompt_cache_key``. That
is not a cache format: it is a request for the gateway to route this
conversation to the node that already holds its prefix, which is what stops a
load-balanced gateway from serving every turn from a cold machine. OpenAI routes
on it and a gateway that ignores the field is unaffected, so it is sent by
default; it is also the only lever that exists on a gateway reporting no cache
usage at all. Measured against the configured gemini-proxy: accepted (HTTP 200)
with the system prompt still reaching the model, and no cache field reported
back, so its routing effect *there* is unproven rather than zero.
"""

from __future__ import annotations

from typing import Any

CACHE_MODES: tuple[str, ...] = ("auto", "markers", "off")
DEFAULT_MODE = "auto"

#: Legacy switch kept working: it only ever meant "markers", no more.
_LEGACY_FORCE_ENV = "POWERX_FORCE_CACHE_MARKERS"
_MODE_ENV = "POWERX_PROMPT_CACHE"


def normalize_cache_mode(value: Any) -> str | None:
    """Return a valid mode, or ``None`` when the value is unset/unusable."""
    if not isinstance(value, str):
        return None
    mode = value.strip().lower()
    if mode in ("", "default", "none", "false", "0"):
        return None
    if mode in CACHE_MODES:
        return mode
    # Accept the obvious aliases so an operator typing "on"/"true" gets markers
    # rather than a silently ignored setting.
    if mode in ("on", "true", "yes", "1", "explicit", "breakpoints"):
        return "markers"
    if mode in ("automatic", "implicit", "prefix"):
        return "auto"
    return None


def resolve_cache_mode(
    *,
    configured: Any = None,
    spec: Any = None,
    environ: dict[str, str] | None = None,
) -> str:
    """Resolve the effective cache mode.

    Precedence: the provider's saved setting, then the environment, then the
    legacy force-markers switch, then the provider spec's own capability flag,
    then :data:`DEFAULT_MODE`.
    """
    mode = normalize_cache_mode(configured)
    if mode is not None:
        return mode

    env = environ if environ is not None else _environ()
    mode = normalize_cache_mode(env.get(_MODE_ENV))
    if mode is not None:
        return mode

    if str(env.get(_LEGACY_FORCE_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}:
        return "markers"

    # A spec that advertises ``supports_prompt_caching`` has been verified to
    # accept explicit breakpoints (Anthropic, OpenRouter's passthrough), so its
    # default is markers rather than the safe-by-default auto.
    if spec is not None and getattr(spec, "supports_prompt_caching", False):
        return "markers"

    return DEFAULT_MODE


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def markers_allowed(
    mode: str,
    *,
    is_claude: bool,
    spec: Any = None,
) -> bool:
    """Whether this request should carry explicit cache breakpoints.

    ``auto`` keeps the historical Claude behaviour: markers only for Claude
    models on a spec that advertises prompt caching. Everything else is left
    alone.
    """
    if mode == "off":
        return False
    if mode == "markers":
        return True
    return bool(is_claude and spec is not None and getattr(spec, "supports_prompt_caching", False))


def cache_marker_rejection(exc: BaseException) -> bool:
    """Return True when a failed request failed *because of* a cache field.

    Covers both additions this module can make to a request: the explicit
    breakpoints (``cache_control``) and the routing key (``prompt_cache_key``).
    Either is a field an unverified gateway is entitled to refuse, and
    :func:`strip_cache_markers` removes both, so one retry makes both refusals
    survivable and a cache setting can never fail a turn.

    Only a loud refusal is detectable here. A gateway that swallows a marked
    block and answers normally cannot be caught at this layer, which is why
    ``markers`` stays opt-in.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        code = int(status) if status is not None else 0
    except (TypeError, ValueError):
        code = 0
    if code and code not in (400, 404, 415, 422):
        return False
    text = f"{exc}".lower()
    named = (
        "cache_control" in text
        or "cache control" in text
        or "prompt_cache_key" in text
        or "prompt cache key" in text
    )
    if not named:
        return False
    return "400" in text or "invalid" in text or "unsupported" in text or "unknown" in text or bool(code)


def strip_cache_markers(kwargs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return a copy of *kwargs* with every ``cache_control`` marker removed.

    Retrying a rejected request with these stripped is what makes ``markers``
    safe to switch on for an unverified OpenAI-compatible endpoint: the first
    attempt carries breakpoints, and if the gateway refuses them the same call
    is re-sent unmarked instead of failing the turn. Returns the new kwargs and
    how many markers were removed, so a caller can tell whether a retry is
    worth attempting at all.
    """
    removed = 0
    new_kwargs = dict(kwargs)

    new_messages: list[Any] = []
    for message in kwargs.get("messages") or []:
        if not isinstance(message, dict):
            new_messages.append(message)
            continue
        if "cache_control" in message:
            message = {k: v for k, v in message.items() if k != "cache_control"}
            removed += 1
        content = message.get("content")
        if isinstance(content, list):
            blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    block = {k: v for k, v in block.items() if k != "cache_control"}
                    removed += 1
                blocks.append(block)
            message = {**message, "content": blocks}
        new_messages.append(message)
    if new_messages:
        new_kwargs["messages"] = new_messages

    tools = kwargs.get("tools")
    if isinstance(tools, list):
        new_tools: list[Any] = []
        for tool in tools:
            if isinstance(tool, dict) and "cache_control" in tool:
                tool = {k: v for k, v in tool.items() if k != "cache_control"}
                removed += 1
            new_tools.append(tool)
        new_kwargs["tools"] = new_tools

    # The routing key is a cache field too: a gateway that refused the
    # breakpoints gets a retry with nothing cache-related on it at all.
    if new_kwargs.pop("prompt_cache_key", None) is not None:
        removed += 1

    return new_kwargs, removed


def bound_session_key() -> str | None:
    """The conversation this LLM call belongs to, when the loop has bound one.

    Imported lazily on purpose: the provider layer must not pull the tool layer
    in at import time, and a caller that never binds a context simply has no
    conversation to key a cache on.
    """
    try:
        from nanobot.agent.tools.context import current_request_session_key
    except Exception:  # noqa: BLE001 - a missing context layer only costs the key
        return None
    try:
        return current_request_session_key()
    except Exception:  # noqa: BLE001
        return None


def conversation_cache_key(session_key: str | None = None) -> str | None:
    """A stable, bounded routing key for one conversation's prompt cache.

    Sent as ``prompt_cache_key``. Gateways that route on it (OpenAI does) keep a
    conversation on the machine that already holds its prefix, which is the
    difference between a repeat request matching the cache and missing it; a
    gateway that ignores the field is unaffected, so sending it costs nothing.
    The session key is hashed because the field is bounded and a session key is
    not: 32 hex characters, deterministic for the life of the conversation and
    useless to anyone reading a gateway's logs.
    """
    import hashlib

    key = (session_key if session_key is not None else bound_session_key()) or ""
    key = key.strip()
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:32]


def cache_hit_pct(prompt_tokens: int, cached_tokens: int) -> float:
    """Share of the prompt served from cache, as a percentage (0 when unknown)."""
    try:
        prompt = int(prompt_tokens or 0)
        cached = int(cached_tokens or 0)
    except (TypeError, ValueError):
        return 0.0
    if prompt <= 0 or cached <= 0:
        return 0.0
    return round(min(100.0, cached * 100.0 / prompt), 1)
