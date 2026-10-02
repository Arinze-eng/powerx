"""The boot repair for cron jobs that were pinned to UTC (or to nothing)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.migrate_cron_timezone import migrate

_TARGET = "Africa/Lagos"


def _store(tmp_path: Path, jobs: list[dict]) -> Path:
    path = tmp_path / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")
    return path


def _job(job_id: str, *, kind: str = "cron", tz: str | None = "UTC", **schedule) -> dict:
    return {
        "id": job_id,
        "name": job_id,
        "enabled": True,
        "schedule": {"kind": kind, "atMs": None, "everyMs": None, "expr": None, "tz": tz, **schedule},
        "payload": {"kind": "agent_turn", "message": "hi"},
        "state": {"nextRunAtMs": 1_700_000_000_000, "lastRunAtMs": None},
    }


def test_a_utc_job_is_repointed_and_its_next_run_reset(tmp_path: Path) -> None:
    path = _store(tmp_path, [_job("a", expr="0 9 * * *")])

    assert migrate(path, _TARGET) == 1

    job = json.loads(path.read_text(encoding="utf-8"))["jobs"][0]
    assert job["schedule"]["tz"] == _TARGET
    # Recomputed by CronService.start() from the corrected zone; a stale value
    # would let the job fire at the old hour one last time.
    assert job["state"]["nextRunAtMs"] is None


def test_a_job_with_no_zone_is_repointed(tmp_path: Path) -> None:
    path = _store(tmp_path, [_job("a", tz=None, expr="0 9 * * *")])

    assert migrate(path, _TARGET) == 1

    job = json.loads(path.read_text(encoding="utf-8"))["jobs"][0]
    assert job["schedule"]["tz"] == _TARGET


def test_every_utc_spelling_is_repointed(tmp_path: Path) -> None:
    jobs = [_job(f"j{i}", tz=tz, expr="0 9 * * *") for i, tz in enumerate(
        ["UTC", "Etc/UTC", "GMT", "Zulu"]
    )]
    path = _store(tmp_path, jobs)

    assert migrate(path, _TARGET) == len(jobs)

    stored = json.loads(path.read_text(encoding="utf-8"))["jobs"]
    assert {job["schedule"]["tz"] for job in stored} == {_TARGET}


def test_a_job_with_a_real_zone_is_left_alone(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        [
            _job("kuwait", tz="Asia/Kuwait", expr="0 9 * * *"),
            _job("ny", tz="America/New_York", expr="0 9 * * *"),
        ],
    )

    assert migrate(path, _TARGET) == 0

    stored = json.loads(path.read_text(encoding="utf-8"))["jobs"]
    assert [job["schedule"]["tz"] for job in stored] == ["Asia/Kuwait", "America/New_York"]
    assert stored[0]["state"]["nextRunAtMs"] == 1_700_000_000_000


def test_interval_and_one_shot_jobs_are_untouched(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        [
            _job("interval", kind="every", tz=None, everyMs=3_600_000),
            _job("once", kind="at", tz=None, atMs=1_800_000_000_000),
        ],
    )

    assert migrate(path, _TARGET) == 0


def test_a_second_run_changes_nothing(tmp_path: Path) -> None:
    path = _store(tmp_path, [_job("a", expr="0 9 * * *")])

    assert migrate(path, _TARGET) == 1
    first = json.loads(path.read_text(encoding="utf-8"))
    assert migrate(path, _TARGET) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == first


def test_a_missing_store_is_not_an_error(tmp_path: Path) -> None:
    assert migrate(tmp_path / "cron" / "jobs.json", _TARGET) == 0


def test_a_corrupt_store_is_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert migrate(path, _TARGET) == 0
    assert path.read_text(encoding="utf-8") == "{not json"
