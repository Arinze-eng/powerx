"""The hard plan gate in the loop: no tools on the first request of a turn.

The gate is structural, not a check. The turn's FIRST provider request advertises
no tool schemas at all, so the model has nothing to call and the only thing it can
produce is the plan. These tests pin that -- and the consequences: the plan lands
in the transcript as the model's own message, tools return on the next request,
an approval-gated turn stops before doing any work, and an approved plan is not
planned again.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.plan_gate import PLAN_AWAITING_STOP_REASON
from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime

PLAN_TEXT = "1. Read the file.\n2. Summarise it."


def _runtime() -> LLMRuntime:
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


def _tools() -> MagicMock:
    """A registry whose one tool is never actually needed to prove the gate."""
    tool = SimpleNamespace(execute=AsyncMock(return_value="file contents"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = [
        {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    ]
    return tools


def _scripted_runner(script: list[LLMResponse], seen: list[object]) -> AgentRunner:
    """A runner that replays *script* and records the tools each request carried.

    ``seen`` is appended in request order, so the plan request is ``seen[0]`` on a
    gated turn. ``None`` means the request advertised no tool schemas.
    """
    runner = AgentRunner()
    state = {"n": 0}

    async def fake_request_model(spec, messages, hook, context, **kwargs):
        spec.llm_calls[0] += 1
        seen.append(kwargs.get("tools", "MISSING"))
        index = min(state["n"], len(script) - 1)
        state["n"] += 1
        return script[index]

    runner._request_model = fake_request_model  # type: ignore[assignment]
    return runner


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], finish_reason="stop")


def _call(name: str = "read_file") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="t1", name=name, arguments={"path": "a.txt"})],
        finish_reason="tool_calls",
    )


def _spec(**overrides: object) -> AgentRunSpec:
    kwargs: dict[str, object] = {
        "initial_messages": [{"role": "user", "content": "summarise a.txt"}],
        "tools": _tools(),
        "runtime": _runtime(),
        "max_iterations": 5,
        "max_tool_result_chars": 1000,
        "concurrent_tools": False,
        "deterministic_router_text": "summarise a.txt",
    }
    kwargs.update(overrides)
    return AgentRunSpec(**kwargs)  # type: ignore[arg-type]


# --- the enforcement --------------------------------------------------------


@pytest.mark.asyncio
async def test_first_request_of_a_gated_turn_advertises_no_tools() -> None:
    """The whole mechanism: with no schemas advertised there is nothing to call,
    so the reply cannot be anything but the plan. No judgement required."""
    seen: list[object] = []
    runner = _scripted_runner([_text(PLAN_TEXT), _text("Done.")], seen)

    result = await runner.run(_spec(plan_gate=True))

    assert seen[0] is None
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_tools_come_back_on_the_next_request() -> None:
    """The gate costs exactly one request. Everything after it runs as before."""
    seen: list[object] = []
    runner = _scripted_runner([_text(PLAN_TEXT), _call(), _text("Done.")], seen)

    await runner.run(_spec(plan_gate=True))

    assert seen[0] is None
    assert all(entry is not None for entry in seen[1:])


@pytest.mark.asyncio
async def test_only_the_first_request_of_the_turn_is_gated() -> None:
    """A gate that re-armed every iteration would make the agent unable to work
    at all."""
    seen: list[object] = []
    runner = _scripted_runner([_text(PLAN_TEXT), _call(), _text("Done.")], seen)

    await runner.run(_spec(plan_gate=True))

    assert seen.count(None) == 1


@pytest.mark.asyncio
async def test_the_plan_lands_in_the_transcript_as_the_models_own_message() -> None:
    """The model then follows what it wrote: the plan is an assistant message in
    the conversation it continues, not a side note."""
    seen: list[object] = []
    runner = _scripted_runner([_text(PLAN_TEXT), _text("Done.")], seen)

    result = await runner.run(_spec(plan_gate=True))

    plans = [
        message
        for message in result.messages
        if message.get("role") == "assistant" and PLAN_TEXT in str(message.get("content"))
    ]
    assert len(plans) == 1


@pytest.mark.asyncio
async def test_a_tool_call_returned_during_the_plan_phase_is_discarded() -> None:
    """No schemas were advertised, so any call is unexecutable. Letting it
    through would slip an action in ahead of the plan that is supposed to
    authorise it."""
    seen: list[object] = []
    runner = _scripted_runner(
        [
            LLMResponse(
                content=PLAN_TEXT,
                tool_calls=[
                    ToolCallRequest(id="t1", name="read_file", arguments={"path": "a.txt"})
                ],
                finish_reason="tool_calls",
            ),
            _text("Done."),
        ],
        seen,
    )

    result = await runner.run(_spec(plan_gate=True))

    assert result.tools_used == []


@pytest.mark.asyncio
async def test_an_ungated_turn_is_unchanged() -> None:
    """With the gate off the loop is exactly what it was: one request, tools
    advertised from the start."""
    seen: list[object] = []
    runner = _scripted_runner([_text("Done.")], seen)

    result = await runner.run(_spec(plan_gate=False))

    assert seen == [seen[0]]
    assert seen[0] is not None
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_a_blank_plan_reply_disarms_the_gate() -> None:
    """One unusable response must not strand a real task: the gate drops and the
    turn runs the way the loop ran before the gate existed."""
    seen: list[object] = []
    runner = _scripted_runner([_text("   "), _call(), _text("Done.")], seen)

    result = await runner.run(_spec(plan_gate=True))

    assert seen[0] is None
    assert seen[1] is not None
    assert result.stop_reason == "completed"


# --- the no-tools escape ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_no_tools_answer_ends_the_turn_in_one_request() -> None:
    """A plain question needs no tools, so a plan would authorise nothing.
    Forcing one would answer the user twice and bill them twice; the marker is
    how the model says so in the single reply it is given."""
    seen: list[object] = []
    runner = _scripted_runner([_text("Answer: 4"), _text("should never run")], seen)

    result = await runner.run(_spec(plan_gate=True, initial_messages=[{"role": "user", "content": "what is 2+2?"}]))

    assert result.stop_reason == "completed"
    assert result.final_content == "4"
    assert len(seen) == 1
    assert seen[0] is None


@pytest.mark.asyncio
async def test_the_marker_is_stripped_from_what_the_user_sees() -> None:
    seen: list[object] = []
    runner = _scripted_runner([_text("Answer: Paris is the capital.")], seen)

    result = await runner.run(_spec(plan_gate=True))

    assert result.final_content == "Paris is the capital."


@pytest.mark.asyncio
async def test_prose_without_the_marker_is_still_a_plan() -> None:
    """A task that needs tools must not be answered from memory by accident: a
    reply that does not claim to be an answer is treated as the plan."""
    seen: list[object] = []
    runner = _scripted_runner([_text("I would need to read the file first"), _text("Done.")], seen)

    result = await runner.run(_spec(plan_gate=True))

    assert len(seen) == 2
    assert seen[1] is not None
    assert result.stop_reason == "completed"


# --- approval ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approval_gated_turn_stops_before_doing_any_work() -> None:
    """Nothing may happen before the user has read the plan."""
    seen: list[object] = []
    runner = _scripted_runner([_text(PLAN_TEXT), _call(), _text("Done.")], seen)

    result = await runner.run(_spec(plan_gate=True, plan_approval_required=True))

    assert result.stop_reason == PLAN_AWAITING_STOP_REASON
    assert result.final_content == PLAN_TEXT
    assert result.tools_used == []
    # Only the plan request was ever made.
    assert seen == [None]


@pytest.mark.asyncio
async def test_the_waiting_reason_is_not_resumed_into_execution() -> None:
    """The stop reason must be a decision, not a stall -- otherwise the resume
    policy would start the work the user has not approved yet."""
    from nanobot.session import turn_continuation

    assert not turn_continuation.stall_is_resumable(PLAN_AWAITING_STOP_REASON)


@pytest.mark.asyncio
async def test_an_approved_plan_is_not_planned_again() -> None:
    """The user already answered, so re-planning would spend a request asking the
    same question. The gate is disarmed and the work starts immediately."""
    seen: list[object] = []
    runner = _scripted_runner([_text("Done.")], seen)

    result = await runner.run(
        _spec(plan_gate=True, approved_plan=PLAN_TEXT),
    )

    assert seen[0] is not None
    assert len(seen) == 1
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_an_approved_plan_missing_from_history_is_re_anchored() -> None:
    """The model cannot execute a plan it cannot see, so a caller that assembled
    its own messages still gets the plan put back in front of it."""
    seen: list[object] = []
    runner = _scripted_runner([_text("Done.")], seen)

    result = await runner.run(
        _spec(
            plan_gate=True,
            approved_plan=PLAN_TEXT,
            initial_messages=[{"role": "user", "content": "go"}],
        )
    )

    assert any(
        message.get("role") == "assistant" and PLAN_TEXT in str(message.get("content"))
        for message in result.messages
    )


@pytest.mark.asyncio
async def test_an_approved_plan_already_in_history_is_not_duplicated() -> None:
    """The plan is normally already the previous turn's assistant message, and
    repeating it would both bloat the context and confuse the transcript."""
    seen: list[object] = []
    runner = _scripted_runner([_text("Done.")], seen)

    result = await runner.run(
        _spec(
            plan_gate=True,
            approved_plan=PLAN_TEXT,
            initial_messages=[
                {"role": "user", "content": "summarise a.txt"},
                {"role": "assistant", "content": PLAN_TEXT},
                {"role": "user", "content": "go"},
            ],
        )
    )

    occurrences = [
        message
        for message in result.messages
        if message.get("role") == "assistant" and PLAN_TEXT in str(message.get("content"))
    ]
    assert len(occurrences) == 1
