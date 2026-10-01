"""A forced speed profile for slow models.

Measured problem: on a slow reasoning model the wall-clock cost of a turn is
dominated by tokens the model emits *before* it does anything useful -- thinking
tokens, then a long preamble -- and by the fact that every extra round-trip pays
that cost again. The agent cannot make a model generate faster. It can stop
asking for more generation than the step needs.

This module is the lever for that, and it is deliberately blunt: a profile is a
set of provider-side generation overrides applied on top of whatever the model
preset says, at the single point where a model call is built. "fast" is not a
hint to the model -- it is the agent refusing to pay for a budget it does not
need, which is why it is called a force and not a suggestion.

Resolution order, first hit wins:

1. An explicit force set in-process (``force_profile``), which is how a turn
   scopes itself to a profile.
2. The ``POWERX_SPEED_PROFILE`` environment variable, which is how an operator
   forces it for a whole deployment -- including for a model they cannot
   configure per-request, like a self-hosted GLM endpoint.

With neither set, ``apply_to_generation`` returns its input unchanged. The
default deployment behaves exactly as it did before this module existed.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

#: The profile values the agent accepts. ``None`` means "no profile" and is a
#: first-class value, not a missing one -- it is the unforced deployment.
PROFILE_NONE = "off"

#: What each profile overrides. Only keys a provider call actually reads.
#:
#: ``reasoning_effort`` is the single biggest lever on a slow reasoning model:
#: "none" tells the gateway not to emit reasoning tokens at all where the
#: provider supports it. ``max_tokens`` is the second: it caps the damage when
#: the model decides to ramble in prose.
#:
#: That ``max_tokens`` cap is a *text answer* lever only, and ``apply_to_generation``
#: refuses to apply it to a request that may have to emit a tool call (see
#: ``tool_call_payload``). A tool step's output IS its arguments -- a ``write_file``
#: call carries the whole file -- so a small ceiling truncates the call mid-JSON,
#: the truncated call is discarded unexecuted, and the step is lost. Capping there
#: does not make the turn faster, it makes it fail.
SPEED_PROFILES: dict[str, dict[str, Any]] = {
    PROFILE_NONE: {},
    # For a small or impatient task on a slow model: answer now, no reasoning.
    "fast": {
        "reasoning_effort": "none",
        "max_tokens": 2_048,
    },
    # The default working speed: reasoning allowed but not unbounded.
    "balanced": {
        "reasoning_effort": "low",
        "max_tokens": 4_096,
    },
}

_FORCED: ContextVar[str | None] = ContextVar("powerx_speed_profile", default=None)

#: The environment variable an operator sets to force a profile deployment-wide.
ENV_VAR = "POWERX_SPEED_PROFILE"


def known_profiles() -> list[str]:
    return sorted(SPEED_PROFILES)


def resolve_profile(explicit: str | None = None) -> str | None:
    """The active profile, or None when nothing is forcing one.

    An unrecognised value is treated as no profile rather than raising: a typo
    in an environment variable must not take the agent down, and silently
    running at normal speed is a safe failure for a performance knob.
    """
    for candidate in (explicit, _FORCED.get(), os.getenv(ENV_VAR)):
        if candidate is None:
            continue
        name = str(candidate).strip().lower()
        if name in SPEED_PROFILES:
            return name
    return None


@contextmanager
def force_profile(profile: str | None) -> Iterator[str | None]:
    """Force a profile for the duration of the block.

    Nesting restores the previous value, so a turn that forces "fast" does not
    silently make the next turn fast as well.
    """
    token = _FORCED.set(profile)
    try:
        yield profile
    finally:
        _FORCED.reset(token)


def apply_to_generation(
    generation: dict[str, Any],
    *,
    profile: str | None = None,
    tool_call_payload: bool = False,
) -> dict[str, Any]:
    """Overlay the active profile onto a set of generation kwargs.

    Returns a new mapping. A profile key that is ``None`` in the input is still
    overridden -- "the provider default" is exactly what a force is meant to
    replace -- but a profile never invents a key the caller did not have.

    ``tool_call_payload`` says the request is being given tools, so the model may
    answer with a tool call whose arguments carry the step's real output (a whole
    file, a whole patch). The profile's ``max_tokens`` cap is skipped for those
    requests and the configured ceiling stands: a ceiling too small to hold one
    complete call does not buy speed, it truncates the call, and the agent drops
    a truncated call -- so the step is lost and the turn ends with nothing. Every
    other profile key (``reasoning_effort``, the bigger lever) still applies.
    """
    active = resolve_profile(profile)
    if active is None:
        return dict(generation)
    overlay = SPEED_PROFILES[active]
    result = dict(generation)
    for key, value in overlay.items():
        if key not in result:
            continue
        if key == "max_tokens" and tool_call_payload:
            continue
        result[key] = value
    return result


def describe(profile: str | None = None) -> str:
    """One line the model or the user can read."""
    active = resolve_profile(profile)
    if active is None:
        return (
            f"no speed profile is forced; the model runs at its configured "
            f"settings (set {ENV_VAR} to one of: {', '.join(known_profiles())})"
        )
    overlay = ", ".join(f"{k}={v}" for k, v in SPEED_PROFILES[active].items())
    return (
        f"speed profile {active!r} is forced, overriding: {overlay} "
        "(the max_tokens cap is skipped on requests that carry tools, so a tool "
        "call's own payload is never truncated)"
    )
