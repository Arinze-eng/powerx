"""Every model iteration must leave a memory trace in the logs.

A container over its cgroup memory limit is killed by the kernel: no exception,
no shutdown line, no reason in the logs — just a restart, which the user reads
as "the sandbox crashed". The runner therefore records usage around each model
request. These tests pin that the trace exists, appears once per iteration, and
carries the session so the offending turn can be identified after the fact.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from nanobot.agent.runner import AgentRunner
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.agent.runner_helpers import make_run_spec


def _capture() -> tuple[list[str], int]:
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
    return captured, sink_id


def _memory_tags(lines: list[str]) -> list[str]:
    return [
        line.split("tag=", 1)[1].split()[0]
        for line in lines
        if "MEMORY" in line and "tag=" in line
    ]


@pytest.mark.asyncio
async def test_runner_records_memory_around_every_model_iteration() -> None:
    """Two iterations (a tool call, then the answer) yield two start/end pairs."""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content="checking",
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.txt"})],
            ),
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="contents")
    runner = AgentRunner()

    captured, sink_id = _capture()
    try:
        await runner.run(
            make_run_spec(
                provider,
                model="test-model",
                initial_messages=[{"role": "user", "content": "go"}],
                tools=tools,
                max_iterations=4,
                max_tool_result_chars=1000,
                context_window_tokens=10000,
                session_key="websocket:memory-telemetry",
            )
        )
    finally:
        logger.remove(sink_id)

    tags = _memory_tags(captured)

    assert tags.count("model_iteration_start") == 2
    assert tags.count("model_iteration_end") == 2
    assert tags.index("model_iteration_start") < tags.index("model_iteration_end")


@pytest.mark.asyncio
async def test_memory_trace_names_the_session_and_iteration() -> None:
    """The line must say which turn and iteration it belongs to, and grade usage."""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[LLMResponse(content="done", tool_calls=[])])
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner()

    captured, sink_id = _capture()
    try:
        await runner.run(
            make_run_spec(
                provider,
                model="test-model",
                initial_messages=[{"role": "user", "content": "go"}],
                tools=tools,
                max_iterations=2,
                max_tool_result_chars=1000,
                context_window_tokens=10000,
                session_key="websocket:abc123",
            )
        )
    finally:
        logger.remove(sink_id)

    lines = [line for line in captured if "MEMORY" in line and "model_iteration_start" in line]

    assert lines, "no memory trace was emitted for the iteration"
    line = lines[0]
    assert "session=websocket:abc123" in line
    assert "iteration=0" in line
    # Grading and the numbers a limit is compared against are always present.
    assert "pressure=" in line
    assert "rss_mb=" in line
    assert "used_mb=" in line
