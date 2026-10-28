"""Pacing between tool steps is opt-in and must never block or break a step.

Pinned here: off by default (no status, no pause), on with POWERX_PACE_STATUS=1
(status first, then a real pause, then the tool), the status wording, a failing
status hook not stopping the step, and cancellation during the pause propagating.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent import pace
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


@pytest.fixture(autouse=True)
def _clean_pace_env(monkeypatch):
    monkeypatch.delenv(pace.PACE_ENV, raising=False)
    monkeypatch.delenv(pace.PACE_SECONDS_ENV, raising=False)


def _one_tool_turn(provider: MagicMock) -> None:
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="reading",
                tool_calls=[
                    ToolCallRequest(id="call_1", name="read_file", arguments={"path": "src/app.py"})
                ],
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry


def _tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="file body")
    return tools


# --- env parsing -----------------------------------------------------------


def test_pacing_is_off_when_unset():
    assert pace.pace_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_pacing_turns_on_for_truthy_values(monkeypatch, value):
    monkeypatch.setenv(pace.PACE_ENV, value)
    assert pace.pace_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "", "off", "no"])
def test_pacing_stays_off_for_anything_else(monkeypatch, value):
    monkeypatch.setenv(pace.PACE_ENV, value)
    assert pace.pace_enabled() is False


def test_pause_defaults_and_is_clamped(monkeypatch):
    assert pace.pace_seconds() == pace.DEFAULT_PACE_SECONDS

    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "0.25")
    assert pace.pace_seconds() == 0.25

    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "9999")
    assert pace.pace_seconds() == pace.MAX_PACE_SECONDS

    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "-3")
    assert pace.pace_seconds() == 0.0

    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "nan")
    assert pace.pace_seconds() == pace.DEFAULT_PACE_SECONDS

    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "soon")
    assert pace.pace_seconds() == pace.DEFAULT_PACE_SECONDS


# --- runner behaviour --------------------------------------------------------


@pytest.mark.asyncio
async def test_off_by_default_no_status_and_no_pause(monkeypatch):
    from nanobot.agent.runner import AgentRunner

    provider = MagicMock(spec=LLMProvider)
    _one_tool_turn(provider)
    events: list[str] = []

    class Recording(AgentHook):
        async def on_pace_pause(self, context, tool_call, seconds):
            events.append("pace")

        async def before_execute_tool(self, context, tool_call, tool, params):
            events.append("execute")

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[],
            tools=_tools(),
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            hook=Recording(),
        )
    )

    assert result.final_content == "done"
    assert events == ["execute"]


@pytest.mark.asyncio
async def test_on_status_comes_before_a_real_pause_then_the_tool(monkeypatch):
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setenv(pace.PACE_ENV, "1")
    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "0.2")

    provider = MagicMock(spec=LLMProvider)
    _one_tool_turn(provider)
    events: list[tuple[str, float]] = []
    seen_seconds: list[float] = []

    class Recording(AgentHook):
        async def on_pace_pause(self, context, tool_call, seconds):
            seen_seconds.append(seconds)
            events.append(("pace", time.perf_counter()))

        async def before_execute_tool(self, context, tool_call, tool, params):
            events.append(("execute", time.perf_counter()))

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[],
            tools=_tools(),
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            hook=Recording(),
        )
    )

    assert result.final_content == "done"
    assert seen_seconds == [0.2]
    assert [name for name, _ in events] == ["pace", "execute"]
    gap = events[1][1] - events[0][1]
    assert gap >= 0.15, f"the pause was not applied (gap {gap:.3f}s)"


@pytest.mark.asyncio
async def test_a_failing_status_hook_does_not_block_the_step(monkeypatch):
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setenv(pace.PACE_ENV, "1")
    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "0")

    provider = MagicMock(spec=LLMProvider)
    _one_tool_turn(provider)
    tools = _tools()

    class Broken(AgentHook):
        async def on_pace_pause(self, context, tool_call, seconds):
            raise RuntimeError("status line broke")

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            hook=Broken(),
        )
    )

    assert result.final_content == "done"
    tools.execute.assert_awaited()


@pytest.mark.asyncio
async def test_cancelling_during_the_pause_propagates(monkeypatch):
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "5")
    executed = []

    class Recording(AgentHook):
        async def before_execute_tool(self, context, tool_call, tool, params):
            executed.append(tool_call.name)

    tool_call = ToolCallRequest(id="c1", name="read_file", arguments={"path": "a"})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            AgentRunner._pace_before_tool(
                Recording(), AgentHookContext(iteration=0, messages=[]), tool_call
            ),
            timeout=0.05,
        )
    assert executed == []


# --- progress wording ------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_hook_shows_understanding_for_the_step():
    from nanobot.agent.progress_hook import AgentProgressHook

    calls: list[dict] = []

    async def on_progress(content, **kwargs):
        calls.append({"content": content, **kwargs})

    hook = AgentProgressHook(on_progress=on_progress)
    await hook.on_pace_pause(
        AgentHookContext(iteration=0, messages=[]),
        ToolCallRequest(id="c1", name="read_file", arguments={"path": "src/app.py"}),
        1.5,
    )

    assert len(calls) == 1
    assert calls[0]["tool_hint"] is True
    assert calls[0]["content"].startswith("understanding: ")
    assert "src/app.py" in calls[0]["content"]


@pytest.mark.asyncio
async def test_progress_hook_without_a_sink_is_silent():
    from nanobot.agent.progress_hook import AgentProgressHook

    hook = AgentProgressHook(on_progress=None)
    await hook.on_pace_pause(
        AgentHookContext(iteration=0, messages=[]),
        ToolCallRequest(id="c1", name="read_file", arguments={"path": "a"}),
        1.5,
    )


@pytest.mark.asyncio
async def test_status_reaches_the_user_through_the_real_progress_hook(monkeypatch):
    """End to end: the runner's pause shows up on the progress channel users see."""
    from nanobot.agent.progress_hook import AgentProgressHook
    from nanobot.agent.runner import AgentRunner

    monkeypatch.setenv(pace.PACE_ENV, "1")
    monkeypatch.setenv(pace.PACE_SECONDS_ENV, "0")

    provider = MagicMock(spec=LLMProvider)
    _one_tool_turn(provider)
    progress: list[str] = []

    async def on_progress(content, **kwargs):
        progress.append(content)

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[],
            tools=_tools(),
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            hook=AgentProgressHook(on_progress=on_progress),
        )
    )

    assert result.final_content == "done"
    understanding = [text for text in progress if text.startswith("understanding: ")]
    assert understanding, progress
    assert "src/app.py" in understanding[0]
