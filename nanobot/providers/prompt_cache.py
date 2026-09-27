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
    """Return True when a failed request failed *because of* cache markers.

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
    if "cache_control" not in text and "cache control" not in text:
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

    return new_kwargs, removed


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
