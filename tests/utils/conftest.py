"""Shared isolation for the memory housekeeping tests.

``nanobot.utils.memory_reclaim`` keeps a monotonic clock for the heap-reclaim
rate limit and, since the sweep breaker, one for the page-cache walk plus an
open-until stamp. Those are module globals on purpose -- the point of a rate
limit is that it survives across calls -- but that makes any test that exercises
them order-dependent: whichever spec runs first leaves the clock armed and a
later one sees the work skipped instead of performed.

Resetting per test is the cheapest way to keep the suite honest, and it is the
same call the specs already make by hand.
"""

from __future__ import annotations

import pytest

from nanobot.utils import memory_reclaim


@pytest.fixture(autouse=True)
def _reset_reclaim_clock():
    """Start every spec with an unarmed rate limiter and a closed breaker."""
    memory_reclaim._reset_interval_for_tests()
    yield
    memory_reclaim._reset_interval_for_tests()
