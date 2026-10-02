"""Hand memory back to the OS after a burst.

A CPython gateway never shrinks. Freed arenas stay mapped, and glibc keeps a
per-thread arena pool, so RSS after a heavy turn stays at its high-water mark for
the life of the process -- charged against every later user even while they are
idle. On a fixed deployment plan (a 488 MiB cgroup here) that is the difference
between "fits" and "the kernel kills the container mid-task with no exception and
no shutdown line", because the ceiling is per *container*, not per session. Every
megabyte the process holds while doing nothing is a megabyte some other
concurrent turn cannot use.

Three levers, all cheap and all no-ops where they do not apply:

* :func:`freeze_long_lived` moves the objects built during startup -- config, tool
  registries, provider pools -- into the permanent generation, so every later
  collection stops rescanning them. They are never garbage, and scanning a
  couple-hundred-megabyte graph on every collection is pure overhead.
* ``gc.collect()`` reclaims reference cycles the generational collector left
  behind. Tools that build one-shot object graphs (parsers, image pipelines,
  sandbox clients) leak into the heap this way.
* ``malloc_trim(0)`` asks glibc to return free heap to the kernel. CPython frees
  its small-object arenas but never asks the C library to shrink the regions it
  carved out of the OS, so RSS only ever ratchets up without this.

Every function here is best effort and never raises: reclaiming memory is
housekeeping, and housekeeping must not be a new way to fail a turn.
"""

from __future__ import annotations

import ctypes
import gc
import sys
import time
from typing import Any

from loguru import logger

from nanobot.utils.memory_guard import (
    MEMORY_CHARGE_WARN_RATIO,
    container_memory_charge_ratio,
    process_rss_bytes,
)

__all__ = [
    "RECLAIM_MIN_INTERVAL_S",
    "freeze_long_lived",
    "maybe_reclaim",
    "reclaim_if_charge_high",
    "reclaim_memory",
]

#: Shortest gap between two automatic reclaims. A full collection on a large heap
#: costs tens of milliseconds, so this bounds the work a burst can trigger while
#: still capping the high-water mark: without it RSS ratchets to the peak of the
#: busiest moment and stays there, and the next concurrent user pays for it.
RECLAIM_MIN_INTERVAL_S = 30.0

_last_reclaim_at: float = 0.0


def _malloc_trim() -> int | None:
    """Ask glibc to return free heap to the kernel. ``None`` when unavailable.

    ``malloc_trim`` is a GNU extension: musl and macOS do not have it, and a
    platform without it is simply skipped rather than treated as an error.
    """
    try:
        libc = ctypes.CDLL("libc.so.6")
    except OSError:
        try:
            libc = ctypes.CDLL(None)
        except OSError:
            return None
    trim = getattr(libc, "malloc_trim", None)
    if trim is None:
        return None
    try:
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return int(trim(0))
    except Exception:  # noqa: BLE001 - best effort by definition
        return None


def freeze_long_lived() -> bool:
    """Move everything currently alive into the permanent generation.

    Call once, after startup has built its long-lived graph and before the first
    turn. Objects built later are still collected normally; only what exists at
    this moment stops being rescanned. Returns whether it happened.
    """
    try:
        gc.collect()
        gc.freeze()
        return True
    except Exception as exc:  # noqa: BLE001 - never fail startup for this
        logger.debug("memory_reclaim: gc.freeze() unavailable: {}", exc)
        return False


def reclaim_memory(*, tag: str = "reclaim", log: bool = True) -> dict[str, Any]:
    """Collect cycles, return free heap, and report what it recovered.

    Returns ``{"rss_before_mb", "rss_after_mb", "freed_mb", "trimmed"}``. Never
    raises and never blocks for long: the caller decides when to spend the
    milliseconds, this only does the work.
    """
    global _last_reclaim_at
    before = process_rss_bytes()
    collected: int | None = None
    try:
        collected = gc.collect()
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory_reclaim: gc.collect() failed: {}", exc)
    trimmed = _malloc_trim()
    after = process_rss_bytes()
    _last_reclaim_at = time.monotonic()
    result = {
        "rss_before_mb": round(before / (1024 * 1024), 1),
        "rss_after_mb": round(after / (1024 * 1024), 1),
        "freed_mb": round((before - after) / (1024 * 1024), 1),
        "collected": collected,
        "trimmed": trimmed,
    }
    if log and result["freed_mb"] >= 1.0:
        # Greppable like the MEMORY lines, and only when it did something: a
        # reclaim that freed nothing is noise in a log an operator reads under
        # pressure.
        logger.info(
            "MEMORY tag={} rss_before_mb={} rss_after_mb={} freed_mb={} collected={} trimmed={}",
            tag,
            result["rss_before_mb"],
            result["rss_after_mb"],
            result["freed_mb"],
            collected,
            trimmed,
        )
    return result


def maybe_reclaim(*, tag: str = "auto", min_interval_s: float = RECLAIM_MIN_INTERVAL_S) -> dict[str, Any] | None:
    """Reclaim, but at most once per *min_interval_s*. Returns ``None`` when skipped.

    Rate limited so a burst of turns cannot pay the collection cost over and
    over. The first call always runs.
    """
    now = time.monotonic()
    if _last_reclaim_at and (now - _last_reclaim_at) < max(0.0, float(min_interval_s)):
        return None
    return reclaim_memory(tag=tag)


def reclaim_if_charge_high(
    *, tag: str = "charge_guard", ratio: float | None = None
) -> dict[str, Any] | None:
    """Reclaim when the raw cgroup charge is near the plan's ceiling.

    Deliberately *not* rate limited. ``maybe_reclaim`` caps the high-water
    mark of a busy gateway, but it also skips a call that arrives inside its
    interval -- and the moment the charge is high is exactly the moment a
    skip is unaffordable. Returns ``None`` when the charge is unknown or
    below ``ratio`` (default :data:`MEMORY_CHARGE_WARN_RATIO`), so the caller
    can put this on every model request for the cost of one cgroup read.

    The charge, not anonymous memory, is what the platform compares against
    the deployment plan's limit; see :mod:`nanobot.utils.memory_guard`.
    """
    threshold = (
        MEMORY_CHARGE_WARN_RATIO if ratio is None else float(ratio)
    )
    current = container_memory_charge_ratio()
    if current is None or current < threshold:
        return None
    return reclaim_memory(tag=tag)


def _reset_interval_for_tests() -> None:
    """Clear the rate-limit clock. Tests only."""
    global _last_reclaim_at
    _last_reclaim_at = 0.0


if sys.platform == "win32":  # pragma: no cover - documented, not exercised
    # ``malloc_trim`` is POSIX-only; everything else here still applies.
    pass
