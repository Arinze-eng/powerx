"""The wall-clock turn budget: a turn must not run for hours inside max_iterations.

``max_iterations`` bounds a turn by COUNT. A count is the wrong unit, because an
iteration's cost is set by the tool, not by the loop: 200 read-only lookups is
seconds, 200 browser sessions or sandbox builds is hours. MEASURED in production:
one turn ran 119 consecutive browser calls (400 s of pure round-trip time) and the
reported video-edit turn ran past two hours -- both comfortably inside a
200-iteration budget, so the count never fired.

These tests pin the time bound, the graceful exit it produces, and the fact that
it is expressed in the loop rather than in a provider client (so it applies to
whatever model the admin configured).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent import runner as runner_module
from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.runtime import TURN_BUDGET_SECONDS, turn_budget_seconds


def _runtime() -> LLMRuntime:
    """A runtime whose provider refuses the finalization call.

    ``_try_finalize_after_max_iterations`` makes a real no-tools request; the
    budget exit then falls back to the template message. Answering with
    ``finish_reason="error"`` makes the finalizer decline, which is what the
    fallback path is for -- and it keeps these tests to a single stubbed seam.
    """
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="", tool_calls=[], finish_reason="error")
    )
    return LLMRuntime(
        provider=provider,
        model="test-model",
        generation=MagicMock(),
        context_window_tokens=10000,
    )


def _looping_runner() -> AgentRunner:
    """A runner whose model keeps working, productively, forever.

    Every call is UNIQUE on purpose. The point of the budget is the turn that is
    not stalled and not repeating -- it is doing real work, and it is simply slow
    (one browser session, one sandbox build, one video render per step). The
    repeat guard must not be what ends these turns; only the clock may.
    """
    runner = AgentRunner()
    counter = {"n": 0}

    async def fake_request_model(spec, messages, hook, *args, **kwargs):
        spec.llm_calls[0] += 1
        counter["n"] += 1
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id=f"t{counter['n']}",
                    name="read_file",
                    arguments={"path": f"file{counter['n']}.txt"},
                )
            ],
            finish_reason="tool_calls",
        )

    runner._request_model = fake_request_model  # type: ignore[assignment]
    return runner


# --- the resolver -----------------------------------------------------------


def test_turn_budget_default_is_thirty_minutes() -> None:
    """The default must be a real ceiling: long enough for real work, short
    enough that '2h+' is impossible."""
    assert TURN_BUDGET_SECONDS == 1800.0


def test_turn_budget_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOBOT_TURN_BUDGET_S", "120")
    assert turn_budget_seconds() == 120.0


def test_turn_budget_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit 0 is the documented opt-out for an operator who wants no cap."""
    monkeypatch.setenv("NANOBOT_TURN_BUDGET_S", "0")
    assert turn_budget_seconds() == 0.0


def test_turn_budget_garbage_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not silently remove the ceiling."""
    monkeypatch.setenv("NANOBOT_TURN_BUDGET_S", "twenty minutes")
    assert turn_budget_seconds() == TURN_BUDGET_SECONDS


def test_turn_budget_blank_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOBOT_TURN_BUDGET_S", "   ")
    assert turn_budget_seconds() == TURN_BUDGET_SECONDS


# --- the loop ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_ends_when_the_wall_clock_budget_is_spent() -> None:
    """A turn that keeps working must still stop when its time is up.

    The model here never stalls and never repeats -- it is a perfectly productive
    turn that simply takes too long, which is exactly the case the iteration
    ceiling cannot catch. The budget is what ends it.
    """
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []

    runner = _looping_runner()
    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        runtime=_runtime(),
        max_iterations=200,
        max_tool_result_chars=1000,
        concurrent_tools=False,
        turn_budget_s=0.05,
        turn_budget_message="Stopped after {minutes} minutes.",
    )

    result = await runner.run(spec)

    assert result.stop_reason == "turn_budget_exceeded"
    # Far fewer than the iteration ceiling: the time bound is what stopped it.
    assert spec.llm_calls[0] < 200


@pytest.mark.asyncio
async def test_budget_exit_reports_the_work_done_not_a_dead_turn() -> None:
    """The user gets an answer, and it is the budget message -- not the
    iteration-ceiling text, which blames the model for splitting the task."""
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []

    runner = _looping_runner()
    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        runtime=_runtime(),
        max_iterations=200,
        max_tool_result_chars=1000,
        concurrent_tools=False,
        turn_budget_s=0.05,
        turn_budget_message="Stopped after {minutes} minutes; ask me to continue.",
    )

    result = await runner.run(spec)

    assert result.final_content
    assert "minutes" in result.final_content
    assert "iteration" not in result.final_content.lower()


@pytest.mark.asyncio
async def test_default_budget_message_is_rendered_from_template() -> None:
    """With no override the shipped template is used, so the wording is not
    hardcoded in the runner."""
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []

    runner = _looping_runner()
    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        runtime=_runtime(),
        max_iterations=200,
        max_tool_result_chars=1000,
        concurrent_tools=False,
        turn_budget_s=0.05,
    )

    result = await runner.run(spec)

    assert result.stop_reason == "turn_budget_exceeded"
    assert "continue" in result.final_content.lower()


@pytest.mark.asyncio
async def test_zero_budget_runs_to_the_iteration_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """turn_budget_s=0 is the opt-out: the loop is then bounded only by
    max_iterations, exactly as before this feature existed."""
    monkeypatch.setenv("NANOBOT_TURN_BUDGET_S", "0")
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []

    runner = _looping_runner()
    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        runtime=_runtime(),
        max_iterations=3,
        max_tool_result_chars=1000,
        concurrent_tools=False,
        turn_budget_s=0,
    )

    result = await runner.run(spec)

    assert result.stop_reason != "turn_budget_exceeded"
    assert spec.llm_calls[0] >= 3


@pytest.mark.asyncio
async def test_budget_is_not_auto_resumed() -> None:
    """The stop reason must not be treated as a transient stall.

    A resumable reason would let the turn restart and run another full budget,
    which turns a 30-minute cap back into an unbounded run -- the exact failure
    this exists to prevent.
    """
    from nanobot.session.turn_continuation import stall_is_resumable

    assert stall_is_resumable("turn_budget_exceeded") is False


@pytest.mark.asyncio
async def test_budget_check_happens_before_the_next_model_call() -> None:
    """The check is at the TOP of the iteration, so a turn that is already out
    of time does not pay for one more round trip."""
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []

    calls = {"n": 0}

    runner = AgentRunner()

    async def fake_request_model(spec, messages, hook, *args, **kwargs):
        calls["n"] += 1
        spec.llm_calls[0] += 1
        # Burn the whole budget during the FIRST request, so the very next
        # iteration is already out of time.
        await asyncio.sleep(0.06)
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="t", name="read_file", arguments={"path": "a"})],
            finish_reason="tool_calls",
        )

    runner._request_model = fake_request_model  # type: ignore[assignment]
    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        runtime=_runtime(),
        max_iterations=200,
        max_tool_result_chars=1000,
        concurrent_tools=False,
        turn_budget_s=0.05,
        turn_budget_message="Stopped after {minutes} minutes.",
    )

    result = await runner.run(spec)

    assert result.stop_reason == "turn_budget_exceeded"
    # Exactly one provider call: the second iteration was refused before it paid
    # for another round trip.
    assert calls["n"] == 1
