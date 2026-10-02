"""Cron expressions must be read in the deployment's zone, not the container's.

Regression: a cron expression with no explicit timezone — or one stamped with
the tool's old hard-coded ``"UTC"`` default — was scheduled in whatever zone the
container happened to run in. Northflank containers are UTC while the owner is
at UTC+1, so every such schedule fired exactly one hour late.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nanobot.agent.tools.cron import CronTool
from nanobot.config.timezone import DEFAULT_TIMEZONE
from nanobot.cron.service import CronService, _compute_next_run
from nanobot.cron.types import CronSchedule

_WAT = ZoneInfo(DEFAULT_TIMEZONE)


def _bound_chat(chat_id: str = "c1") -> dict[str, str]:
    return {
        "session_key": f"websocket:{chat_id}",
        "origin_channel": "websocket",
        "origin_chat_id": chat_id,
    }


def test_a_zoneless_expression_uses_the_deployment_zone() -> None:
    """09:00 with no timezone is 09:00 in the deployment zone, not 09:00 UTC."""
    now_ms = int(datetime(2026, 6, 1, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)

    with_default = _compute_next_run(
        CronSchedule(kind="cron", expr="0 9 * * *"), now_ms, default_tz=DEFAULT_TIMEZONE
    )
    explicit = _compute_next_run(
        CronSchedule(kind="cron", expr="0 9 * * *", tz=DEFAULT_TIMEZONE), now_ms
    )

    assert with_default == explicit
    assert datetime.fromtimestamp(with_default / 1000, tz=_WAT).hour == 9
    # 09:00 WAT is 08:00 UTC — one hour away from what the container zone produced.
    assert datetime.fromtimestamp(with_default / 1000, tz=ZoneInfo("UTC")).hour == 8


def test_the_container_zone_is_not_the_fallback() -> None:
    now_ms = int(datetime(2026, 6, 1, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    schedule = CronSchedule(kind="cron", expr="0 9 * * *")

    in_default = _compute_next_run(schedule, now_ms, default_tz=DEFAULT_TIMEZONE)
    in_utc = _compute_next_run(schedule, now_ms, default_tz="UTC")

    assert in_utc - in_default == 3_600_000


def test_a_job_stored_without_a_zone_is_scheduled_in_the_service_zone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json", default_timezone=DEFAULT_TIMEZONE)
    job = service.add_job(
        name="zoneless",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *"),
        message="hello",
        **_bound_chat(),
    )

    assert datetime.fromtimestamp(job.state.next_run_at_ms / 1000, tz=_WAT).hour == 9


def test_a_service_without_a_configured_zone_uses_the_deployment_zone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    assert service.default_timezone == DEFAULT_TIMEZONE
    assert service.default_timezone != "UTC"


def test_a_configured_zone_is_kept(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json", default_timezone="Asia/Shanghai")

    assert service.default_timezone == "Asia/Shanghai"


def test_a_blank_zone_falls_back_to_the_deployment_zone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json", default_timezone="   ")

    assert service.default_timezone == DEFAULT_TIMEZONE


def test_the_cron_tool_no_longer_defaults_to_utc(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    assert CronTool(service)._default_timezone == DEFAULT_TIMEZONE
    assert "(UTC)" not in CronTool(service).description


def test_a_naive_iso_time_lands_in_the_deployment_zone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json", default_timezone=DEFAULT_TIMEZONE)
    tool = CronTool(service, default_timezone=DEFAULT_TIMEZONE)
    soon = (datetime.now(tz=_WAT) + timedelta(days=1)).strftime("%Y-%m-%dT09:00:00")

    schedule, _delete_after = tool._build_schedule(
        every_seconds=None, cron_expr=None, tz=None, at=soon
    )

    assert isinstance(schedule, CronSchedule)
    assert datetime.fromtimestamp(schedule.at_ms / 1000, tz=_WAT).hour == 9
