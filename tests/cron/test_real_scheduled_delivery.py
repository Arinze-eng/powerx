"""REAL end-to-end cron firing + feedback delivery test.

Simulates 'in two minutes check price': schedule an `at` job a couple seconds
out, start the real CronService with on_job wired like the gateway wires it for
a BOUND telegram job (run_bound_cron_job -> submit_cron_turn through a real
AgentLoop), then assert a Telegram OutboundMessage lands on the bus.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.cron.bound_runner import run_bound_cron_job
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.providers.base import GenerationSettings, LLMResponse


def _make_loop(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    with patch("nanobot.agent.loop.ContextBuilder") as cb, \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as m:
        m.return_value.cancel_by_session = AsyncMock(return_value=0)
        cb.return_value.skills.build_explicit_skill_runtime_context.return_value = None
        cb.return_value.build_system_prompt.return_value = "sys"
        cb.return_value.build_messages.side_effect = lambda **kw: [
            {"role": "system", "content": "sys"},
            {"role": kw.get("current_role", "user"), "content": kw.get("current_message", "")},
        ]
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    return loop, bus


@pytest.mark.asyncio
async def test_scheduled_at_job_delivers_feedback(tmp_path):
    loop, bus = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="BTC is at 65,432 USD right now.", tool_calls=[], usage={})
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    if hasattr(loop, "consolidator"):
        loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=None)

    # Build a BOUND telegram `at` job due ~1s from now (like "in 2 min check price").
    svc = CronService(tmp_path / "cron" / "jobs.json")

    async def on_job(job: CronJob):
        # Mirror gateway's bound path exactly.
        return await run_bound_cron_job(job, agent=loop, cron=svc)

    svc.on_job = on_job

    at_ms = int((time.time() + 1.0) * 1000)
    svc.add_job(
        name="price-check",
        message="Check the current BTC price and report it.",
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        session_key="telegram:-777",
        origin_channel="telegram",
        origin_chat_id="-777",
    )
    await svc.start()
    try:
        # Wait up to ~15s for the timer to fire the job and deliver feedback.
        delivered = []
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                delivered.append(await asyncio.wait_for(bus.outbound.get(), timeout=0.5))
            except asyncio.TimeoutError:
                continue
            if any("65,432" in (m.content or "") for m in delivered[-1:] if getattr(m, "channel", None) == "telegram"):
                break
    finally:
        svc.stop()

    tg = [m for m in delivered if getattr(m, "channel", None) == "telegram"]
    print(f"\n=== {len(delivered)} outbound; telegram: {len(tg)} ===")
    for m in delivered:
        print(f"   {getattr(m,'channel',None)}:{getattr(m,'chat_id',None)} :: {str(getattr(m,'content',''))[:70]!r}")
    assert tg, "SCHEDULED CRON JOB DID NOT DELIVER FEEDBACK TO TELEGRAM"
    assert any("65,432" in (m.content or "") for m in tg), "feedback content missing"
