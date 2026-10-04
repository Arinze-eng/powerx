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


def _write_cgroup(
    root: Path,
    *,
    limit: str | None,
    current: str | None,
    anon: str | None = None,
    file_cache: str | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if limit is not None:
        (root / "memory.max").write_text(limit)
    if current is not None:
        (root / "memory.current").write_text(current)
    if anon is not None or file_cache is not None:
        # Real layout: one ``key value`` pair per line, bytes.
        lines = [f"anon {anon if anon is not None else 0}"]
        if file_cache is not None:
            lines.append(f"file {file_cache}")
        lines.append("kernel_stack 1048576")
        (root / "memory.stat").write_text("\n".join(lines) + "\n")


@pytest.fixture
def cgroup_v2(tmp_path, monkeypatch):
    """Point the guard at a fake cgroup v2 tree and no v1 tree."""

    def _install(
        limit: str = str(512 * 1024 * 1024),
        current: str = str(128 * 1024 * 1024),
        anon: str | None = None,
        file_cache: str | None = None,
    ):
        root = tmp_path / "cgroup"
        _write_cgroup(root, limit=limit, current=current, anon=anon, file_cache=file_cache)
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


def test_pressure_without_a_limit_is_unknown(tmp_path, monkeypatch) -> None:
    # Both trees missing: usage is unreadable, which must stay ``unknown``
    # rather than falling back to something invented.
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", tmp_path / "no-v2")
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", tmp_path / "no-v1")

    assert memory_guard.memory_pressure(500, None) == "unknown"
    # No readable usage is not the same as zero usage, and is never guessed.
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


def test_anonymous_memory_is_read_from_memory_stat(cgroup_v2) -> None:
    """v2 spells the unreclaimable part ``anon``; that is the number graded."""
    cgroup_v2(limit=str(1000), current=str(950), anon=str(300), file_cache=str(640))

    assert memory_guard.container_memory_anonymous_bytes() == 300


def test_file_cache_is_read_from_memory_stat(cgroup_v2) -> None:
    """The reclaimable half of the charge, which a fadvise sweep is meant to move.

    Read either side of a sweep it is the only honest measure of what was handed
    back: the summed size of the files a sweep reached is an upper bound, because
    a file contributes its whole length whether or not any of it was cached.
    """
    cgroup_v2(limit=str(1000), current=str(950), anon=str(300), file_cache=str(640))

    assert memory_guard.container_memory_file_cache_bytes() == 640


def test_file_cache_is_none_without_a_file_line(cgroup_v2) -> None:
    """An unknown cache figure must stay unknown rather than read as zero."""
    cgroup_v2(limit=str(1000), current=str(950), anon=str(300))

    assert memory_guard.container_memory_file_cache_bytes() is None


def test_cgroup_v1_names_file_cache_total_cache(tmp_path, monkeypatch) -> None:
    """v1 has no ``file``: the same quantity is ``total_cache``."""
    v1 = tmp_path / "memory"
    v1.mkdir()
    (v1 / "memory.stat").write_text("total_cache 314572800\nrss 104857600\n")
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", tmp_path / "absent-v2")
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", v1)

    assert memory_guard.container_memory_file_cache_bytes() == 314572800


def test_cgroup_v1_names_anonymous_memory_rss(tmp_path, monkeypatch) -> None:
    """v1 has no ``anon``: the same quantity is ``rss`` in its memory.stat."""
    v1 = tmp_path / "memory"
    v1.mkdir()
    (v1 / "memory.limit_in_bytes").write_text(str(512 * 1024 * 1024))
    (v1 / "memory.usage_in_bytes").write_text(str(400 * 1024 * 1024))
    (v1 / "memory.stat").write_text("cache 314572800\nrss 104857600\n")
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", tmp_path / "absent-v2")
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", v1)

    assert memory_guard.container_memory_anonymous_bytes() == 104857600
    assert memory_guard.memory_pressure() == "ok"


def test_reclaimable_cache_does_not_grade_as_critical(cgroup_v2) -> None:
    """The live false alarm: 488 MB charged against a 488 MB limit, mostly cache.

    Grading the raw charge reported ``pressure=critical pct=100.0`` on an idle
    gateway whose anonymous memory was a quarter of the limit. Page cache is
    reclaimable, so it must not be read as a process about to be killed.
    """
    cgroup_v2(limit="1000", current="1000", anon="250", file_cache="700")

    snapshot = memory_guard.memory_snapshot()

    assert snapshot["pressure"] == "ok"
    assert snapshot["used_bytes"] == 250
    assert snapshot["pct"] == 25.0
    assert snapshot["cgroup_used_bytes"] == 1000
    assert snapshot["reclaimable_bytes"] == 750


def test_a_real_heap_climb_still_grades_critical(cgroup_v2) -> None:
    """Reclaimable-aware grading must not hide the kill it exists to predict."""
    cgroup_v2(limit="1000", current="960", anon="940", file_cache="20")

    snapshot = memory_guard.memory_snapshot()

    assert snapshot["pressure"] == "critical"
    assert snapshot["anonymous_bytes"] == 940
    assert snapshot["reclaimable_bytes"] == 20


def test_grading_falls_back_to_the_raw_charge_without_memory_stat(cgroup_v2) -> None:
    """No memory.stat means no reclaimable figure — never invent headroom."""
    cgroup_v2(limit="1000", current="950")

    assert memory_guard.container_memory_anonymous_bytes() is None
    snapshot = memory_guard.memory_snapshot()
    assert snapshot["used_bytes"] == 950
    assert snapshot["anonymous_bytes"] is None
    assert snapshot["reclaimable_bytes"] is None
    assert snapshot["pressure"] == "critical"


def test_unparsable_memory_stat_is_ignored_not_fatal(tmp_path, monkeypatch) -> None:
    """A kernel that changes the table must degrade, not raise."""
    root = tmp_path / "cgroup"
    _write_cgroup(root, limit="1000", current="500")
    (root / "memory.stat").write_text("this is not a memory.stat\nanon not-a-number\n")
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", root)
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", tmp_path / "absent-v1")

    assert memory_guard.container_memory_anonymous_bytes() is None
    assert memory_guard.memory_snapshot()["used_bytes"] == 500


def test_log_memory_line_separates_heap_from_cache(cgroup_v2) -> None:
    """The line must let a reader tell a heap climb from cache pressure."""
    cgroup_v2(limit=str(488 * 1024 * 1024), current=str(488 * 1024 * 1024),
              anon=str(250 * 1024 * 1024), file_cache=str(230 * 1024 * 1024))
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")

    try:
        memory_guard.log_memory("gateway_start", version="9.9.9")
    finally:
        logger.remove(sink_id)

    line = "\n".join(captured)
    assert "pressure=ok" in line
    assert "used_mb=250.0" in line
    assert "cgroup_mb=488.0" in line
    assert "rss_mb=" in line
    assert "oom_kill=-" in line
    assert "version=9.9.9" in line


# --- memory.events: the kernel's own verdict, not our inference ----------------
#
# A container restarted at cgroup_pct=99.5 with no SIGTERM and no shutdown line
# is *probably* an OOM kill, and "probably" cost a full day of wrong theories.
# ``memory.events`` carries ``oom_kill``, which is not a inference: it counts the
# processes this cgroup has already had killed for memory.


def test_oom_kill_count_is_read_from_memory_events(cgroup_v2) -> None:
    root = cgroup_v2(limit=str(488 * 1024 * 1024), current=str(486 * 1024 * 1024))
    (root / "memory.events").write_text(
        "low 0\nhigh 0\nmax 12\noom 0\noom_kill 3\noom_group_kill 0\n"
    )

    assert memory_guard.container_oom_kill_count() == 3


def test_oom_kill_is_none_when_events_are_unreadable(cgroup_v2) -> None:
    """Absent must not collapse into zero -- only one of them is evidence."""
    cgroup_v2(limit=str(488 * 1024 * 1024), current=str(486 * 1024 * 1024))

    assert memory_guard.container_oom_kill_count() is None
    assert memory_guard.memory_snapshot()["oom_kill"] is None


def test_a_nonzero_oom_kill_escalates_the_log_line_even_when_charge_is_low(
    cgroup_v2,
) -> None:
    """A build subprocess reaped for memory is worth a WARNING at any charge.

    The gateway itself can sit comfortably at 40% while the npm it shelled out to
    has already been killed. Grading only the current charge hides exactly that.
    """
    root = cgroup_v2(limit=str(488 * 1024 * 1024), current=str(120 * 1024 * 1024))
    (root / "memory.events").write_text("max 0\noom 1\noom_kill 1\n")
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")

    try:
        memory_guard.log_memory("model_iteration_start")
    finally:
        logger.remove(sink_id)

    line = "\n".join(captured)
    assert "oom_kill=1" in line
    assert "WARNING" in line, "a recorded OOM kill logged as informational"
