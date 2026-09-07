"""Tests for the extended cron tool management actions:
update, pause, resume, run_now, info — plus an end-to-end 'job actually fires'
check that drives the whole path through the CronTool public API.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from nanobot.agent.tools.cron import CronTool
from nanobot.cron.service import CronService


def _make_tool(tmp_path: Path) -> tuple[CronTool, CronService]:
    service = CronService(tmp_path / "cron" / "jobs.json")
    return CronTool(service), service


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# validate_params
# --------------------------------------------------------------------------- #
def test_validate_params_requires_job_id_for_all_manage_actions(tmp_path) -> None:
    tool, _ = _make_tool(tmp_path)
    for action in ("remove", "update", "pause", "resume", "run_now", "info"):
        errs = tool.validate_params({"action": action})
        assert any("job_id is required" in e for e in errs), action
    # list needs nothing extra
    assert tool.validate_params({"action": "list"}) == []


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #
def test_update_changes_message_and_schedule(tmp_path) -> None:
    tool, service = _make_tool(tmp_path)
    created = _run(tool.execute(action="add", message="original", every_seconds=3600))
    job_id = created.split("id: ")[1].rstrip(")")

    out = _run(
        tool.execute(
            action="update",
            job_id=job_id,
            name="renamed",
            message="updated body",
            every_seconds=7200,
        )
    )
    assert "Updated job 'renamed'" in out
    job = service.get_job(job_id)
    assert job is not None
    assert job.payload.message == "updated body"
    assert job.schedule.every_ms == 7_200_000


def test_update_without_changes_returns_error(tmp_path) -> None:
    tool, _ = _make_tool(tmp_path)
    created = _run(tool.execute(action="add", message="x", every_seconds=60))
    job_id = created.split("id: ")[1].rstrip(")")
    out = _run(tool.execute(action="update", job_id=job_id))
    assert "nothing to update" in out.lower()


def test_update_missing_job(tmp_path) -> None:
    tool, _ = _make_tool(tmp_path)
    out = _run(tool.execute(action="update", job_id="nope", name="z"))
    assert "not found" in out


# --------------------------------------------------------------------------- #
# pause / resume
# --------------------------------------------------------------------------- #
def test_pause_and_resume_toggle_enabled(tmp_path) -> None:
    tool, service = _make_tool(tmp_path)
    created = _run(tool.execute(action="add", message="tick", every_seconds=60))
    job_id = created.split("id: ")[1].rstrip(")")

    assert "Paused" in _run(tool.execute(action="pause", job_id=job_id))
    assert service.get_job(job_id).enabled is False
    assert service.get_job(job_id).state.next_run_at_ms is None

    assert "Resumed" in _run(tool.execute(action="resume", job_id=job_id))
    assert service.get_job(job_id).enabled is True
    assert service.get_job(job_id).state.next_run_at_ms is not None


# --------------------------------------------------------------------------- #
# run_now
# --------------------------------------------------------------------------- #
def test_run_now_executes_callback(tmp_path) -> None:
    fired: list[str] = []

    async def on_job(job):
        fired.append(job.name)
        return "done"

    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job)
    tool = CronTool(service)
    created = _run(tool.execute(action="add", message="hi", every_seconds=9999))
    job_id = created.split("id: ")[1].rstrip(")")

    out = _run(tool.execute(action="run_now", job_id=job_id))
    assert "Ran job" in out
    assert "status: ok" in out
    assert fired == [fired[0]] and len(fired) == 1


def test_run_now_missing_job(tmp_path) -> None:
    tool, _ = _make_tool(tmp_path)
    out = _run(tool.execute(action="run_now", job_id="missing"))
    assert "not found" in out


# --------------------------------------------------------------------------- #
# info
# --------------------------------------------------------------------------- #
def test_info_reports_details(tmp_path) -> None:
    tool, _ = _make_tool(tmp_path)
    created = _run(tool.execute(action="add", name="my-job", message="do it", every_seconds=120))
    job_id = created.split("id: ")[1].rstrip(")")
    out = _run(tool.execute(action="info", job_id=job_id))
    assert "my-job" in out
    assert "do it" in out
    assert "every 2m" in out
    assert "Enabled: True" in out


# --------------------------------------------------------------------------- #
# End-to-end: a scheduled one-shot job actually FIRES via the service timer
# --------------------------------------------------------------------------- #
def test_scheduled_job_actually_fires_end_to_end(tmp_path) -> None:
    """Schedule a job ~now through the tool API, start the service, and assert
    the callback runs and state is persisted — the real 'does it fire' proof."""
    fired: list[tuple[str, str]] = []

    async def on_job(job):
        fired.append((job.name, job.payload.message))
        return "ok"

    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job, max_sleep_ms=100)
    tool = CronTool(service)

    # One-shot 1 second from now (naive ISO uses default tz = UTC here).
    at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 1))
    created = _run(tool.execute(action="add", name="e2e", message="hello world", at=at))
    assert "Created job" in created

    async def scenario():
        await service.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not fired:
                await asyncio.sleep(0.05)
        finally:
            service.stop()

    asyncio.run(scenario())

    assert fired == [("e2e", "hello world")]
    # one-shot with delete_after_run removes itself from the store
    assert service.list_jobs(include_disabled=True) == [] or all(
        j.name != "e2e" or not j.enabled for j in service.list_jobs(include_disabled=True)
    )
