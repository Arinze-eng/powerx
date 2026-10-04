"""Concurrent turns must be bounded, because each one holds RAM.

The 512 MB plan on which ``powerx`` runs is charged for the *whole* cgroup, and
a running agent turn keeps its entire message list resident for the turn's
lifetime. nanobot's own default permits three turns at once, which is a promise
a 512 MB host cannot keep: one measured turn alone reached 318 MB of anonymous
memory, and the container was SIGKILLed at 99.5% of the charge -- surfacing to
users as ``503 no healthy upstream``.

The bound is applied in ``entrypoint.sh`` rather than in ``config.json`` for two
reasons: the agent loop reads this value straight from the environment
(:file:`nanobot/agent/loop.py`, ``NANOBOT_MAX_CONCURRENT_REQUESTS``), so it is
not subject to the persistent volume's ``config.json`` outranking injected
variables; and it stays overridable per-platform.

If this test fails because someone raised the plan, that is fine -- raise the
expected bound with it, since the number exists to track the memory ceiling.
"""

from __future__ import annotations

import re
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"
LOOP = Path(__file__).resolve().parents[1] / "nanobot" / "agent" / "loop.py"

#: The entrypoint default. Two, not one: one serialises every user behind every
#: other, three is the default that already killed the container.
EXPECTED_BOUND = 2
#: nanobot's own fallback when the variable is absent.
LOOP_DEFAULT = 3


def _exported_bound(source: str) -> str | None:
    match = re.search(
        r'export\s+NANOBOT_MAX_CONCURRENT_REQUESTS="\$\{NANOBOT_MAX_CONCURRENT_REQUESTS:-'
        r'(\d+)\}"',
        source,
    )
    return match.group(1) if match else None


def test_entrypoint_bounds_concurrent_turns() -> None:
    """The container must not accept nanobot's unbounded-by-default gate."""
    source = ENTRYPOINT.read_text()

    assert _exported_bound(source) is not None, (
        "entrypoint.sh no longer sets NANOBOT_MAX_CONCURRENT_REQUESTS; without it "
        "the agent falls back to a default sized for a larger host"
    )


def test_the_bound_is_two_not_the_loop_default() -> None:
    """Asserting the value, so a silent revert to the default is a failure."""
    bound = _exported_bound(ENTRYPOINT.read_text())

    assert bound is not None
    assert int(bound) == EXPECTED_BOUND, (
        f"NANOBOT_MAX_CONCURRENT_REQUESTS default is {bound}; expected {EXPECTED_BOUND}. "
        "0 or negative means unlimited, which must never be the default here."
    )
    assert int(bound) > 0


def test_the_bound_is_lower_than_the_library_default() -> None:
    """The whole point is that this plan is smaller than the library assumes."""
    bound = _exported_bound(ENTRYPOINT.read_text())
    assert bound is not None and int(bound) < LOOP_DEFAULT


def test_platform_override_still_wins() -> None:
    """``${VAR:-default}`` semantics, not a hard assignment that hides the knob."""
    source = ENTRYPOINT.read_text()

    assert re.search(
        r'export\s+NANOBOT_MAX_CONCURRENT_REQUESTS='
        r'"\$\{NANOBOT_MAX_CONCURRENT_REQUESTS:-\d+\}"',
        source,
    ), "the bound must be a default, so the platform can still set it explicitly"


def test_the_agent_loop_still_reads_this_variable() -> None:
    """Guard against the knob being renamed under our feet."""
    assert "NANOBOT_MAX_CONCURRENT_REQUESTS" in LOOP.read_text(), (
        "the agent loop no longer reads NANOBOT_MAX_CONCURRENT_REQUESTS; "
        "this entrypoint default is now decoration"
    )
