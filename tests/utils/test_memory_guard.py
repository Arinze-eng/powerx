"""Container memory telemetry must read the cgroup, grade it, and never raise.

The deployment is killed by the kernel when it crosses its cgroup memory limit.
Nothing in the process used to observe that limit, so a kill left no trace in
the logs and read to the user as "the sandbox crashed". These tests pin the
reading, the grading and the log line, plus the guarantee that missing or
unreadable cgroup files degrade to ``unknown`` instead of failing a turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from nanobot.utils import memory_guard


def _write_cgroup(root: Path, *, limit: str | None, current: str | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if limit is not None:
        (root / "memory.max").write_text(limit)
    if current is not None:
        (root / "memory.current").write_text(current)


@pytest.fixture
def cgroup_v2(tmp_path, monkeypatch):
    """Point the guard at a fake cgroup v2 tree and no v1 tree."""

    def _install(limit: str = str(512 * 1024 * 1024), current: str = str(128 * 1024 * 1024)):
        root = tmp_path / "cgroup"
        _write_cgroup(root, limit=limit, current=current)
        monkeypatch.setattr(memory_guard, "_CGROUP_V2", root)
        monkeypatch.setattr(memory_guard, "_CGROUP_V1", tmp_path / "absent-v1")
        return root

    return _install


def test_limit_and_usage_are_read_from_cgroup_v2(cgroup_v2) -> None:
    cgroup_v2(limit=str(512 * 1024 * 1024), current=str(256 * 1024 * 1024))

    assert memory_guard.container_memory_limit_bytes() == 512 * 1024 * 1024
    assert memory_guard.container_memory_used_bytes() == 256 * 1024 * 1024


def test_unlimited_max_reads_as_no_limit(cgroup_v2) -> None:
    """``max`` is the cgroup spelling of "no limit" and must not parse as a number."""
    cgroup_v2(limit="max")

    assert memory_guard.container_memory_limit_bytes() is None


def test_cgroup_v1_sentinel_is_not_treated_as_a_limit(tmp_path, monkeypatch) -> None:
    """An unset v1 limit is a sentinel near 2**63, not a real ceiling."""
    v1 = tmp_path / "memory"
    v1.mkdir()
    (v1 / "memory.limit_in_bytes").write_text(str(2**63 - 4096))
    (v1 / "memory.usage_in_bytes").write_text(str(64 * 1024 * 1024))
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", tmp_path / "absent-v2")
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", v1)

    assert memory_guard.container_memory_limit_bytes() is None
    assert memory_guard.container_memory_used_bytes() == 64 * 1024 * 1024


def test_missing_cgroup_files_report_unknown_without_raising(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", tmp_path / "nope")
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", tmp_path / "also-nope")

    snapshot = memory_guard.memory_snapshot()

    assert snapshot["limit_bytes"] is None
    assert snapshot["pressure"] == "unknown"
    # Falls back to this process's own RSS rather than reporting nothing.
    assert snapshot["rss_bytes"] > 0


def test_cgroup_usage_wins_over_process_rss(cgroup_v2) -> None:
    """The limit is compared against the whole cgroup, so that is what is graded."""
    cgroup_v2(limit=str(1000), current=str(950))

    snapshot = memory_guard.memory_snapshot()

    assert snapshot["used_bytes"] == 950
    assert snapshot["cgroup_used_bytes"] == 950
    assert snapshot["pressure"] == "critical"


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        (0, "ok"),
        (799, "ok"),
        (800, "warn"),       # exactly MEMORY_WARN_RATIO
        (919, "warn"),
        (920, "critical"),   # exactly MEMORY_CRITICAL_RATIO
        (1000, "critical"),
    ],
)
def test_pressure_grades_at_the_documented_boundaries(used, expected) -> None:
    assert memory_guard.memory_pressure(used, 1000) == expected


def test_pressure_without_a_limit_is_unknown() -> None:
    assert memory_guard.memory_pressure(500, None) == "unknown"
    assert memory_guard.memory_pressure(None, 1000) == "unknown"


def test_log_memory_emits_one_greppable_line(cgroup_v2) -> None:
    cgroup_v2(limit=str(512 * 1024 * 1024), current=str(64 * 1024 * 1024))
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(message), level="INFO")

    try:
        memory_guard.log_memory("gateway_start", version="9.9.9")
    finally:
        logger.remove(sink_id)

    line = "\n".join(captured)
    assert "MEMORY" in line
    assert "tag=gateway_start" in line
    assert "pressure=ok" in line
    assert "limit_mb=512.0" in line
    assert "used_mb=64.0" in line
    assert "version=9.9.9" in line


def test_log_memory_warns_at_critical_pressure(cgroup_v2) -> None:
    cgroup_v2(limit=str(1000), current=str(980))
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, message)),
        level="INFO",
    )

    try:
        snapshot = memory_guard.log_memory("model_iteration_start")
    finally:
        logger.remove(sink_id)

    assert snapshot["pressure"] == "critical"
    assert records and records[0][0] == "WARNING"
    assert "pressure=critical" in records[0][1]


def test_log_memory_returns_the_snapshot_used_for_the_line(cgroup_v2) -> None:
    cgroup_v2(limit=str(2000), current=str(1000))
    sink_id = logger.add(lambda message: None, level="INFO")
    try:
        snapshot = memory_guard.log_memory("check")
    finally:
        logger.remove(sink_id)

    assert snapshot["used_bytes"] == 1000
    assert snapshot["limit_bytes"] == 2000
    assert snapshot["pct"] == 50.0
