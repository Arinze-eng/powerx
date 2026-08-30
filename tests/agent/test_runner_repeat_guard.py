from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.base import ToolCallRequest
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
