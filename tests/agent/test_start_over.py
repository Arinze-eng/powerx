"""Start over" must abandon the running turn and run the request fresh.

A message queued for mid-turn injection is only read between model calls, so a
looping turn never sees it. These tests pin the three parts of the fix: the
phrase detector, the abandon step (drops unfinished work, keeps finished turns),
and the end-to-end path through the inbound loop.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import Session
from nanobot.utils.runtime import is_restart_request


def _make_loop(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        mock_sub_mgr.return_value.close = AsyncMock()
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    return loop


@pytest.mark.parametrize(
    "text",
    [
        "start over",
        "Start over.",
        "please start over",
        "start the whole process all over again",
        "start again",
        "begin again please",
        "restart",
        "start from scratch",
        "please start from the beginning",
        "redo everything from the start",
        "all over again",
    ],
)
def test_short_start_over_phrases_are_restart_requests(text: str) -> None:
    assert is_restart_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "restart the server",
        "the server failed, please fix it and try again",
        "start the server again",
        "go over the code once more",
        "",
        # Long instruction that happens to contain the words: not a restart.
        "I need a full report on the sales numbers for last quarter, split by region, "
        "and then start over the table layout with the new column order please",
    ],
)
def test_other_messages_are_not_restart_requests(text: str) -> None:
    assert not is_restart_request(text)


@pytest.mark.asyncio
async def test_abandon_stops_task_drops_unfinished_work_and_keeps_history(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    key = "cli:c"
    session = Session(key=key)
    session.add_message("user", "first request")
    session.add_message("assistant", "first answer")
    session.add_message("user", "build the report")
    session.metadata[loop._PENDING_USER_TURN_KEY] = True
    session.metadata[loop._RUNTIME_CHECKPOINT_KEY] = {"completed_tool_results": ["looping"]}
    loop.sessions.get_or_create.return_value = session

    started = asyncio.Event()

    async def looping_turn() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(looping_turn())
    await started.wait()
    loop._active_tasks[key] = {task}
    queued = asyncio.Queue(maxsize=5)
    queued.put_nowait(InboundMessage(channel="cli", sender_id="u", chat_id="c", content="x"))
    loop._pending_queues[key] = queued

    cancelled = await loop._abandon_active_turn(key)

    assert task.cancelled()
    assert cancelled >= 2  # the task and the queued message
    assert key not in loop._pending_queues
    assert key not in loop._discarding_sessions
    assert loop._RUNTIME_CHECKPOINT_KEY not in session.metadata
    assert loop._PENDING_USER_TURN_KEY not in session.metadata
    contents = [m["content"] for m in session.messages]
    # Earlier completed turns are kept; the abandoned request gets a closing note.
    assert contents[:2] == ["first request", "first answer"]
    assert contents[-2] == "build the report"
    assert "start over" in contents[-1]


@pytest.mark.asyncio
async def test_start_over_message_restarts_a_looping_turn(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    key = "cli:c"
    loop.sessions.get_or_create.return_value = Session(key=key)
    # Prompt assembly needs a real workspace; this test is about turn routing.
    loop.context.skills.build_explicit_skill_runtime_context.return_value = None
    loop._build_initial_messages = lambda ctx: [
        {"role": "system", "content": "system"},
        {"role": "user", "content": ctx.msg.content},
    ]

    first_call_started = asyncio.Event()
    calls: list[int] = []

    async def chat_with_retry(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            first_call_started.set()
            await asyncio.Event().wait()  # the looping turn never finishes on its own
        return LLMResponse(content="fresh start", tool_calls=[], usage={})

    loop.provider.chat_with_retry = chat_with_retry
    runner = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(
            InboundMessage(channel="cli", sender_id="u", chat_id="c", content="build the report")
        )
        await asyncio.wait_for(first_call_started.wait(), timeout=5)
        await loop.bus.publish_inbound(
            InboundMessage(channel="cli", sender_id="u", chat_id="c", content="start over")
        )

        deadline = asyncio.get_running_loop().time() + 5
        while len(calls) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        loop.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    # The start-over message ran as its own turn instead of being queued into
    # the looping one, so the model was called a second time.
    assert len(calls) == 2
