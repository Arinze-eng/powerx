"""A futile page-cache sweep must stop, because on this box it costs and never pays.

Measured on the live deployment across one 6 h window of real traffic, every
single charge-guard sweep returned the same thing::

    charge_guard_page_cache cgroup_ok=False asked_mb=1475.0 dropped_mb=0.0    (x79)

~240 files walked, ~1.4 GB *asked* about, 0 MB handed back. ``POSIX_FADV_DONTNEED``
only drops clean, unreferenced pages, and on a busy gateway the pages holding the
charge are the ones still in use -- so the ask can never be satisfied.

That is not merely wasted work. The walk opens and ``stat``s every large file in
the tree, and the kernel charges *this* cgroup for the dentries and inodes that
creates. The boot sweep recorded the exchange in one line: 286 MB of cache out,
105 MB of unreclaimable slab in. A sweep that frees nothing therefore converts
reclaimable pages into unreclaimable ones on the iteration that is racing the
ceiling -- which is the turn the platform then OOM-kills, surfacing to the user
as ``503 no healthy upstream``.

So the breaker exists to make the guard stop when it has proved it cannot help.
"""

from __future__ import annotations

import os

import pytest

from nanobot.utils import memory_reclaim


@pytest.fixture
def no_interval(monkeypatch):
    """Remove the rate limit so the test drives the futility counter directly."""
    monkeypatch.setattr(memory_reclaim, "_SWEEP_MIN_INTERVAL_S", 0.0)


@pytest.fixture
def refused_cgroup(monkeypatch, tmp_path):
    """No writable memory.reclaim interface: the sweep is the only lever."""
    monkeypatch.setattr(memory_reclaim, "_CGROUP_RECLAIM_PATHS", (tmp_path / "absent",))


@pytest.fixture
def unreadable_stat(monkeypatch):
    """``file`` unreadable, so every sweep measures dropped_mb=None."""
    monkeypatch.setattr(memory_reclaim, "container_memory_file_cache_bytes", lambda: None)


def _counting_sweep(monkeypatch, *, dropped_mb):
    """Replace the walk with a counter, and report a fixed result."""
    calls: list[int] = []

    def fake(roots=None, **kwargs):
        calls.append(1)
        return {"files": 240, "asked_mb": 1475.0, "dropped_mb": dropped_mb, "roots": []}

    monkeypatch.setattr(memory_reclaim, "fadvise_page_cache", fake)
    return calls


def test_repeatedly_useless_sweep_stops_the_walk(refused_cgroup, unreadable_stat, no_interval, monkeypatch):
    """N futile sweeps in a row must open the breaker and stop walking."""
    calls = _counting_sweep(monkeypatch, dropped_mb=0.0)

    for _ in range(memory_reclaim._SWEEP_FUTILITY_LIMIT):
        memory_reclaim.reclaim_page_cache(roots=[], log=False)
    assert len(calls) == memory_reclaim._SWEEP_FUTILITY_LIMIT

    # Past the limit the walk is paused: the same call no longer touches the tree.
    for _ in range(5):
        result = memory_reclaim.reclaim_page_cache(roots=[], log=False)
    assert len(calls) == memory_reclaim._SWEEP_FUTILITY_LIMIT, "breaker did not open"
    assert "breaker" in str(result.get("skipped", ""))


def test_useful_sweep_resets_the_counter(refused_cgroup, unreadable_stat, no_interval, monkeypatch):
    """A sweep that actually returns cache must not be counted against the breaker."""
    results = [0.0, 0.0, 120.0, 0.0, 0.0]
    idx = {"n": 0}

    def fake(roots=None, **kwargs):
        value = results[idx["n"]]
        idx["n"] += 1
        return {"files": 10, "asked_mb": 200.0, "dropped_mb": value, "roots": []}

    monkeypatch.setattr(memory_reclaim, "fadvise_page_cache", fake)

    for _ in results:
        memory_reclaim.reclaim_page_cache(roots=[], log=False)

    assert memory_reclaim._sweep_open_until == 0.0, (
        "a sweep that handed back 120 MB was treated as futile"
    )


def test_walk_is_rate_limited_between_sweeps(refused_cgroup, unreadable_stat, monkeypatch):
    """One walk per charge window, not one per model iteration."""
    monkeypatch.setattr(memory_reclaim, "_SWEEP_MIN_INTERVAL_S", 3600.0)
    calls = _counting_sweep(monkeypatch, dropped_mb=0.0)

    for _ in range(10):
        memory_reclaim.reclaim_page_cache(roots=[], log=False)

    assert len(calls) == 1, "the tree was walked once per iteration"


def test_permission_refusal_of_memory_reclaim_is_latched(tmp_path, monkeypatch):
    """A root-only interface stays root-only for the life of the process."""
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("permission bits are not enforced for root")

    locked = tmp_path / "memory.reclaim"
    locked.write_text("")
    locked.chmod(0o000)
    monkeypatch.setattr(memory_reclaim, "_CGROUP_RECLAIM_PATHS", (locked,))

    first = memory_reclaim.reclaim_cgroup_charge(amount_bytes=1)
    assert first["ok"] is False
    assert memory_reclaim._cgroup_reclaim_denied is True

    # Second call must not even re-open the file.
    locked.chmod(0o644)
    second = memory_reclaim.reclaim_cgroup_charge(amount_bytes=1)
    assert second["ok"] is False
    assert "latched" in second["reason"]


def test_directory_refusal_is_not_latched(tmp_path, monkeypatch):
    """Only permission-style refusals latch; a wrong path is worth retrying."""
    monkeypatch.setattr(memory_reclaim, "_CGROUP_RECLAIM_PATHS", (tmp_path,))

    result = memory_reclaim.reclaim_cgroup_charge(amount_bytes=1)

    assert result["ok"] is False
    assert memory_reclaim._cgroup_reclaim_denied is False
