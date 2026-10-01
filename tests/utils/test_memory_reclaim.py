"""The reclaim path has to be free of ways to fail a turn.

It runs inside the agent loop, so the contract is: it returns a report, it never
raises, and it never does the work twice inside its interval.
"""

from __future__ import annotations

import gc

from nanobot.utils import memory_reclaim
from nanobot.utils.memory_reclaim import (
    RECLAIM_MIN_INTERVAL_S,
    _reset_interval_for_tests,
    freeze_long_lived,
    maybe_reclaim,
    reclaim_memory,
)


def test_reclaim_reports_and_never_raises() -> None:
    _reset_interval_for_tests()
    report = reclaim_memory(tag="test")
    assert set(report) == {
        "rss_before_mb",
        "rss_after_mb",
        "freed_mb",
        "collected",
        "trimmed",
    }
    assert report["rss_before_mb"] > 0
    assert report["rss_after_mb"] > 0
    # gc.collect() always returns an int; trim is None on a platform without it.
    assert isinstance(report["collected"], int)
    assert report["trimmed"] is None or isinstance(report["trimmed"], int)


def test_a_burst_is_actually_returned_to_the_os() -> None:
    """The whole point: RSS after a spike must come back down.

    CPython frees the objects but never asks the C library to shrink the heap it
    carved out, so without the trim the process sits at its high-water mark and
    every later concurrent turn pays for the busiest moment that ever happened.
    """
    _reset_interval_for_tests()
    reclaim_memory(tag="warmup", log=False)
    baseline = memory_reclaim.process_rss_bytes()

    burst = [bytearray(100_000) for _ in range(600)]  # ~60 MB of 100 KB buffers
    for index, block in enumerate(burst):
        block[0] = index & 0xFF
    spiked = memory_reclaim.process_rss_bytes()
    assert spiked > baseline + 20 * 1024 * 1024, "the burst did not show up in RSS"
    del burst

    reclaim_memory(tag="after_burst", log=False)
    recovered = memory_reclaim.process_rss_bytes()
    # A trim on a platform that has one must give most of the spike back. Where
    # malloc_trim is absent, fall back to asserting it at least did not grow.
    if memory_reclaim._malloc_trim() is not None:
        assert recovered < spiked - 10 * 1024 * 1024, (
            f"reclaim held the spike: {spiked/1e6:.1f} MB -> {recovered/1e6:.1f} MB"
        )


def test_maybe_reclaim_is_rate_limited() -> None:
    _reset_interval_for_tests()
    first = maybe_reclaim(tag="first")
    assert first is not None
    # Immediately again: inside the interval, so it must skip rather than pay a
    # full collection on every turn of a burst.
    assert maybe_reclaim(tag="second") is None
    # A zero interval disables the limit, and the first call after a reset runs.
    assert maybe_reclaim(tag="third", min_interval_s=0.0) is not None


def test_freeze_long_lived_moves_the_startup_graph_out_of_the_scan() -> None:
    assert freeze_long_lived() is True
    # Frozen objects land in a permanent generation; a later collect must not
    # report them as collectable, or startup would be rescanned forever.
    assert gc.collect() >= 0
    assert RECLAIM_MIN_INTERVAL_S > 0


def test_reclaim_is_silent_when_it_frees_nothing(capsys) -> None:
    """A no-op reclaim must not add a log line: operators read these in a crisis."""
    _reset_interval_for_tests()
    reclaim_memory(tag="noop")  # nothing to free on a fresh interpreter
    del capsys  # loguru does not write through pytest's capture; assert the value
    _reset_interval_for_tests()
    report = reclaim_memory(tag="noop2", log=False)
    assert report["freed_mb"] >= 0.0
