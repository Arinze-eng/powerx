"""The container charge is what the platform enforces; the guard must read it.

Measured on the live deployment (container log, 2026-10-02):

    13:42:07 MEMORY tag=model_iteration_start pressure=ok pct=65.7
             used_mb=320.8 limit_mb=488.3 rss_mb=351.4 cgroup_mb=456.7
    13:42:07 Gateway shutdown requested by SIGTERM

The gateway graded itself ``pressure=ok`` because pressure is graded on
anonymous memory, and it was recycled by the platform in the same second at
93.5% of the plan limit. The number that decided the kill was the *raw cgroup
charge* -- heap plus page cache -- and nothing in the process looked at it.

These tests pin the missing signal and the reclaim it triggers: the charge is
published, a high charge is visible (and warned about) even while anonymous
pressure reads fine, and the guard acts on it without waiting out the
rate-limit window that exists for the cheap path.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest
from loguru import logger

from nanobot.utils import memory_guard, memory_reclaim
from nanobot.utils.memory_guard import MEMORY_CHARGE_WARN_RATIO
from nanobot.utils.memory_reclaim import reclaim_if_charge_high

_MB = 1024 * 1024


def _install_cgroup(monkeypatch, tmp_path, *, limit_mb, charge_mb, anon_mb):
    """Fake cgroup v2 tree with a limit, a raw charge and an anon figure."""
    root = tmp_path / "cgroup"
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.max").write_text(str(int(limit_mb * _MB)))
    (root / "memory.current").write_text(str(int(charge_mb * _MB)))
    (root / "memory.stat").write_text(f"anon {int(anon_mb * _MB)}\n")
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", root)
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", tmp_path / "absent-v1")
    return root


def _install_no_cgroup(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", tmp_path / "no-v2")
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", tmp_path / "no-v1")


# --------------------------------------------------------------------------- #
# reading the charge
# --------------------------------------------------------------------------- #

def test_charge_ratio_is_the_raw_charge_over_the_limit(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=456.7, anon_mb=320.8)

    assert memory_guard.container_memory_charge_ratio() == pytest.approx(456.7 / 488)


def test_snapshot_publishes_the_charge_percentage(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=456.7, anon_mb=320.8)

    snapshot = memory_guard.memory_snapshot()

    assert snapshot["charge_pct"] == pytest.approx(93.6, abs=0.1)
    # And the page cache is still not read as a process about to be killed.
    assert snapshot["pressure"] == "ok"


def test_charge_ratio_is_none_without_a_limit(monkeypatch, tmp_path) -> None:
    """No cgroup is not the same as an empty cgroup: never invent pressure."""
    _install_no_cgroup(monkeypatch, tmp_path)

    assert memory_guard.container_memory_charge_ratio() is None
    assert memory_guard.memory_snapshot()["charge_pct"] is None


# --------------------------------------------------------------------------- #
# the production incident: a high charge must be visible while anon reads fine
# --------------------------------------------------------------------------- #

def test_a_high_charge_warns_even_while_anonymous_pressure_is_ok(
    monkeypatch, tmp_path
) -> None:
    """The exact blind spot that let the container be replaced.

    ``pressure=ok`` on a 93%-charged container is not a contradiction -- it is
    the guard reading the wrong number. It must not be silent any more.
    """
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=456.7, anon_mb=320.8)
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, str(message))),
        level="INFO",
    )

    try:
        snapshot = memory_guard.log_memory("model_iteration_start", iteration=0)
    finally:
        logger.remove(sink_id)

    assert snapshot["pressure"] == "ok"
    assert records, "a 93%-charged container logged nothing"
    level, line = records[0]
    assert level == "WARNING"
    assert "cgroup_pct=93.6" in line


def test_a_healthy_charge_stays_at_info(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=390, anon_mb=285)
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, str(message))),
        level="INFO",
    )

    try:
        memory_guard.log_memory("idle_reclaim")
    finally:
        logger.remove(sink_id)

    assert [level for level, _ in records] == ["INFO"]


def test_the_warn_threshold_sits_below_the_observed_steady_state() -> None:
    """0.85 must not fire on the charge the same deployment showed while healthy.

    Post-restart idle charge measured 79-85% of the limit; the fatal turn
    reached 93.5%. A threshold in that gap acts on a real climb only.
    """
    assert 0.85 <= MEMORY_CHARGE_WARN_RATIO < 0.935


# --------------------------------------------------------------------------- #
# acting on it
# --------------------------------------------------------------------------- #

def test_reclaim_is_a_no_op_while_the_charge_is_low(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=300, anon_mb=270)
    called: list[str] = []
    monkeypatch.setattr(
        memory_reclaim, "reclaim_memory", lambda *, tag="reclaim", log=True: called.append(tag)
    )

    assert reclaim_if_charge_high(tag="charge_guard") is None
    assert called == []


def test_a_high_charge_reclaims_immediately(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=456.7, anon_mb=320.8)
    called: list[str] = []
    monkeypatch.setattr(
        memory_reclaim,
        "reclaim_memory",
        lambda *, tag="reclaim", log=True: (called.append(tag), {"freed_mb": 12.0})[1],
    )

    assert reclaim_if_charge_high(tag="charge_guard") is not None
    assert called == ["charge_guard"]


def test_the_charge_guard_is_not_rate_limited(monkeypatch, tmp_path) -> None:
    """The rate limit exists to skip *cheap* work, not to skip the kill.

    ``maybe_reclaim`` caps a busy gateway's high-water mark, but it also drops a
    call that arrives inside its window -- and a high charge is precisely when a
    dropped call is unaffordable. The guard must fire anyway.
    """
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=456.7, anon_mb=320.8)
    calls: list[str] = []
    monkeypatch.setattr(
        memory_reclaim,
        "reclaim_memory",
        lambda *, tag="reclaim", log=True: (calls.append(tag), {"freed_mb": 1.0})[1],
    )
    # Start a reclaim interval, exactly as a real reclaim just left it.
    memory_reclaim._last_reclaim_at = time.monotonic()

    # The cheap path is skipped inside its own window, as designed.
    assert memory_reclaim.maybe_reclaim(tag="turn_end") is None
    calls.clear()

    # Same instant, same window: the charge guard still acts.
    assert reclaim_if_charge_high(tag="charge_guard") is not None
    assert calls == ["charge_guard"]


def test_an_unknown_charge_does_not_reclaim(monkeypatch, tmp_path) -> None:
    _install_no_cgroup(monkeypatch, tmp_path)
    called: list[str] = []
    monkeypatch.setattr(
        memory_reclaim, "reclaim_memory", lambda *, tag="reclaim", log=True: called.append(tag)
    )

    assert reclaim_if_charge_high() is None
    assert called == []


# --------------------------------------------------------------------------- #
# wiring: it has to run before the request it is protecting
# --------------------------------------------------------------------------- #

def test_the_guard_runs_before_every_model_request() -> None:
    from nanobot.agent import runner

    src = inspect.getsource(runner)
    started = src.index('"model_iteration_start"')
    # Off the loop: the guard now reaches page cache too, which is filesystem
    # work, and a stalled loop is what fails the health probe. See
    # tests/utils/test_memory_page_cache_reclaim.py for that half.
    guard = src.index('await asyncio.to_thread(reclaim_if_charge_high, tag="charge_guard")')
    request = src.index("response = await self._request_model(")

    assert started < guard < request, (
        "the reclaim guard must sit between the iteration-start memory line and "
        "the model request it exists to make room for"
    )
