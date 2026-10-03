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
import os
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.utils.memory_guard import (
    MEMORY_CHARGE_WARN_RATIO,
    container_memory_charge_ratio,
    container_memory_file_cache_bytes,
    memory_snapshot,
    process_rss_bytes,
)

__all__ = [
    "RECLAIM_MIN_INTERVAL_S",
    "fadvise_page_cache",
    "freeze_long_lived",
    "maybe_reclaim",
    "reclaim_cgroup_charge",
    "reclaim_if_charge_high",
    "reclaim_memory",
    "reclaim_page_cache",
]

#: Shortest gap between two automatic reclaims. A full collection on a large heap
#: costs tens of milliseconds, so this bounds the work a burst can trigger while
#: still capping the high-water mark: without it RSS ratchets to the peak of the
#: busiest moment and stays there, and the next concurrent user pays for it.
RECLAIM_MIN_INTERVAL_S = 30.0

_last_reclaim_at: float = 0.0

#: cgroup v2's page-cache reclaim interface. Writing a byte count asks the kernel
#: to hand back up to that much of *this* cgroup's reclaimable memory. Mode 0200
#: owned by root on a normal container, so a gateway that has dropped to an
#: unprivileged user can only see the refusal; the entrypoint uses it at boot
#: while it still has root. A tuple so tests can point it at a temp file.
_CGROUP_RECLAIM_PATHS = (Path("/sys/fs/cgroup/memory.reclaim"),)

#: Fraction of the measured reclaimable figure to ask the kernel for. An oversize
#: write is rejected outright (``EIO`` -- verified against the running kernel, so
#: ``100M`` fails where ``1M`` succeeds), and ``reclaimable_bytes`` is an upper
#: bound because it also counts kernel and shared memory this interface will not
#: hand back. Hold a margin back rather than lose the whole write.
_CGROUP_RECLAIM_MARGIN = 0.75

#: Bounds on the fadvise sweep. It runs on every model iteration once the charge
#: is high, so it must stay a bounded number of syscalls rather than a crawl; and
#: a file under the floor is not worth a syscall to hand back a fraction of one.
#: Page cache is what a build leaves behind, and a build leaves large files.
_FADVISE_MAX_FILES = 512
_FADVISE_MIN_FILE_BYTES = 1 * 1024 * 1024


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


def _default_page_cache_roots() -> list[Path]:
    """Directories whose page cache this deployment wants back.

    The agent workspace and the data dir are what a turn writes; the toolchain
    caches are what installing or running a CLI fills. Missing paths are skipped
    by the sweep, so listing a directory this deployment does not have is free.
    """
    roots: list[Path] = []
    try:
        from nanobot.config.paths import get_data_dir, get_workspace_path

        roots.append(get_workspace_path())
        roots.append(get_data_dir())
    except Exception:  # noqa: BLE001 - a missing config must not fail housekeeping
        pass
    home = Path(os.path.expanduser("~"))
    roots.extend((home / ".npm", home / ".cache", home / ".vercel"))
    return roots


def _iter_cacheable_files(root: Path, *, max_files: int) -> Iterator[Path]:
    """Yield up to *max_files* regular files under *root*, depth-first.

    ``scandir`` is consumed lazily rather than materialised: this runs on every
    model iteration once the charge is high, and a volume here holds hundreds of
    thousands of files. Symlinks are not followed, so the sweep cannot be walked
    out of the directories it was pointed at.
    """
    if max_files <= 0:
        return
    stack = [root]
    yielded = 0
    while stack and yielded < max_files:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yielded += 1
                        yield Path(entry.path)
                        if yielded >= max_files:
                            return
                except OSError:
                    continue


def fadvise_page_cache(
    roots: Sequence[str | Path] | None = None,
    *,
    max_files: int = _FADVISE_MAX_FILES,
    min_file_bytes: int = _FADVISE_MIN_FILE_BYTES,
) -> dict[str, Any]:
    """Drop clean page cache for this deployment's own files. Needs no privileges.

    ``POSIX_FADV_DONTNEED`` on a file the process has been reading or writing is
    the one cache lever available to an unprivileged gateway, and it targets
    exactly the cache a build leaves behind: the project it wrote and the
    toolchain it read back. Only *clean* pages are dropped, so this can never
    lose data, and a file still being written keeps its dirty pages.

    ``asked_mb`` is the summed *size* of the files the sweep reached, which is an
    upper bound and not a result: a file contributes its whole length whether or
    not any of it was cached, and ``POSIX_FADV_DONTNEED`` drops only the clean
    pages that are not referenced. Measured on this kernel against a directory of
    freshly read files, a sweep that asked for 192 MB handed back 161 MB of
    cgroup charge -- close, but not the same number, and the gap grows when the
    cache is being re-read by a running build. ``dropped_mb`` is therefore the
    measured figure: the cgroup's own page-cache charge read either side of the
    sweep. It is ``None`` where ``memory.stat`` is unreadable, never a guess.

    Returns ``{"files", "asked_mb", "dropped_mb", "roots"}``. Never raises:
    reclaiming is housekeeping, and housekeeping must not become a new way to
    fail a turn.
    """
    targets = [
        Path(root)
        for root in (roots if roots is not None else _default_page_cache_roots())
    ]
    cache_before = container_memory_file_cache_bytes()
    files = 0
    asked = 0
    walked: list[str] = []
    for root in targets:
        if files >= max_files:
            break
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        walked.append(str(root))
        for path in _iter_cacheable_files(root, max_files=max_files - files):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < min_file_bytes:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                continue
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except (OSError, AttributeError):
                # No fadvise on this platform, or the file went away between the
                # stat and the open. Either way it is not this sweep's problem.
                continue
            finally:
                os.close(fd)
            files += 1
            asked += size
    dropped: float | None = None
    cache_after = container_memory_file_cache_bytes()
    if cache_before is not None and cache_after is not None:
        # Never negative: another thread can fault a page in between the two
        # reads, and a negative "handed back" would be nonsense in a log line.
        dropped = round(max(cache_before - cache_after, 0) / (1024 * 1024), 1)
    return {
        "files": files,
        "asked_mb": round(asked / (1024 * 1024), 1),
        "dropped_mb": dropped,
        "roots": walked,
    }


def reclaim_cgroup_charge(*, amount_bytes: int | None = None) -> dict[str, Any]:
    """Ask the kernel for this cgroup's page cache in a single write.

    ``memory.reclaim`` is the only interface that returns the *whole* cgroup's
    cache rather than the files the process happens to know about, and it is
    deliberately root-owned: a deployment that runs the gateway as an
    unprivileged user will be refused at runtime, and the fadvise sweep carries
    the work there. The entrypoint calls this at boot, while it still has root.

    The kernel rejects a request larger than what is currently reclaimable, so
    the ask is the measured reclaimable figure less a margin, never "everything".

    Returns ``{"ok", "reason", "requested_mb"}``. Never raises.
    """
    reclaimable = memory_snapshot().get("reclaimable_bytes")
    if amount_bytes is None:
        if not reclaimable:
            return {"ok": False, "reason": "reclaimable unknown", "requested_mb": 0.0}
        amount_bytes = int(reclaimable * _CGROUP_RECLAIM_MARGIN)
    amount_bytes = max(int(amount_bytes), 0)
    requested_mb = round(amount_bytes / (1024 * 1024), 1)
    for path in _CGROUP_RECLAIM_PATHS:
        try:
            if not path.exists():
                continue
        except OSError:
            continue
        try:
            with open(path, "w", encoding="ascii") as handle:
                handle.write(str(amount_bytes))
        except OSError as exc:
            return {
                "ok": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "requested_mb": requested_mb,
            }
        return {"ok": True, "reason": "", "requested_mb": requested_mb}
    return {"ok": False, "reason": "memory.reclaim not present", "requested_mb": requested_mb}


def reclaim_page_cache(
    roots: Sequence[str | Path] | None = None, *, log: bool = True
) -> dict[str, Any]:
    """Hand back page cache: the part of the charge ``malloc_trim`` cannot reach.

    :func:`reclaim_memory` frees heap -- reference cycles and glibc arenas -- but
    the reading that gets a container replaced shows the opposite shape. Measured
    on the live deployment while a web-dev turn ran ``web_dev deploy``
    (2026-10-03):

        04:47:33 MEMORY ... pct=59.4 used_mb=289.9 cgroup_mb=401.9 cgroup_pct=82.3
        04:48:02 MEMORY ... pct=59.1 used_mb=288.4 cgroup_mb=450.1 cgroup_pct=92.2
        04:48:02 Gateway shutdown requested by SIGTERM

    About 48 MB arrived inside one iteration with the heap unchanged: page cache,
    from the CLI writing a project and reading a toolchain back. Nothing the
    process already did could touch it, so the charge guard fired, freed heap, and
    the container was recycled anyway -- taking the turn with it, and leaving the
    edge to answer 503 until the replacement booted.

    Both levers are best effort and never raise. The cgroup write is tried first
    because one syscall can return the whole charge; the sweep runs when it is
    refused, which is the normal case for an unprivileged gateway.
    """
    cache_before = container_memory_file_cache_bytes()
    cgroup = reclaim_cgroup_charge()
    result: dict[str, Any] = {
        "cgroup_ok": cgroup["ok"],
        "cgroup_reason": cgroup["reason"],
    }
    if cgroup["ok"]:
        # One write asked the kernel for the whole cgroup; nothing to walk.
        result["files"] = 0
        result["asked_mb"] = cgroup["requested_mb"]
        cache_after = container_memory_file_cache_bytes()
        result["dropped_mb"] = (
            round(max(cache_before - cache_after, 0) / (1024 * 1024), 1)
            if cache_before is not None and cache_after is not None
            else None
        )
    else:
        result.update(fadvise_page_cache(roots))
    if log:
        if cgroup["ok"] or result.get("asked_mb", 0) >= 1.0:
            logger.info(
                "MEMORY tag=charge_guard_page_cache cgroup_ok={} asked_mb={} dropped_mb={}"
                " files={} reason={}",
                cgroup["ok"],
                result.get("asked_mb"),
                result.get("dropped_mb"),
                result.get("files"),
                cgroup["reason"] or "-",
            )
        else:
            # An unprivileged cgroup refusal is the expected steady state, so it
            # is a debug line: an operator reading under pressure should not have
            # to filter a per-iteration EACCES out of the log.
            logger.debug(
                "memory_reclaim: no page cache handed back (cgroup: {})",
                cgroup["reason"],
            )
    return result


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
    the deployment plan's limit; see :mod:`nanobot.utils.memory_guard`. Both
    halves of that charge are handed back: the heap by :func:`reclaim_memory`
    and the reclaimable cache by :func:`reclaim_page_cache`, because the
    reading that gets a container replaced is usually the cache half.
    """
    threshold = (
        MEMORY_CHARGE_WARN_RATIO if ratio is None else float(ratio)
    )
    current = container_memory_charge_ratio()
    if current is None or current < threshold:
        return None
    result = dict(reclaim_memory(tag=tag))
    result["page_cache"] = reclaim_page_cache()
    return result


def _reset_interval_for_tests() -> None:
    """Clear the rate-limit clock. Tests only."""
    global _last_reclaim_at
    _last_reclaim_at = 0.0


if sys.platform == "win32":  # pragma: no cover - documented, not exercised
    # ``malloc_trim`` is POSIX-only; everything else here still applies.
    pass
