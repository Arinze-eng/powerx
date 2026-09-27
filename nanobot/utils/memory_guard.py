"""Container-aware memory telemetry and pressure checks.

The deployment runs in a cgroup with a hard memory limit (the Northflank
``nf-compute-*`` deployment plan). Exceeding that limit does not raise a Python
exception — the kernel kills the process, the container restarts in place with
no new deployment and no shutdown log line, and the user sees the task vanish.
On a 512 MB plan that reads as "the sandbox crashed" even though the sandbox is
in another datacentre and was never called.

Nothing in the process used to observe its own memory, so the kill was silent:
a log scan for ``Killed`` / ``MemoryError`` / ``OOMKilled`` over the window of a
real incident returned zero lines. This module makes the ceiling observable
from inside the process:

* :func:`memory_snapshot` reads the cgroup's own usage and limit, falling back
  to ``VmRSS`` and ``ru_maxrss`` when it is not running under a cgroup.
* :func:`log_memory` emits one greppable line (``MEMORY``) so usage at any
  point — gateway start, each model iteration — can be reconstructed from logs.
* :func:`memory_pressure` grades usage so callers can warn, trim caches, or
  refuse heavy work before the kernel decides for them.

Reading is best-effort and never raises: telemetry must not be a new way to
fail a turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

__all__ = [
    "MEMORY_CRITICAL_RATIO",
    "MEMORY_WARN_RATIO",
    "container_memory_limit_bytes",
    "container_memory_used_bytes",
    "log_memory",
    "memory_pressure",
    "memory_snapshot",
    "process_rss_bytes",
]

# Fractions of the container limit at which usage is worth a warning, and at
# which a caller should refuse to start more work. The gap between them is the
# headroom a single turn needs: a 2M-token prompt is ~52 MB on its own, and one
# model iteration holds the request body, tool schemas and the response at once.
MEMORY_WARN_RATIO = 0.80
MEMORY_CRITICAL_RATIO = 0.92

_CGROUP_V2 = Path("/sys/fs/cgroup")
_CGROUP_V1 = Path("/sys/fs/cgroup/memory")
_MB = 1024 * 1024


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def container_memory_limit_bytes() -> int | None:
    """Return the cgroup memory limit in bytes, or ``None`` when unlimited/unknown."""
    for candidate in (
        _CGROUP_V2 / "memory.max",           # cgroup v2
        _CGROUP_V1 / "memory.limit_in_bytes",  # cgroup v1
    ):
        value = _read_int(candidate)
        # v1 reports a sentinel near 2**63 when no limit is set.
        if value is not None and value < 1 << 62:
            return value
    return None


def container_memory_used_bytes() -> int | None:
    """Return the cgroup's current memory usage, or ``None`` when unavailable.

    This is the whole cgroup (all processes in the container), which is what the
    kernel compares against the limit — so it is the number that decides a kill.
    """
    for candidate in (
        _CGROUP_V2 / "memory.current",           # cgroup v2
        _CGROUP_V1 / "memory.usage_in_bytes",    # cgroup v1
    ):
        value = _read_int(candidate)
        if value is not None:
            return value
    return None


def process_rss_bytes() -> int:
    """Return this process's resident set size in bytes."""
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:  # pragma: no cover - telemetry must never raise
        return 0


def memory_pressure(used: int | None = None, limit: int | None = None) -> str:
    """Grade memory usage as ``ok``, ``warn``, ``critical`` or ``unknown``."""
    if used is None:
        used = container_memory_used_bytes()
    if limit is None:
        limit = container_memory_limit_bytes()
    if not limit or used is None:
        return "unknown"
    ratio = used / limit
    if ratio >= MEMORY_CRITICAL_RATIO:
        return "critical"
    if ratio >= MEMORY_WARN_RATIO:
        return "warn"
    return "ok"


def memory_snapshot() -> dict[str, Any]:
    """Return a JSON-ready view of container and process memory."""
    limit = container_memory_limit_bytes()
    used = container_memory_used_bytes()
    cgroup_used = used
    if used is None:
        used = process_rss_bytes()
    ratio = (used / limit) if (limit and used) else None
    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "used_mb": round(used / _MB, 1) if used else 0.0,
        "limit_mb": round(limit / _MB, 1) if limit else None,
        "pct": round(ratio * 100, 1) if ratio is not None else None,
        "cgroup_used_bytes": cgroup_used,
        "rss_bytes": process_rss_bytes(),
        "rss_mb": round(process_rss_bytes() / _MB, 1),
        "pressure": memory_pressure(used, limit),
    }


def log_memory(tag: str, **extra: Any) -> dict[str, Any]:
    """Log one ``MEMORY`` line for ``tag`` and return the snapshot.

    Grep ``MEMORY`` in the service logs to reconstruct the usage curve, and match
    the last line before a silent container restart against the plan's limit.
    """
    snapshot = memory_snapshot()
    fields = " ".join(f"{key}={value}" for key, value in extra.items())
    message = (
        "MEMORY tag={} pressure={} pct={} used_mb={} limit_mb={} rss_mb={}{}"
    ).format(
        tag,
        snapshot["pressure"],
        snapshot["pct"],
        snapshot["used_mb"],
        snapshot["limit_mb"],
        snapshot["rss_mb"],
        (" " + fields) if fields else "",
    )
    try:
        if snapshot["pressure"] == "critical":
            logger.warning(message)
        else:
            logger.info(message)
    except Exception:  # pragma: no cover - telemetry must never raise
        pass
    return snapshot
