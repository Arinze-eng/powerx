from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime


@pytest.mark.asyncio
async def test_third_identical_tool_call_is_blocked_but_different_action_runs() -> None:
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    runtime = LLMRuntime(
        provider=MagicMock(),
        model="test-model",
        generation=MagicMock(),
        context_window_tokens=10000,
    )
    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        runtime=runtime,
        max_iterations=4,
        max_tool_result_chars=1000,
        fail_on_tool_error=False,
    )
    runner = AgentRunner()
    state: dict[str, object] = {"fingerprint": None, "count": 0}
    repeated = ToolCallRequest(id="1", name="read_file", arguments={"path": "x.txt"})

    first = await runner._run_tool(spec, repeated, {}, {}, repeat_tool_state=state)
    second = await runner._run_tool(spec, repeated, {}, {}, repeat_tool_state=state)
    blocked = await runner._run_tool(spec, repeated, {}, {}, repeat_tool_state=state)
    different = await runner._run_tool(
        spec,
        ToolCallRequest(id="4", name="write_file", arguments={"path": "x.txt", "content": "y"}),
        {},
        {},
        repeat_tool_state=state,
    )

    assert first[1]["status"] == "ok"
    assert second[1]["status"] == "ok"
    assert blocked[1]["detail"] == "identical tool call blocked"
    assert blocked[2] is None
    assert different[1]["status"] == "ok"
    assert tool.execute.await_count == 3


def test_tool_fingerprint_is_stable_for_json_key_order() -> None:
    first = ToolCallRequest(id="1", name="exec", arguments={"a": 1, "b": 2})
    second = ToolCallRequest(id="2", name="exec", arguments={"b": 2, "a": 1})

    assert AgentRunner._tool_fingerprint(first) == AgentRunner._tool_fingerprint(second)


def test_fingerprint_ignores_an_explicitly_empty_optional() -> None:
    """A padded argument dict must not read as a different call.

    The guard exists to catch a model that re-issues the same request, and a
    model that re-issues it with ``"target": null`` spelled out is re-issuing
    the same request. Before normalization these fingerprinted differently and
    the guard never fired.
    """
    omitted = ToolCallRequest(
        id="1", name="human_browser", arguments={"url": "https://example.test/a"}
    )
    padded = ToolCallRequest(
        id="2",
        name="human_browser",
        arguments={"url": "https://example.test/a", "target": None, "text": ""},
    )

    assert AgentRunner._tool_fingerprint(omitted) == AgentRunner._tool_fingerprint(padded)


def test_fingerprint_still_separates_calls_that_differ_in_substance() -> None:
    """Normalization must not collapse genuinely different work."""
    first = ToolCallRequest(id="1", name="read_file", arguments={"path": "a.txt"})
    second = ToolCallRequest(id="2", name="read_file", arguments={"path": "b.txt"})

    assert AgentRunner._tool_fingerprint(first) != AgentRunner._tool_fingerprint(second)


@pytest.mark.asyncio
async def test_a_model_stuck_on_one_call_ends_the_turn_instead_of_looping() -> None:
    """The 2h turn: a repeated call must not cost a round trip per iteration.

    A refused call is a soft error, so nothing else ends the turn -- the model
    is handed "choose a different action" and re-issues the same call until
    max_iterations. MEASURED in production: 36 identical browser navigations
    back to back, turn ran to iteration 119. Here the same shape of model is
    given a 50-iteration budget and must be stopped after two refused
    iterations, having executed only the calls that were allowed through.
    """
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")

    repeated = ToolCallRequest(
        id="call_same", name="human_browser", arguments={"url": "https://example.test/a"}
    )
    calls = {"n": 0}

    async def chat_with_retry(*, messages, tools=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        # ``tools is None`` is the no-tools finalization request; an empty list
        # is a normal tool-bearing turn (the stub registry has no definitions).
        if tools is not None:
            return LLMResponse(content="working", tool_calls=[repeated], usage={})
        return LLMResponse(content="Here is what I have so far.", tool_calls=[], usage={})

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner()
    result = await runner.run(
        make_run_spec(
            provider,
            initial_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "do task"},
            ],
            tools=tools,
            model="test-model",
            max_iterations=50,
            max_tool_result_chars=1000,
        )
    )

    assert result.stop_reason == "repeat_stall"
    assert result.final_content == "Here is what I have so far."
    # Two allowed calls, then the guard refuses; the turn stops two iterations
    # later rather than running the remaining 46.
    assert tool.execute.await_count == 2
    assert calls["n"] <= 6


@pytest.mark.asyncio
async def test_the_stall_breaker_does_not_fire_on_ordinary_work() -> None:
    """A model that varies its calls and then answers must be left alone."""
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")

    calls = {"n": 0}

    async def chat_with_retry(*, messages, tools=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] <= 2:
            return LLMResponse(
                content="working",
                tool_calls=[
                    ToolCallRequest(
                        id=f"call_{calls['n']}",
                        name="read_file",
                        arguments={"path": f"file{calls['n']}.txt"},
                    )
                ],
                usage={},
            )
        return LLMResponse(content="All done.", tool_calls=[], usage={})

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner()
    result = await runner.run(
        make_run_spec(
            provider,
            initial_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "do task"},
            ],
            tools=tools,
            model="test-model",
            max_iterations=50,
            max_tool_result_chars=1000,
        )
    )

    assert result.stop_reason == "completed"
    assert result.final_content == "All done."
    assert tool.execute.await_count == 2


def test_alternation_is_recognised_as_a_loop() -> None:
    """A, B, A, B, A, B never repeats consecutively, so the count rule misses it.

    This is the second shape the industry's stuck detectors carry, and the one a
    consecutive-only guard is structurally blind to.
    """
    from nanobot.utils.runtime import stuck_pattern

    assert stuck_pattern(["a", "b", "a", "b", "a", "b"]) is not None
    # Two cycles is not enough -- a task may legitimately alternate two probes.
    assert stuck_pattern(["a", "b", "a", "b"]) is None
    # Ordinary, varied work is untouched.
    assert stuck_pattern(["a", "b", "c", "a", "b", "c"]) is None
    assert stuck_pattern(["a", "a", "b", "b", "a", "a"]) is None


@pytest.mark.asyncio
async def test_an_alternating_model_is_stopped_too() -> None:
    """A-B-A-B with two *different* calls must also end the turn."""
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")

    pair = [
        ToolCallRequest(id="call_a", name="read_file", arguments={"path": "a.txt"}),
        ToolCallRequest(id="call_b", name="read_file", arguments={"path": "b.txt"}),
    ]
    calls = {"n": 0}

    async def chat_with_retry(*, messages, tools=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        if tools is not None:
            return LLMResponse(
                content="working", tool_calls=[pair[calls["n"] % 2]], usage={}
            )
        return LLMResponse(content="Here is what I have.", tool_calls=[], usage={})

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner()
    result = await runner.run(
        make_run_spec(
            provider,
            initial_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "do task"},
            ],
            tools=tools,
            model="test-model",
            max_iterations=50,
            max_tool_result_chars=1000,
        )
    )

    assert result.stop_reason == "repeat_stall"
    # Five allowed calls complete the three cycles, the sixth is refused, the
    # seventh is refused again, and then one no-tools finalization closes it --
    # eight provider calls instead of the remaining 44.
    assert tool.execute.await_count == 5
    assert calls["n"] == 8
