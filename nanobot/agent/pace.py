"""Opt-in pacing between tool steps.

WHY: on a fast model a read that takes milliseconds lands in the UI and is
replaced by the next step before the user can see what the agent was doing.
The turn then looks like it skipped from one step to the next with nothing in
between, or froze. The fix is a visible status before each tool step and a
short, deliberate pause so the status is on screen for a moment.

Off by default. Enable with ``POWERX_PACE_STATUS=1``. The pause length is
``POWERX_PACE_SECONDS`` (default 1.5, clamped to 0..10). ``0`` keeps the status
line but drops the pause.

The pause is not a model delay: it runs in the runner, after the tool has been
chosen and before it executes, and it is cancellable like any other await.
"""

from __future__ import annotations

import os

PACE_ENV = "POWERX_PACE_STATUS"
PACE_SECONDS_ENV = "POWERX_PACE_SECONDS"

DEFAULT_PACE_SECONDS = 1.5
MAX_PACE_SECONDS = 10.0

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def pace_enabled() -> bool:
    """True when the operator turned pacing on. Anything else means off."""
    raw = os.environ.get(PACE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_VALUES


def pace_seconds() -> float:
    """Length of the deliberate pause before each tool step.

    An unset or blank value means the default. An invalid value falls back to
    the default rather than silently removing the pause. Values are clamped to
    ``0..MAX_PACE_SECONDS`` so a typo cannot stall a turn for minutes.
    """
    raw = os.environ.get(PACE_SECONDS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PACE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PACE_SECONDS
    if value != value:  # NaN
        return DEFAULT_PACE_SECONDS
    return min(max(value, 0.0), MAX_PACE_SECONDS)
