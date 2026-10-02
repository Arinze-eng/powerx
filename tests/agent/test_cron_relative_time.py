"""A reminder the user set "in about 5 minutes" must fire in five minutes.

Reported from the deployed bot: asked at 10:40 AM to check the XAUUSDT price in
five minutes, it created a one-shot job stamped **Mar 31, 2026, 5:45 AM** — a
different month, five months in the past — and the Automations screen showed
"No next run". The job could never fire.

The cause is structural, not a slip. ``cron`` takes a fully-computed ISO
datetime from the model, and the model has no current timestamp to compute it
with: the system-prompt prefix is deliberately kept free of volatile text so it
stays byte-identical for provider prompt caching (see
``tests/agent/test_context_prompt_cache.py``), and nothing else hands the turn a
clock. So the model invents a date.

These tests pin the two halves of the fix: a relative offset is resolved against
the server clock (the only version that cannot be wrong), and an absolute time
that has already passed is refused with the server's own clock quoted back
instead of quietly creating a dead job.
"""
from __future__ import annotations

import pytest

from nanobot.agent.tools.cron import _format_delta, parse_relative_at
from nanobot.cron.service import _now_ms


# ---- the relative parser -------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected_ms"),
    [
        ("+5m", 300_000),
        ("5m", 300_000),
        ("in 5 minutes", 300_000),
        ("in about 5 minutes", 300_000),
        ("about 5 minutes", 300_000),
        ("after 2 hours", 7_200_000),
        ("+2h", 7_200_000),
        ("90s", 90_000),
        ("1d", 86_400_000),
        ("+1w", 604_800_000),
        ("2.5m", 150_000),
        ("30 seconds", 30_000),
    ],
)
def test_relative_offsets_are_understood(value: str, expected_ms: int) -> None:
    assert parse_relative_at(value) == expected_ms


@pytest.mark.parametrize(
    "value",
    [
        "5",  # ambiguous between seconds and minutes - must not be guessed
        "2026-10-02T11:00:00",  # an ISO time is not a relative offset
        "",  # control case: an empty/falsey value is "no rel
        "every 5 minutes",
        "soon",
    ],
)
def test_non_relative_values_are_not_relative_offsets(value: str) -> None:
    assert parse_relative_at(value) is None


def test_a_relative_offset_is_always_in_the_future() -> None:
    """Whatever "now" is, a positive offset cannot land in the past."""
    assert parse_relative_at("+5m") > 0
    assert _now_ms() + parse_relative_at("+5m") > _now_ms()


def test_delta_formatting_is_human() -> None:
    assert _format_delta(4 * 60_000 + 30_000) == "4m 30s"
    assert _format_delta(45_000) == "45s"
    assert _format_delta(3_600_000) == "1h"
    assert _format_delta(90_000_000) == "1d 1h"


# ---- the tool itself -----------------------------------------------------


def _cron_tool(tmp_path, tz: str = "Africa/Lagos"):
    from nanobot.agent.tools.cron import CronTool
    from nanobot.cron.service import CronService

    service = CronService(tmp_path / "cron.json")
    tool = CronTool(cron_service=service, default_timezone=tz)
    return tool, service, None


async def test_in_five_minutes_lands_five_minutes_ahead(tmp_path) -> None:
    """The reported case, end to end through the tool."""
    tool, service, _ = _cron_tool(tmp_path)

    before = _now_ms()
    result = await tool.execute(
        action="add",
        message="Check the current price of XAUUSDT (gold) and report it to Arinze.",
        at="+5m",
    )
    after = _now_ms()

    assert not getattr(result, "is_error", False), str(result)
    text = str(result)
    job = service.list_jobs()[0]
    assert job.schedule.kind == "at"
    assert before + 300_000 <= job.schedule.at_ms <= after + 300_000

    # And the job can actually run, rather than sitting at "No next run".
    assert job.state.next_run_at_ms == job.schedule.at_ms
    assert "runs once at" in text


async def test_an_iso_time_in_the_past_is_refused_with_the_server_clock(tmp_path) -> None:
    """The exact shape of the report: a guessed date months behind is rejected."""
    tool, service, _ = _cron_tool(tmp_path)

    result = await tool.execute(
        action="add",
        message="Check the current price of XAUUSDT (gold)",
        at="2026-03-31T05:45:00",
    )

    text = str(result)
    assert "already passed" in text
    assert "never runs" in text
    # The server's own clock is quoted back so the retry cannot be a second guess.
    assert "Server time is now" in text
    assert "Africa/Lagos" in text
    # And nothing was stored.
    assert service.list_jobs() == []


async def test_a_future_iso_time_is_still_accepted(tmp_path) -> None:
    """The absolute path still works — it is only a past time that is refused."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tool, service, _ = _cron_tool(tmp_path)
    future = datetime.now(tz=ZoneInfo("Africa/Lagos")) + timedelta(hours=1)
    at = future.strftime("%Y-%m-%dT%H:%M:%S")

    result = await tool.execute(action="add", message="Standup", at=at)

    assert not getattr(result, "is_error", False), str(result)
    assert "runs once at" in str(result)
    assert service.list_jobs()[0].state.next_run_at_ms is not None


async def test_the_confirmation_states_when_it_will_fire(tmp_path) -> None:
    """The user reads the model's reply, so the model gets the resolved time."""
    tool, _, _ = _cron_tool(tmp_path)

    text = str(await tool.execute(action="add", message="Reminder", at="+30m"))

    assert "runs once at" in text
    assert "(in 30m)" in text


async def test_a_garbage_at_time_names_the_relative_form(tmp_path) -> None:
    tool, _, _ = _cron_tool(tmp_path)

    text = str(await tool.execute(action="add", message="Reminder", at="tomorrow morning"))

    assert "invalid ISO datetime format" in text
    assert "+5m" in text
    assert "Server time is now" in text
