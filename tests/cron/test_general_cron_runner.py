"""Tests for the general (unbound) cron execution path."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nanobot.cron.bound_runner import run_general_cron_job
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule


class _StubAgent:
    """Minimal GeneralCronAgent stand-in."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._cron_tool = None

    @property
    def tools(self):
        class _Reg:
            def __init__(self, tool):
                self._tool = tool

            def get(self, name):
                return self._tool if name == "cron" else None

        return _Reg(self._cron_tool)

    async def process_direct(
        self,
        content: str,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        sender_id: str | None = None,
        on_progress=None,
        extra_metadata: dict | None = None,
    ):
        self.calls.append(
            {
                "content": content,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "sender_id": sender_id,
            }
        )

        class _Resp:
            content = "general response"

        return _Resp()


def _make_job(name: str = "general-task", message: str = "do the thing") -> CronJob:
    return CronJob(
        id="general-1",
        name=name,
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(kind="agent_turn", message=message),
        enabled=True,
    )


def _make_cron(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "cron" / "jobs.json")


def test_run_general_cron_job_runs_in_dedicated_session(tmp_path) -> None:
    agent = _StubAgent()
    cron = _make_cron(tmp_path)
    job = _make_job()

    result = asyncio.run(
        run_general_cron_job(job, agent=agent, cron=cron, channel="webui", chat_id="general")
    )

    assert result == "general response"
    assert len(agent.calls) == 1
    call = agent.calls[0]
    # Runs in a dedicated per-job general session, not tied to a chat.
    assert call["session_key"] == "cron-general:general-1"
    assert call["channel"] == "webui"
    assert call["chat_id"] == "general"
    assert call["sender_id"] == "cron"
    # The task instruction is present in the rendered prompt.
    assert "do the thing" in call["content"]


def test_run_general_cron_job_writes_run_record(tmp_path) -> None:
    agent = _StubAgent()
    cron = _make_cron(tmp_path)
    job = _make_job()

    asyncio.run(
        run_general_cron_job(job, agent=agent, cron=cron, channel="webui", chat_id="general")
    )

    records = list((tmp_path / "cron" / "runs").glob("*.json"))
    assert records, "expected a run record to be written"
    ok_records = [r for r in records if "ok" in r.read_text(encoding="utf-8")]
    assert ok_records, "expected at least one 'ok' run record"


def test_run_general_cron_job_records_errors(tmp_path) -> None:
    class _FailingAgent(_StubAgent):
        async def process_direct(self, *args, **kwargs):
            raise RuntimeError("agent boom")

    agent = _FailingAgent()
    cron = _make_cron(tmp_path)
    job = _make_job()

    with pytest.raises(RuntimeError, match="agent boom"):
        asyncio.run(
            run_general_cron_job(
                job, agent=agent, cron=cron, channel="webui", chat_id="general"
            )
        )

    records = list((tmp_path / "cron" / "runs").glob("*.json"))
    assert records
    assert any(
        '"status": "error"' in r.read_text(encoding="utf-8") and "agent boom" in r.read_text(encoding="utf-8")
        for r in records
    )