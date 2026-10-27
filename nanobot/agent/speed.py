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
3. The deployment default (:func:`deployment_default`), named by
   ``POWERX_SPEED_PROFILE_DEFAULT`` and ``fast`` when that is unset.

There is always a profile in force. That is deliberate: leaving the pace to the
provider default measured out as turns that ran to the 30-minute wall-clock
ceiling without finishing. An operator restores the old behaviour explicitly
with ``POWERX_SPEED_PROFILE=off``.
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
    # Sustained work on a conversational model: enough reasoning for a real
    # multi-step task, none of the unbounded rambling that made a turn drag.
    # The text ceiling matches the configured default (8192): this profile
    # restricts thinking, not the answer -- capping prose to a number below what
    # the deployment chose is how a long answer gets truncated mid-file.
    "steady": {
        "reasoning_effort": "medium",
    },
}

#: The profile a deployment runs when it has not chosen one.
#:
#: This used to be ``None`` -- "no profile, run at the configured settings".
#: MEASURED problem with that default: the configured settings are the provider
#: default, and on a slow reasoning model that means every step pays for
#: unbounded thinking before it does anything. The result was a turn that ran
#: to the wall-clock ceiling (30 min) without finishing the task. A pace is
#: therefore a property of the deployment, not an opt-in: "fast" is the shipped
#: default, and an operator who wants the old behaviour sets
#: ``POWERX_SPEED_PROFILE=off``.
DEFAULT_PROFILE = "fast"

_FORCED: ContextVar[str | None] = ContextVar("powerx_speed_profile", default=None)

#: The environment variable an operator sets to force a profile deployment-wide.
ENV_VAR = "POWERX_SPEED_PROFILE"

#: The env var that names the deployment default. Read once at import so a
#: deployment can point at ``balanced`` without the module guessing at it.
DEFAULT_ENV_VAR = "POWERX_SPEED_PROFILE_DEFAULT"


def known_profiles() -> list[str]:
    return sorted(SPEED_PROFILES)


def deployment_default() -> str:
    """The profile a deployment runs when it has chosen nothing.

    ``POWERX_SPEED_PROFILE_DEFAULT`` names it, so a deployment can pick
    ``balanced``/``steady`` without also being unable to loosen it later. An
    unparseable or unknown name falls back to :data:`DEFAULT_PROFILE` rather
    than to "off": a typo must not silently uncap the agent, which is the exact
    failure this constant exists to prevent.
    """
    raw = os.getenv(DEFAULT_ENV_VAR)
    if raw is not None:
        name = str(raw).strip().lower()
        if name in SPEED_PROFILES:
            return name
    return DEFAULT_PROFILE


def resolve_profile(explicit: str | None = None) -> str:
    """The active profile. Always a profile -- never None.

    A deployment always has a pace now: the forced value wins, then an
    in-process force, then ``POWERX_SPEED_PROFILE``, then the deployment default
    (:func:`deployment_default`, itself overridable with
    ``POWERX_SPEED_PROFILE_DEFAULT``, and ``fast`` if neither is set).

    ``POWERX_SPEED_PROFILE=off`` is the documented opt-out and is honoured
    explicitly: an operator who writes "off" gets the old unforced behaviour,
    and no other unrecognised value is allowed to reach it by accident.

    An unrecognised value at any level is skipped rather than raising: a typo in
    an environment variable must not take the agent down, and the safe failure
    for a performance knob is the default pace, not no pace at all.
    """
    for candidate in (explicit, _FORCED.get(), os.getenv(ENV_VAR)):
        if candidate is None:
            continue
        name = str(candidate).strip().lower()
        if name == PROFILE_NONE:
            return PROFILE_NONE
        if name in SPEED_PROFILES:
            return name
    return deployment_default()


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
    if active == PROFILE_NONE:
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
    """One line the model or the user can read.

    Always names a profile, because there always is one in force. The only
    "off" is the explicit opt-out, and it says so and gives the switch back.
    The counterfactual -- what is *forced*, if anything, versus what the
    deployment default merely is -- is reported so an operator can tell a lever
    they set from the shipped one.
    """
    active = resolve_profile(profile)
    if active == PROFILE_NONE:
        return (
            f"no speed profile is in force ({ENV_VAR}=off); the model runs at "
            f"its configured settings (set {ENV_VAR} to one of: "
            f"{', '.join(known_profiles())})"
        )
    overlay = ", ".join(f"{k}={v}" for k, v in SPEED_PROFILES[active].items())
    source = "forced" if _is_forced(profile) else "deployment default"
    return (
        f"speed profile {active!r} ({source}) is in force, overriding: {overlay} "
        f"(set {ENV_VAR}=<profile> to change it, {ENV_VAR}={PROFILE_NONE} to opt "
        "out; the max_tokens cap is skipped on requests that carry tools, so a "
        "tool call's own payload is never truncated)"
    )


def _is_forced(profile: str | None) -> bool:
    """Whether an explicit/env/context force picked the profile, not the default."""
    for candidate in (profile, _FORCED.get(), os.getenv(ENV_VAR)):
        if candidate is None:
            continue
        name = str(candidate).strip().lower()
        if name == PROFILE_NONE or name in SPEED_PROFILES:
            return True
    return False
