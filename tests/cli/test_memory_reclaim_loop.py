"""The idle memory reclaimer must actually run, and actually stop.

``reclaim_memory`` is only otherwise called at ``tool_batch``/``turn_end``, so an
idle gateway holds its worst-moment footprint for the life of the process while
the cgroup charges it. These tests pin the timer that closes that gap: it
reclaims without a turn, its cadence cannot spin, and a failure in housekeeping
never becomes a new way for the gateway to die.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from nanobot.cli import gateway_runtime


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #

def test_reclaim_loop_is_registered_as_a_gateway_task() -> None:
    """A loop nobody schedules is not a reclaimer."""
    src = inspect.getsource(gateway_runtime)
    assert "_memory_reclaim_loop(shutdown_event)" in src
    assert '"nanobot-memory-reclaim"' in src


def test_reclaim_loop_is_a_coroutine_function() -> None:
    assert asyncio.iscoroutinefunction(gateway_runtime._memory_reclaim_loop)


def test_gateway_start_returns_page_cache_not_only_heap() -> None:
    """The boot footprint is mostly cache, so ``gateway_ready`` must sweep it.

    ``reclaim_memory`` at gateway_ready reaches the heap only, and the idle loop
    that does sweep page cache does not tick for a minute. On a plan this small
    the container that dies during boot dies in that gap (production: two boots
    on 2026-10-03, 06:33:30 and 06:36:46, both ended before the port bound), so
    the boot reclaim has to hand the cache back itself, off the event loop.
    """
    src = inspect.getsource(gateway_runtime)
    assert "await asyncio.to_thread(reclaim_page_cache" in src
    assert '"gateway_ready_cache"' in src


# --------------------------------------------------------------------------- #
# cadence
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", ["", "not-a-number", "0", "1", "-5", "14.9"])
def test_cadence_is_floored_so_env_cannot_spin(raw: str, monkeypatch) -> None:
    """A junk or tiny value must not turn housekeeping into a busy loop."""
    monkeypatch.setenv("NANOBOT_MEMORY_RECLAIM_INTERVAL_S", raw)
    assert gateway_runtime._reclaim_interval_seconds() >= gateway_runtime._RECLAIM_MIN_INTERVAL_S


def test_cadence_defaults_to_a_minute_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("NANOBOT_MEMORY_RECLAIM_INTERVAL_S", raising=False)
    assert gateway_runtime._reclaim_interval_seconds() == 60.0


def test_cadence_honours_a_sane_operator_value(monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_MEMORY_RECLAIM_INTERVAL_S", "120")
    assert gateway_runtime._reclaim_interval_seconds() == 120.0


# --------------------------------------------------------------------------- #
# behaviour
# --------------------------------------------------------------------------- #

def _fast_interval(monkeypatch, seconds: float = 0.02) -> None:
    monkeypatch.setattr(
        gateway_runtime, "_reclaim_interval_seconds", lambda: seconds
    )


@pytest.fixture(autouse=True)
def _no_real_page_cache_sweep(monkeypatch):
    """Every cycle now also hands back page cache; keep that off the real disk.

    The sweep is bounded but real, and these tests must not depend on the host
    filesystem. Individual tests override this to assert what gets logged.
    """
    monkeypatch.setattr(
        gateway_runtime,
        "reclaim_page_cache",
        lambda *a, **k: {"asked_mb": 0.0, "files": 0, "cgroup_ok": False, "cgroup_reason": ""},
    )


def test_loop_reclaims_without_any_turn_and_logs_it(monkeypatch) -> None:
    """The whole point: memory comes back while the service is idle."""
    calls: list[str] = []
    logged: list[tuple[str, dict]] = []

    def fake_reclaim(*, tag: str = "reclaim", log: bool = True):
        calls.append(tag)
        return {"freed_mb": 7.5, "trimmed": 1}

    def fake_log_memory(tag: str, **extra):
        logged.append((tag, extra))
        return {}

    page_calls: list[str] = []

    def fake_page_cache(*, log: bool = True):
        page_calls.append("sweep")
        return {"asked_mb": 41.2, "files": 17, "cgroup_ok": False, "cgroup_reason": ""}

    monkeypatch.setattr(gateway_runtime, "reclaim_memory", fake_reclaim)
    monkeypatch.setattr(gateway_runtime, "reclaim_page_cache", fake_page_cache)
    monkeypatch.setattr(gateway_runtime, "log_memory", fake_log_memory)
    _fast_interval(monkeypatch)

    async def scenario() -> None:
        shutdown = asyncio.Event()
        task = asyncio.create_task(gateway_runtime._memory_reclaim_loop(shutdown))
        for _ in range(300):
            if calls:
                break
            await asyncio.sleep(0.01)
        shutdown.set()
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    assert calls == ["idle_reclaim"], calls
    assert page_calls == ["sweep"], "the idle cycle must hand back page cache too"
    assert logged, "each cycle must emit a greppable MEMORY line"
    assert logged[0][0] == "idle_reclaim"
    assert logged[0][1]["freed_mb"] == 7.5
    assert logged[0][1]["trimmed"] == 1
    assert logged[0][1]["cache_mb"] == 41.2
    assert logged[0][1]["cache_files"] == 17


def test_ready_shutdown_exits_without_reclaiming(monkeypatch) -> None:
    """Stopping must be immediate, not one more cycle of work."""
    calls: list[str] = []
    monkeypatch.setattr(
        gateway_runtime,
        "reclaim_memory",
        lambda *, tag="reclaim", log=True: calls.append(tag) or {"freed_mb": 0.0, "trimmed": 1},
    )
    _fast_interval(monkeypatch, seconds=30.0)

    async def scenario() -> None:
        shutdown = asyncio.Event()
        shutdown.set()  # already shutting down
        task = asyncio.create_task(gateway_runtime._memory_reclaim_loop(shutdown))
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    assert calls == [], "an already-set shutdown event must exit without reclaiming"


def test_a_failed_reclaim_does_not_kill_the_loop(monkeypatch) -> None:
    """Housekeeping must never be a new way for the gateway to die."""
    seen: list[str] = []

    def boom(*, tag: str = "reclaim", log: bool = True):
        seen.append(tag)
        raise RuntimeError("trim exploded")

    monkeypatch.setattr(gateway_runtime, "reclaim_memory", boom)
    _fast_interval(monkeypatch)

    async def scenario() -> tuple[int, bool]:
        shutdown = asyncio.Event()
        task = asyncio.create_task(gateway_runtime._memory_reclaim_loop(shutdown))
        for _ in range(600):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.01)
        alive = not task.done()
        shutdown.set()
        await asyncio.wait_for(task, timeout=3)
        return len(seen), alive

    attempts, survived = asyncio.run(scenario())
    assert attempts >= 2, "the loop stopped after one failed reclaim"
    assert survived, "a failed reclaim killed the loop task"


def test_loop_exits_on_cancellation(monkeypatch) -> None:
    """Gateway teardown cancels its tasks; this one must not resist."""
    monkeypatch.setattr(
        gateway_runtime,
        "reclaim_memory",
        lambda *, tag="reclaim", log=True: {"freed_mb": 0.0, "trimmed": 1},
    )
    _fast_interval(monkeypatch, seconds=30.0)

    async def scenario() -> bool:
        shutdown = asyncio.Event()
        task = asyncio.create_task(gateway_runtime._memory_reclaim_loop(shutdown))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(scenario()) is True