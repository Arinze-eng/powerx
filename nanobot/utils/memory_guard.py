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

Grading is deliberately *reclaimable-aware*. ``memory.current`` charges page
cache along with everything else, but the kernel reclaims clean cache under
pressure instead of killing the cgroup — so a container holding a small heap
next to a large read cache is idle, not doomed. Grading the raw charge is what
made a healthy idle gateway log ``pressure=critical pct=100.0``: 488 MB "used"
against a 488 MB limit, of which 243 MB was this process's own RSS and the rest
was cache. :func:`container_memory_anonymous_bytes` therefore reports the
``anon`` figure from ``memory.stat`` (the memory that cannot be handed back)
and that is what pressure is graded on; the raw cgroup charge is still reported
alongside it as ``cgroup_mb`` so a real anon climb stays visible.

Reading is best-effort and never raises: telemetry must not be a new way to
fail a turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

__all__ = [
    "MEMORY_CHARGE_WARN_RATIO",
    "MEMORY_CRITICAL_RATIO",
    "MEMORY_WARN_RATIO",
    "container_memory_anonymous_bytes",
    "container_memory_charge_ratio",
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
#: Fraction of the *raw* cgroup charge at which the reclaim guard acts.
#:
#: ``MEMORY_CRITICAL_RATIO`` grades anonymous memory, which is the right
#: number for "is this process about to be OOM-killed". It is the wrong
#: number for "is the platform about to replace this container": the plan's
#: ceiling is enforced against ``memory.max``, and that is charged the whole
#: cgroup -- heap *plus* page cache. Measured in production: a gateway graded
#: ``pressure=ok pct=65.7`` was replaced the same second at ``cgroup_mb=456.7``
#: of a 488.3 MiB limit (93.5%). The guard never saw it coming because it was
#: reading the other number.
#:
#: Expect this to trip on a freshly booted container too, and that is correct
#: rather than a false alarm: a replacement container reads its own image into
#: page cache and charges ~92% of the limit before it has served anything (the
#: kernel hands that back within a minute -- a later idle reclaim on the same
#: process measured 46.7%). Tripping there costs one collection, which is the
#: same thing ``idle_reclaim`` would have done a moment later anyway.
MEMORY_CHARGE_WARN_RATIO = 0.85

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
    """Return the cgroup's current memory charge, or ``None`` when unavailable.

    This is the whole cgroup (all processes in the container) *including* page
    cache, which is what the kernel charges against ``memory.max``. It is the
    raw number, not the one pressure is graded on: see
    :func:`container_memory_anonymous_bytes`.
    """
    for candidate in (
        _CGROUP_V2 / "memory.current",           # cgroup v2
        _CGROUP_V1 / "memory.usage_in_bytes",    # cgroup v1
    ):
        value = _read_int(candidate)
        if value is not None:
            return value
    return None


def container_memory_charge_ratio() -> float | None:
    """Return the raw cgroup charge as a fraction of the limit, or ``None``.

    This is the ratio the *platform* acts on. ``memory.current`` is charged
    against ``memory.max`` for the whole cgroup, so page cache counts: a
    container holding a modest heap next to a large read cache still reads as
    full to whoever decided the plan limit and watches it. Grading on
    anonymous memory answers a different question, so this is reported
    alongside it rather than replacing it.
    """
    charge = container_memory_used_bytes()
    limit = container_memory_limit_bytes()
    if not charge or not limit:
        return None
    return charge / limit


def _parse_memory_stat(text: str) -> dict[str, int]:
    """Parse a cgroup ``memory.stat`` table into ``{key: bytes}``.

    Two whitespace-separated columns, one key per line. Anything unexpected is
    skipped rather than raised: a diagnostic readout must not be able to fail a
    turn, and a kernel that adds a column later must not silence the whole line.
    """
    values: dict[str, int] = {}
    for line in str(text or "").splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            values[fields[0]] = int(fields[1])
        except ValueError:
            continue
    return values


def container_memory_anonymous_bytes() -> int | None:
    """Return the cgroup's anonymous memory in bytes, or ``None`` when unknown.

    Anonymous memory is heap, stacks and process-private mappings: the part the
    kernel cannot simply hand back, so it is the part that actually decides an
    OOM kill. Everything else in ``memory.current`` — page cache above all — is
    reclaimable and is released under pressure instead of killing the container.

    cgroup v2 names this ``anon``; v1 names the same quantity ``rss``.
    """
    for stat_path in (
        _CGROUP_V2 / "memory.stat",   # cgroup v2: anon
        _CGROUP_V1 / "memory.stat",   # cgroup v1: rss
    ):
        try:
            text = stat_path.read_text()
        except OSError:
            continue
        values = _parse_memory_stat(text)
        for key in ("anon", "rss"):
            if key in values:
                return values[key]
    return None


def _graded_memory_bytes() -> int | None:
    """The number pressure is graded on: anonymous memory when it is readable.

    Falling back to the raw charge keeps the guard honest on a kernel that does
    not expose ``memory.stat`` — it never invents headroom, it just stops
    treating reclaimable cache as though it were a live process.
    """
    anonymous = container_memory_anonymous_bytes()
    if anonymous is not None:
        return anonymous
    return container_memory_used_bytes()


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
    """Grade memory usage as ``ok``, ``warn``, ``critical`` or ``unknown``.

    ``used`` defaults to anonymous memory, so reclaimable page cache cannot
    grade a healthy container as critical (see the module docstring). Pass an
    explicit ``used`` to grade a raw number instead.
    """
    if used is None:
        used = _graded_memory_bytes()
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
    """Return a JSON-ready view of container and process memory.

    ``used_bytes``/``used_mb``/``pct`` describe the anonymous memory that
    pressure is graded on. ``cgroup_used_bytes`` is the raw charge including
    reclaimable cache, and ``reclaimable_bytes`` is the difference between the
    two — the headroom the kernel can take back before it starts killing.
    """
    limit = container_memory_limit_bytes()
    charge = container_memory_used_bytes()
    anonymous = container_memory_anonymous_bytes()
    used = anonymous if anonymous is not None else charge
    cgroup_used = charge
    if used is None:
        used = process_rss_bytes()
    ratio = (used / limit) if (limit and used) else None
    reclaimable = None
    if charge is not None and anonymous is not None:
        reclaimable = max(charge - anonymous, 0)
    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "used_mb": round(used / _MB, 1) if used else 0.0,
        "limit_mb": round(limit / _MB, 1) if limit else None,
        "pct": round(ratio * 100, 1) if ratio is not None else None,
        "cgroup_used_bytes": cgroup_used,
        "cgroup_used_mb": round(cgroup_used / _MB, 1) if cgroup_used else 0.0,
        "charge_pct": (
            round(cgroup_used / limit * 100, 1) if (cgroup_used and limit) else None
        ),
        "anonymous_bytes": anonymous,
        "reclaimable_bytes": reclaimable,
        "rss_bytes": process_rss_bytes(),
        "rss_mb": round(process_rss_bytes() / _MB, 1),
        "pressure": memory_pressure(used, limit),
    }


def log_memory(tag: str, **extra: Any) -> dict[str, Any]:
    """Log one ``MEMORY`` line for ``tag`` and return the snapshot.

    Grep ``MEMORY`` in the service logs to reconstruct the usage curve, and match
    the last line before a silent container restart against the plan's limit.
    ``used_mb`` is anonymous memory (what is graded); ``cgroup_mb`` is the raw
    cgroup charge, so the two together say whether a high reading was heap or
    reclaimable cache.
    """
    snapshot = memory_snapshot()
    fields = " ".join(f"{key}={value}" for key, value in extra.items())
    message = (
        "MEMORY tag={} pressure={} pct={} used_mb={} limit_mb={} rss_mb={}"
        " cgroup_mb={} cgroup_pct={}{}"
    ).format(
        tag,
        snapshot["pressure"],
        snapshot["pct"],
        snapshot["used_mb"],
        snapshot["limit_mb"],
        snapshot["rss_mb"],
        snapshot["cgroup_used_mb"],
        snapshot["charge_pct"],
        (" " + fields) if fields else "",
    )
    charge_pct = snapshot.get("charge_pct")
    charge_is_high = (
        charge_pct is not None
        and charge_pct >= MEMORY_CHARGE_WARN_RATIO * 100
    )
    try:
        if snapshot["pressure"] == "critical" or charge_is_high:
            logger.warning(message)
        else:
            logger.info(message)
    except Exception:  # pragma: no cover - telemetry must never raise
        pass
    return snapshot
