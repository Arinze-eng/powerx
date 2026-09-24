"""The ReAct loop must not go silent on a tool that blocks.

``before_execute_tools`` publishes what the agent is about to do and
``after_iteration`` publishes what it did, so a tool that blocks in between used
to be announced once and then silent for as long as it took -- a step label that
could sit unchanged for minutes while the call was working. These tests pin the
liveness signal that closes that gap: elapsed time, emitted while the tool runs.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _one_tool_turn(provider: MagicMock) -> None:
    """Make the provider ask for exactly one tool call, then finish."""
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="checking",
                tool_calls=[ToolCallRequest(id="call_1", name="slow_probe", arguments={})],
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry


@pytest.mark.asyncio
async def test_a_tool_that_returns_promptly_emits_no_heartbeat(monkeypatch):
    """The quiet path: a normal call must not produce progress noise.

    This is the property that makes the heartbeat safe to add to every tool call.
    If it fired on a call that returned immediately it would be pure chatter, and
    it would have to be tool-allowlisted instead of being generic.
    """
    from nanobot.agent import runner as runner_module
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setattr(runner_module, "TOOL_HEARTBEAT_SECONDS", 0.05)

    provider = MagicMock(spec=LLMProvider)
    _one_tool_turn(provider)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="tool result")

    beats: list[float] = []

    class RecordingHook(AgentHook):
        async def on_tool_heartbeat(self, context, tool_call, elapsed_s):
            beats.append(elapsed_s)

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            hook=RecordingHook(),
        )
    )

    assert result.final_content == "done"
    assert beats == []


@pytest.mark.asyncio
async def test_a_blocked_tool_reports_elapsed_time_until_it_returns(monkeypatch):
    """The whole point: a blocked step keeps saying it is still going.

    The elapsed values must INCREASE. A hook that fired repeatedly with a
    constant string would be deduped by the UI and would look exactly as frozen
    as saying nothing, which is the bug being fixed.
    """
    from nanobot.agent import runner as runner_module
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setattr(runner_module, "TOOL_HEARTBEAT_SECONDS", 0.02)

    provider = MagicMock(spec=LLMProvider)
    _one_tool_turn(provider)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="tool result")

    async def slow_execute(*args, **kwargs):
        await asyncio.sleep(0.11)
        return "slow result"

    tools.execute = slow_execute

    beats: list[tuple[str, float]] = []

    class RecordingHook(AgentHook):
        async def on_tool_heartbeat(self, context, tool_call, elapsed_s):
            beats.append((tool_call.name, elapsed_s))

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            hook=RecordingHook(),
        )
    )

    # The tool still ran to completion and its result reached the turn.
    assert result.final_content == "done"
    assert len(beats) >= 2, f"expected repeated heartbeats, got {beats}"
    assert all(name == "slow_probe" for name, _ in beats)
    elapsed = [value for _, value in beats]
    assert elapsed == sorted(elapsed), f"elapsed must increase, got {elapsed}"
    assert elapsed[-1] > 0


@pytest.mark.asyncio
async def test_a_tool_that_raises_still_raises_through_the_heartbeat():
    """Observation must not swallow anything the tool throws.

    The heartbeat wraps the call in a task, and ``task.result()`` is where the
    exception is handed back. If it were swallowed here the runner's error
    classification would never see it and a failing tool would look like a
    successful one that returned nothing.
    """
    from nanobot.agent.runner import AgentRunner

    hook = AgentHook()

    async def boom():
        raise ValueError("tool exploded")

    with pytest.raises(ValueError, match="tool exploded"):
        await AgentRunner()._execute_tool_with_heartbeat(
            hook,
            AgentHookContext(iteration=0, messages=[]),
            ToolCallRequest(id="c1", name="boom", arguments={}),
            boom(),
        )


@pytest.mark.asyncio
async def test_cancelling_a_blocked_tool_cancels_the_tool_itself():
    """A cancelled turn must not leak the tool it was blocked on.

    Driving the call as a task is the part of this change that could regress
    cancellation, so it is pinned directly: cancelling the await must reach the
    tool, not merely stop waiting for it.
    """
    from nanobot.agent.runner import AgentRunner

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocks_forever():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    outer = asyncio.ensure_future(
        AgentRunner()._execute_tool_with_heartbeat(
            AgentHook(),
            AgentHookContext(iteration=0, messages=[]),
            ToolCallRequest(id="c1", name="blocker", arguments={}),
            blocks_forever(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    await asyncio.wait_for(cancelled.wait(), timeout=5)


@pytest.mark.asyncio
async def test_a_heartbeat_hook_that_raises_does_not_break_the_tool(monkeypatch):
    """Narration must never take down the work it narrates.

    The heartbeat fires while the turn is blocked, so an exception escaping it
    would turn a working tool call into a failed turn.
    """
    from nanobot.agent import runner as runner_module
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setattr(runner_module, "TOOL_HEARTBEAT_SECONDS", 0.01)

    class ExplodingHook(AgentHook):
        async def on_tool_heartbeat(self, context, tool_call, elapsed_s):
            raise RuntimeError("progress channel is down")

    async def slow():
        await asyncio.sleep(0.05)
        return "survived"

    value = await AgentRunner()._execute_tool_with_heartbeat(
        ExplodingHook(),
        AgentHookContext(iteration=0, messages=[]),
        ToolCallRequest(id="c1", name="slow", arguments={}),
        slow(),
    )

    assert value == "survived"


@pytest.mark.asyncio
async def test_a_composite_heartbeat_reaches_the_progress_hook(monkeypatch):
    """The hint a user sees must carry the elapsed time, not just repeat itself."""
    from nanobot.agent.hook import CompositeHook
    from nanobot.agent.progress_hook import AgentProgressHook

    calls: list[dict] = []

    async def on_progress(content, **kwargs):
        calls.append({"content": content, **kwargs})

    progress = AgentProgressHook(on_progress=on_progress)
    composite = CompositeHook([progress])

    await composite.on_tool_heartbeat(
        AgentHookContext(iteration=0, messages=[]),
        ToolCallRequest(id="c1", name="bash", arguments={"command": "sleep 200"}),
        130.0,
    )

    assert len(calls) == 1
    assert calls[0]["tool_hint"] is True
    assert "still running" in calls[0]["content"]
    assert "2m10s" in calls[0]["content"]


def test_elapsed_is_formatted_for_a_human():
    from nanobot.agent.progress_hook import _format_elapsed

    assert _format_elapsed(0) == "0s"
    assert _format_elapsed(45) == "45s"
    assert _format_elapsed(60) == "1m00s"
    assert _format_elapsed(130) == "2m10s"
    assert _format_elapsed(3725) == "1h02m"
    # Negative is impossible from perf_counter, but a clock skew must never
    # render as "-3s" in a message a user reads.
    assert _format_elapsed(-4) == "0s"
    assert time.perf_counter() > 0


# --------------------------------------------------------------------------
# The other silent window: the wait for the model's first token.
#
# Streaming hides a model request only once it has started talking. Nothing
# arrives before the first token at ANY setting, so on a queued or cold model
# the turn's opening step is silent for its whole duration -- the "processing"
# freeze, as opposed to the "checking" one the tool heartbeat covers.
# --------------------------------------------------------------------------


def _instant_provider(provider: MagicMock) -> None:
    """Answer on the first call, never ask for a tool."""

    async def chat_with_retry(**kwargs):
        return LLMResponse(content="answered", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry


@pytest.mark.asyncio
async def test_a_silent_model_request_reports_elapsed_time(monkeypatch):
    """A model that has said nothing yet must not look like a dead turn."""
    from nanobot.agent import runner as runner_module
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setattr(runner_module, "MODEL_HEARTBEAT_SECONDS", 0.02)

    provider = MagicMock(spec=LLMProvider)

    async def slow_chat(**kwargs):
        await asyncio.sleep(0.11)
        return LLMResponse(content="answered", tool_calls=[], usage={})

    provider.chat_with_retry = slow_chat
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="unused")

    beats: list[float] = []

    class RecordingHook(AgentHook):
        async def on_model_heartbeat(self, context, elapsed_s):
            beats.append(elapsed_s)

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=2,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            hook=RecordingHook(),
        )
    )

    assert result.final_content == "answered"
    assert len(beats) >= 2, f"expected repeated model heartbeats, got {beats}"
    assert beats == sorted(beats), f"elapsed must increase, got {beats}"


@pytest.mark.asyncio
async def test_a_model_that_answers_promptly_emits_no_heartbeat(monkeypatch):
    """The quiet path again -- this one runs on EVERY iteration, so noise here
    would be far more damaging than noise on a tool call."""
    from nanobot.agent import runner as runner_module
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setattr(runner_module, "MODEL_HEARTBEAT_SECONDS", 0.5)

    provider = MagicMock(spec=LLMProvider)
    _instant_provider(provider)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="unused")

    beats: list[float] = []

    class RecordingHook(AgentHook):
        async def on_model_heartbeat(self, context, elapsed_s):
            beats.append(elapsed_s)

    await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=2,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            hook=RecordingHook(),
        )
    )

    assert beats == []


@pytest.mark.asyncio
async def test_the_model_watcher_stops_once_output_has_started(monkeypatch):
    """Once the model is talking, the stream is the progress signal.

    A narrator that kept ticking through a streaming answer would fight the
    deltas for the same activity row, so the watcher must end at the first
    token rather than at the end of the request.
    """
    from nanobot.agent import runner as runner_module
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setattr(runner_module, "MODEL_HEARTBEAT_SECONDS", 0.01)

    spoken = asyncio.Event()
    beats: list[float] = []

    class RecordingHook(AgentHook):
        async def on_model_heartbeat(self, context, elapsed_s):
            beats.append(elapsed_s)

    watcher = asyncio.ensure_future(
        AgentRunner()._watch_model_wait(
            RecordingHook(),
            AgentHookContext(iteration=0, messages=[]),
            time.perf_counter(),
            spoken.is_set,
        )
    )

    await asyncio.sleep(0.03)
    assert beats, "watcher must tick while the model is still silent"
    spoken.set()
    await asyncio.wait_for(watcher, timeout=5)

    settled = len(beats)
    await asyncio.sleep(0.05)
    assert len(beats) == settled, "watcher kept narrating after output began"


@pytest.mark.asyncio
async def test_the_model_watcher_is_stopped_when_the_request_ends():
    """No narrator may outlive the request it describes.

    The watcher sleeps on a timer beside the request, so a turn that finishes
    must tear it down explicitly or it wakes up and talks about a turn that is
    already over.
    """
    from nanobot.agent.runner import AgentRunner

    task = asyncio.ensure_future(
        AgentRunner()._watch_model_wait(
            AgentHook(),
            AgentHookContext(iteration=0, messages=[]),
            time.perf_counter(),
            lambda: True,
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_model_hook_that_raises_does_not_break_the_request(monkeypatch):
    """Same guarantee as the tool heartbeat, on the hotter path: this one runs
    concurrently with the model request itself."""
    from nanobot.agent import runner as runner_module
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setattr(runner_module, "MODEL_HEARTBEAT_SECONDS", 0.01)

    class ExplodingHook(AgentHook):
        async def on_model_heartbeat(self, context, elapsed_s):
            raise RuntimeError("progress channel is down")

    watcher = asyncio.ensure_future(
        AgentRunner()._watch_model_wait(
            ExplodingHook(),
            AgentHookContext(iteration=0, messages=[]),
            time.perf_counter(),
            lambda: False,
        )
    )
    # Several intervals of a raising hook must leave the task alive and sane.
    await asyncio.sleep(0.05)
    assert not watcher.done()
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher


@pytest.mark.asyncio
async def test_a_model_wait_reaches_the_progress_hook_as_thinking():
    """The wait belongs on the thinking lane, not the tool lane.

    Publishing it as a tool hint would tell the user a tool was running when
    none was. The non-tool-hint progress lane is what every surface already
    renders as "processing", so it needs no new event and no client change.
    """
    from nanobot.agent.hook import CompositeHook
    from nanobot.agent.progress_hook import AgentProgressHook

    calls: list[dict] = []

    async def on_progress(content, **kwargs):
        calls.append({"content": content, **kwargs})

    composite = CompositeHook([AgentProgressHook(on_progress=on_progress)])
    await composite.on_model_heartbeat(
        AgentHookContext(iteration=0, messages=[]),
        130.0,
    )

    assert len(calls) == 1
    assert not calls[0].get("tool_hint"), "the model wait is not a tool call"
    assert "waiting for the model" in calls[0]["content"]
    assert "2m10s" in calls[0]["content"]
