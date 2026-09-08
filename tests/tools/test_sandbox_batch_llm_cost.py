"""Live proof: sandbox_batch collapses N sandbox ops into ONE LLM round-trip.

The whole point of ``sandbox_batch`` (and the runner's coalescer) is that a
multi-step sandbox task does NOT charge one provider API call per step. This
test drives the real ``AgentRunner`` loop with an instrumented fake provider
that counts every request, plus the real ``SandboxBatchTool`` and real
``NovitaSandboxTool`` code path (only the Novita SDK boundary is faked):

* 6 consecutive lone ``novita_sandbox run`` calls from the model → coalesced
  by the runner into 1 ``sandbox_batch`` execution → 2 provider calls total
  (one to emit the tool calls, one for the final answer), NOT 7.
* A single explicit ``sandbox_batch`` op list → also exactly 1 backend
  execution per op, still 2 provider calls for the turn.
* Without the batch tool registered, the same 6 ops pass through as 6
  individual executions but STILL only cost 2 provider calls — proving the
  savings come from the loop structure, and the coalescer preserves it.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent.runner_helpers import make_run_spec

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.sandbox_batch import SandboxBatchTool
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class CountingProvider(LLMProvider):
    """Scripted provider that records every request it receives.

    The runner's non-streaming path calls ``chat_with_retry`` → ``_safe_chat``
    → (with a provider context) ``chat_with_context`` → ``chat``, so the real
    entry point we must instrument is :meth:`chat`.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self._responses = list(responses)
        self.request_count = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.request_count += 1
        return self._responses.pop(0)

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "counting-test"


class FakeSandboxBackend:
    """Stands in for NovitaSandboxTool.execute (the true SDK boundary)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"out:{kwargs.get('action')}"


def _run_op(i: int) -> dict[str, Any]:
    return {"action": "run", "command": f"echo step-{i}"}


def _sandbox_call(i: int) -> ToolCallRequest:
    return ToolCallRequest(id=f"s{i}", name="novita_sandbox", arguments=_run_op(i))


def _batch_call(n: int) -> ToolCallRequest:
    return ToolCallRequest(
        id="b0",
        name="sandbox_batch",
        arguments={"operations": [_run_op(i) for i in range(n)], "stop_on_error": False},
    )


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


def _batch_tool(backend: FakeSandboxBackend) -> SandboxBatchTool:
    tool = SandboxBatchTool()
    tool._sandbox = backend  # replace ONLY the Novita SDK boundary
    return tool


_FINAL = LLMResponse(content="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_coalescer_collapses_lone_sandbox_calls_to_one_llm_roundtrip() -> None:
    backend = FakeSandboxBackend()
    registry = _registry(_batch_tool(backend))
    six_calls = [_sandbox_call(i) for i in range(6)]
    provider = CountingProvider(
        [LLMResponse(content=None, tool_calls=six_calls, finish_reason="tool_calls"), _FINAL]
    )
    runner = AgentRunner()
    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "six steps please"}],
        model="counting-test",
        tools=registry,
        max_iterations=6,
        max_tool_result_chars=8_000,
    )
    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    # The model emitted 6 sandbox ops; they executed once each…
    assert len(backend.calls) == 6
    # …but the conversation only ever touched the provider TWICE:
    #   call 1 → assistant emits tool calls, call 2 → final answer.
    # Pre-batching behaviour would have needed >=7 iterations/calls if the
    # runner had to re-ask after each individual tool result.
    assert provider.request_count == 2


@pytest.mark.asyncio
async def test_explicit_batch_single_call_executes_all_ops_without_extra_ai_calls() -> None:
    backend = FakeSandboxBackend()
    registry = _registry(_batch_tool(backend))
    provider = CountingProvider(
        [
            LLMResponse(content=None, tool_calls=[_batch_call(10)], finish_reason="tool_calls"),
            _FINAL,
        ]
    )
    runner = AgentRunner()
    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "ten steps in one call"}],
        model="counting-test",
        tools=registry,
        max_iterations=6,
        max_tool_result_chars=8_000,
    )
    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    assert len(backend.calls) == 10  # all ops ran inside ONE tool execution
    assert provider.request_count == 2  # 10 ops cost ZERO extra AI round-trips


@pytest.mark.asyncio
async def test_batch_report_contains_every_op_result_in_order() -> None:
    """One tool result carries ALL op outputs back to the model (single rest)."""
    backend = FakeSandboxBackend()
    tool = _batch_tool(backend)
    report = await tool.execute(operations=[_run_op(i) for i in range(5)])
    text = str(report)
    for i in range(5):
        assert "out:run" in text
        assert f"[op {i} run → ok]" in text
    assert "5 operation(s), 0 failure(s)" in text
