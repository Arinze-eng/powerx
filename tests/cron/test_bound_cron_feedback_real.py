"""REAL integration test: does a fired bound cron job deliver feedback to the user?

Reproduces the operator-reported symptom: "in two minutes check price" runs but
no reply reaches Telegram. Uses a real AgentLoop + real MessageBus, firing the
exact InboundMessage shape produced by run_bound_cron_job() and asserting that a
Telegram OutboundMessage actually lands on the outbound bus.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.cron.bound_runner import run_bound_cron_job
from nanobot.cron.session_turns import CRON_DEFER_UNTIL_IDLE_META, CRON_TRIGGER_META
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.providers.base import GenerationSettings, LLMResponse


def _make_loop(tmp_path: Path) -> tuple[AgentLoop, MessageBus]:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    with patch("nanobot.agent.loop.ContextBuilder") as cb, \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        # Real turn path calls skills.build_explicit_skill_runtime_context and
        # build_system_prompt; make them inert so the harness isolates delivery.
        cb.return_value.skills.build_explicit_skill_runtime_context.return_value = None
        cb.return_value.build_system_prompt.return_value = "sys"
        cb.return_value.build_messages.side_effect = lambda **kw: [
            {"role": "system", "content": "sys"},
            {"role": kw.get("current_role", "user"), "content": kw.get("current_message", "")},
        ]
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    return loop, bus


class _FakeCronRecorder:
    def __init__(self) -> None:
        self.records = []

    def write_run_record(self, run_id, record):
        self.records.append((run_id, dict(record)))


@pytest.mark.asyncio
async def test_bound_telegram_cron_delivers_feedback_to_bus(tmp_path):
    """The whole point: after a bound Telegram cron fires, a Telegram
    OutboundMessage with the agent's answer must be published to the bus."""
    loop, bus = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="BTC price is 65,000 USD", tool_calls=[], usage={})
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    # avoid consolidation network work
    if hasattr(loop, "consolidator"):
        loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=None)

    job = CronJob(
        id="price-check",
        name="Check price",
        schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000)),
        payload=CronPayload(
            message="Check the current BTC price and report it.",
            session_key="telegram:-1001",
            origin_channel="telegram",
            origin_chat_id="-1001",
        ),
        enabled=True,
    )

    recorder = _FakeCronRecorder()

    async def drain():
        out = []
        while True:
            try:
                out.append(bus.outbound.get_nowait())
            except Exception:
                break
        return out

    response = await run_bound_cron_job(job, agent=loop, cron=recorder)

    # The runner returned the agent text...
    assert response == "BTC price is 65,000 USD"

    # ...but did it ALSO publish a user-visible Telegram message to the bus?
    delivered = await drain()
    tg_msgs = [m for m in delivered if getattr(m, "channel", None) == "telegram"]
    print(f"\n=== delivered {len(delivered)} msgs; telegram-bound: {len(tg_msgs)} ===")
    for m in delivered:
        print(f"   channel={getattr(m,'channel',None)!r} chat={getattr(m,'chat_id',None)!r} content={str(getattr(m,'content',''))[:60]!r}")
    assert tg_msgs, "NO FEEDBACK DELIVERED TO TELEGRAM — this is the reported bug"
    assert any("65,000" in (m.content or "") for m in tg_msgs), "feedback content not delivered"


@pytest.mark.asyncio
async def test_general_unbound_cron_feedback(tmp_path):
    """A job created with NO routable origin (webui/general fallback from
    CronTool._add_job) runs via run_general_cron_job; the gateway then decides
    delivery via _general_cron_target()/_pick_heartbeat_target(). Reproduce the
    'does the task but no feedback' symptom for the general path."""
    loop, bus = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="BTC price is 42", tool_calls=[], usage={})
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    if hasattr(loop, "consolidator"):
        loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=None)

    # Simulate the fallback job CronTool creates when _request_route() is empty.
    job = CronJob(
        id="price-general",
        name="Check price",
        schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000)),
        payload=CronPayload(
            message="Check the current BTC price and report it.",
            session_key="general",
            origin_channel="webui",
            origin_chat_id="general",
        ),
        enabled=True,
    )
    from nanobot.cron.session_turns import is_bound_cron_job
    print("is_bound:", is_bound_cron_job(job))

    recorder = _FakeCronRecorder()
    response = await __import__(
        "nanobot.cron.bound_runner", fromlist=["run_general_cron_job"]
    ).run_general_cron_job(job, agent=loop, cron=recorder, channel="webui", chat_id="general")
    print("runner returned:", repr(response))

    out = []
    while True:
        try:
            out.append(bus.outbound.get_nowait())
        except Exception:
            break
    print(f"=== bus outbound after general run: {len(out)} ===")
    for m in out:
        print(f"   channel={getattr(m,'channel',None)!r} content={str(getattr(m,'content',''))[:50]!r}")
    # General runner itself does NOT publish to a user channel — the GATEWAY
    # must call _deliver_to_channel afterwards using a real target. If the only
    # target is webui/general, should_deliver=False => user never gets feedback.


@pytest.mark.asyncio
async def test_poll_watch_delivers_feedback_via_notifier(tmp_path):
    """A triggered market/notify watch must push user-facing feedback through the
    WatchManager notifier (the fix for 'poll runs but no feedback')."""
    from nanobot.trading.polling_engine import (
        PollResult,
        WatchManager,
        WatchSpec,
        format_poll_feedback,
    )

    manager = WatchManager()  # fresh instance, not the global singleton
    delivered: list[tuple[str, str, str]] = []

    async def fake_notifier(spec, result):
        content = format_poll_feedback(spec, result)
        if content:
            delivered.append((spec.channel, spec.chat_id or "", content))

    manager.set_notifier(fake_notifier)

    spec = WatchSpec(
        label="BTC drop",
        channel="telegram",
        chat_id="-1001",
        condition={"symbol": "BTCUSD", "target_price": 60000, "direction": "below", "action": "notify"},
        interval_seconds=0.25,
        max_polls=3,
    )

    async def tick(s, t):
        # Condition met on tick 1 -> notify action stops the loop.
        return {
            "price": 59000,
            "condition_met": True,
            "actions": ["BTCUSD crossed below 60000"],
            "stop": True,
            "summary": "BTCUSD fell to 59000, below your 60000 target.",
        }

    await manager.start(spec, tick)
    # Wait for the background watch task to finish.
    for _ in range(80):
        if not manager.running():
            break
        await asyncio.sleep(0.05)

    print(f"\n=== poll delivered {len(delivered)} feedback msg(s) ===")
    for ch, cid, content in delivered:
        print(f"   {ch}:{cid}\n{content}")
    assert delivered, "POLL FEEDBACK NOT DELIVERED — the reported bug"
    ch, cid, content = delivered[0]
    assert ch == "telegram" and cid == "-1001"
    assert "60000" in content or "59000" in content


