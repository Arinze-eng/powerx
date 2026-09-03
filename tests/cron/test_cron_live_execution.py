"""End-to-end live execution tests for the cron scheduler.

These tests prove the whole scheduling loop actually *fires*, not just that a
job can be added to the store:

    add_job -> start() (arms the timer) -> _on_timer() wakes at the due time
        -> _execute_job() runs the on_job callback -> next_run is recomputed.

They use a real asyncio event loop and short real clock delays so the test is a
true integration check of the timer path (not a mocked-out unit test).

Regressions guarded here (why this file exists in this fork):
  * A scheduled "reminder/assignment" that is reported as registered but never
    actually runs. This is the exact failure the operator hit on Render: the
    tool said the job was scheduled, but the timer never delivered the task.
  * every-schedule jobs must keep rescheduling (not die after round one).
  * at-schedule (one-shot) jobs must complete and then be disabled/deleted.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule

SHORT = 40  # ms — well under the 5-minute default max_sleep so _arm_timer ties
# precisely to the due time and fires within the test window.


async def _wait_for(predicate, timeout_s: float = 10.0) -> None:
    """Spin until *predicate* returns truthy or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


async def test_every_job_actually_executes_and_reschedules(tmp_path: Path) -> None:
    """A recurring (every) job must fire the on_job callback and then arm
    itself again for the next interval — proving the scheduler loop runs."""
    store = CronService(tmp_path / "cron" / "jobs.json", max_sleep_ms=200)
    runs: list[str] = []

    async def on_job(job):
        runs.append(job.name)
        return "ok"

    store.on_job = on_job
    job = store.add_job(
        name="assignment-reminder",
        schedule=CronSchedule(kind="every", every_ms=SHORT),
        message="Run the assignment now",
    )
    assert job.id

    await store.start()
    try:
        await _wait_for(lambda: len(runs) >= 2)
        # The job actually executed through the timer path.
        assert "assignment-reminder" in runs
        state = store.get_job(job.id)
        assert state is not None
        assert state.state.last_status == "ok"
        assert state.state.last_run_at_ms
        # A recurring job must still be enabled and due again.
        assert state.enabled is True
        assert state.state.next_run_at_ms is not None
        assert state.state.next_run_at_ms > state.state.last_run_at_ms
    finally:
        store.stop()


async def test_one_shot_at_job_runs_once_then_turns_off(tmp_path: Path) -> None:
    """A one-time (at) job must fire exactly once and then be disabled."""
    store = CronService(tmp_path / "cron" / "jobs.json", max_sleep_ms=200)
    runs: list[str] = []

    async def on_job(job):
        runs.append(job.name)
        return "done"

    store.on_job = on_job
    due = int(time.time() * 1000) + SHORT
    job = store.add_job(
        name="one-shot-assignment",
        schedule=CronSchedule(kind="at", at_ms=due),
        message="Do the one-time thing",
        delete_after_run=True,
    )
    await store.start()
    try:
        await _wait_for(lambda: len(runs) == 1)
        # It ran.
        assert runs == ["one-shot-assignment"]
    finally:
        store.stop()
    # After stop, the one-shot job was deleted (delete_after_run) so it is gone.
    assert store.get_job(job.id) is None or store.get_job(job.id).enabled is False


async def test_job_survives_restart_and_refires(tmp_path: Path) -> None:
    """A persisted job must still fire after a fresh CronService instance
    (simulating a Render redeploy / process restart)."""
    store_path = tmp_path / "cron" / "jobs.json"

    s1 = CronService(store_path, max_sleep_ms=200)
    s1.add_job(
        name="persistent-assignment",
        schedule=CronSchedule(kind="every", every_ms=SHORT),
        message="Survive the restart",
    )
    # Running start() persists the store through _save_store().
    await s1.start()
    s1.stop()

    # New process, same store file.
    s2 = CronService(store_path, max_sleep_ms=200)
    runs: list[str] = []

    async def on_job(job):
        runs.append(job.name)
        return "ok"

    s2.on_job = on_job
    await s2.start()
    try:
        await _wait_for(lambda: len(runs) >= 1)
        assert "persistent-assignment" in runs
    finally:
        s2.stop()
