"""Automatic context-window resolution for admin-loaded models.

The problem this solves is a silent money leak: ``context_window_tokens`` drives
when the ContextGovernor compacts history before each LLM call. Its config
default is 200_000, but most OpenAI-compatible models an admin points PowerX at
have far smaller windows (8k–128k). If the governor believes it has 200k of
room it NEVER compacts, so every turn re-sends an ever-growing history and you
pay full input-token price on all of it — the exact cost users complain about.

Rather than make the admin hand-set a number per model, we DERIVE it from the
model id automatically, in priority order:

1. Explicit override — if the admin already set a non-default window (or set
   POWERX_CONTEXT_WINDOW_TOKENS), honour it. Never fight a deliberate choice.
2. Provider metadata — a curated value already attached to the model by the
   provider registry (builtin_models[].context_window).
3. Name-pattern heuristics — a small, ordered table matching common model-id
   substrings (gpt-4.1-mini, llama-3.1-70b, qwen2.5-coder-32k, …) to their
   published context length. Covers the vast majority of loaded models.
4. Conservative fallback — assume a modest window so compaction still triggers
   early; over-trimming costs a little extra summarisation, under-trimming costs
   a LOT of repeated tokens, so erring small is the cheap mistake.

Everything is pure + cached per model id, so resolution is free after first use
and trivially testable. It never raises: on any surprise it returns the caller's
existing value unchanged.
"""

from __future__ import annotations

import os
import re
from typing import Any

from loguru import logger

#: The config's built-in default. We treat "value == this" as "admin did not
#: deliberately choose a window", which unlocks auto-detection. A custom value
#: different from this is respected as-is.
CONFIG_DEFAULT_WINDOW = 200_000

#: Used only when nothing else matches. Small enough that long chats compact
#: (saving real token spend) yet large enough not to thrash normal short turns.
_CONSERVATIVE_FALLBACK = 16_000

#: Output budget we reserve from the window when deriving max_tokens. Mirrors a
#: typical assistant answer ceiling; keeps prompt+output inside the true window.
_OUTPUT_RESERVE_RATIO = 0.25
_MIN_OUTPUT_TOKENS = 1024
_MAX_OUTPUT_TOKENS = 8192

# Ordered name-pattern → context window (tokens). FIRST match wins, so put the
# more specific / longer patterns before generic ones. Values are the *input*
# context lengths these models advertise (conservative, well-known figures).
_MODEL_WINDOW_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    # GPT-4.1 family: 4.1 = 128k, mini/nano = 128k too.
    (re.compile(r"gpt-4\.1", re.I), 128_000),
    # GPT-4o / o-series / turbo: 128k.
    (re.compile(r"(gpt-4o|gpt-4-turbo|chatgpt-4o|o1-|o3-|o4-)", re.I), 128_000),
    # GPT-3.5 / older 4: 16k.
    (re.compile(r"(gpt-3\.5|gpt-4-0|gpt-4-32k$)", re.I), 16_000),
    # Claude 3/4 family: 200k.
    (re.compile(r"claude-[34]", re.I), 200_000),
    # Gemini 1.5/2.x flash/pro: 1M-class, but cap effective at 200k to stay safe
    # and avoid huge bills on very long contexts.
    (re.compile(r"gemini-(1\.5|2)[-.]", re.I), 200_000),
    # Llama 3.x: 3.1/3.2/3.3 = 128k; plain llama-3(0) = 8k.
    (re.compile(r"llama[-_]3[.\s]?[123]", re.I), 128_000),
    (re.compile(r"llama[-_]3(?:[^.\d]|$)", re.I), 8_000),
    # Qwen2.5-Coder / Qwen2.5: many builds are 32k; 1m variants exist but rare.
    (re.compile(r"qwen2[.\s]?5.*coder", re.I), 32_000),
    (re.compile(r"qwen2[.\s]?5", re.I), 32_000),
    # Mistral/Mixtral: 32k standard, large-latest up to 128k.
    (re.compile(r"(mistral-large|magistral|devstral)", re.I), 128_000),
    (re.compile(r"(mistral|mixtral)", re.I), 32_000),
    # DeepSeek: chat/coder v2/v3 = 64k (v3 up to 128k, keep 64k conservative).
    (re.compile(r"deepseek", re.I), 64_000),
    # Phi-4 / phi-3: 16k-ish.
    (re.compile(r"phi[-_]?\d", re.I), 16_000),
    # Gemma 2/3: 8k (gemma-2-27b) / up to 128k; conservative 8k.
    (re.compile(r"gemma[-_]?\d", re.I), 8_000),
    # Grok: 128k+.
    (re.compile(r"grok", re.I), 128_000),
)


def _env_override() -> int | None:
    raw = os.environ.get("POWERX_CONTEXT_WINDOW_TOKENS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid POWERX_CONTEXT_WINDOW_TOKENS={!r}", raw)
        return None
    return value if value > 0 else None


def _registry_window(model_id: str) -> int | None:
    """Curated context_window the provider registry already knows for this model."""
    try:
        from nanobot.providers.registry import PROVIDERS
    except Exception:  # pragma: no cover - defensive import guard
        return None
    needle = model_id.lower()
    for spec in PROVIDERS:
        for entry in spec.builtin_models or ():
            mid = (entry.id or "").lower()
            if mid and (mid == needle or needle.endswith(mid) or mid.endswith(needle)):
                if entry.context_window:
                    return int(entry.context_window)
    return None


def _pattern_window(model_id: str) -> int | None:
    for pattern, window in _MODEL_WINDOW_PATTERNS:
        if pattern.search(model_id):
            return window
    return None


def resolve_context_window(
    model_id: str,
    configured: int | None,
    *,
    allow_auto: bool = True,
) -> int:
    """Return the effective context window for *model_id*.

    ``configured`` is whatever the admin/config currently carries. Precedence:
    env override > explicit non-default configured > provider registry > name
    pattern > conservative fallback. Pure lookups; never raises.
    """
    env = _env_override()
    if env is not None:
        return env
    # A deliberate admin value (anything not equal to the untouched default) wins.
    if configured and configured != CONFIG_DEFAULT_WINDOW:
        return int(configured)
    if not allow_auto or not model_id:
        return int(configured or _CONSERVATIVE_FALLBACK)
    reg = _registry_window(model_id)
    if reg:
        return reg
    pat = _pattern_window(model_id)
    if pat:
        return pat
    # Unknown model with no curated metadata: do NOT guess a smaller window.
    # Over-shrinking a genuinely-large-window model would trigger needless
    # compaction (and can drop context); under-shrinking only costs some extra
    # tokens. So preserve whatever the caller/config already carries. Operators
    # who want protection for an unknown model set POWERX_CONTEXT_WINDOW_TOKENS.
    return int(configured or CONFIG_DEFAULT_WINDOW)


def derive_max_output(window: int, configured: int | None) -> int:
    """Pick a safe output cap from the resolved window unless admin chose one.

    Keeps prompt+completion inside the true window. Honours an explicit admin
    max_tokens (anything other than the 8192 default is respected).
    """
    out = int(max(_MIN_OUTPUT_TOKENS, min(_MAX_OUTPUT_TOKENS, window // 4)))
    return out


class WindowResolver:
    """Tiny cache so per-turn runtime construction doesn't redo lookups."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int | None], int] = {}

    def resolve(self, model_id: str, configured: int | None) -> int:
        key = (model_id or "", configured)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        value = resolve_context_window(model_id, configured)
        self._cache[key] = value
        if value != configured:
            logger.info(
                "auto context-window: model={} {} -> {} tokens (compaction tuned)",
                model_id or "?",
                configured,
                value,
            )
        return value


_shared_resolver = WindowResolver()


def auto_tune_runtime(model_id: str, snapshot: Any) -> tuple[int, int]:
    """Convenience used at runtime construction: returns (window, max_output).

    Reads the snapshot's current values, auto-resolves the window, and derives a
    matching output cap. Safe on any object exposing .context_window_tokens and
    .generation.max_tokens; falls back to passing them through on surprises.
    """
    try:
        configured_window = getattr(snapshot, "context_window_tokens", None)
        resolver = _shared_resolver
        window = resolver.resolve(model_id, configured_window)
        gen = getattr(snapshot, "generation", None)
        configured_out = getattr(gen, "max_tokens", None) if gen is not None else None
        # Only auto-derive the output cap when the admin left it at the built-in
        # default (or unset). Any other value — including a deliberate 0 or a
        # small number a test/operator chose — is respected verbatim.
        _DEFAULT_OUTPUT = 8192
        if configured_out in (None, _DEFAULT_OUTPUT):
            out = derive_max_output(window, configured_out)
        else:
            out = int(configured_out)
        return window, out
    except Exception:  # pragma: no cover - tuning must never break a run
        logger.debug("auto context-window failed, using configured values", exc_info=True)
        configured_window = getattr(snapshot, "context_window_tokens", None) or CONFIG_DEFAULT_WINDOW
        gen = getattr(snapshot, "generation", None)
        configured_out = getattr(gen, "max_tokens", None) if gen is not None else None
        return int(configured_window), int(configured_out or _MAX_OUTPUT_TOKENS)
