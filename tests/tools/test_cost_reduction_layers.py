"""The three API-cost layers, proven live:

1. Terminal ``complete`` op — a task that ends with ``action=complete`` inside
   sandbox_batch finishes in ONE provider round-trip (the call that emitted the
   batch). No separate final-answer call.
2. Silent in-sandbox retries — an op with ``retries`` re-runs inside the same
   batch when it fails, so a flaky command never costs an extra LLM call.
3. Replay cache — an identical task completed recently replays with ZERO
   provider calls (usage marks ``replayed``).

Same harness as ``test_sandbox_batch_llm_cost.py``: real AgentRunner + real
SandboxBatchTool, only the Novita SDK boundary faked.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent.runner_helpers import make_run_spec

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.sandbox_batch import SandboxBatchTool
from nanobot.providers.base import LLMProvider as ProviderBase, LLMResponse, ToolCallRequest


class CountingProvider(ProviderBase):
    """Records every request; raises if asked for a response it does not have.

    Passing an empty ``responses`` list makes any unexpected provider call fail
    loudly — the sharpest assertion that we did NOT spend a round-trip.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self._responses = list(responses)
        self.request_count = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.request_count += 1
        if not self._responses:
            raise AssertionError(f"provider called {self.request_count} times, expected no more calls")
        return self._responses.pop(0)

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "counting-test"


class FakeSandboxBackend:
    """Stands in for NovitaSandboxTool.execute (the true SDK boundary)."""

    def __init__(self, script: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        # Optional per-call scripted outputs: each call pops the next entry.
        self._script = list(script or [])

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self._script:
            return self._script.pop(0)
        return f"out:{kwargs.get('action')}"


def _registry(*tools: Any) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


def _batch_tool(backend: FakeSandboxBackend) -> SandboxBatchTool:
    tool = SandboxBatchTool()
    tool._sandbox = backend  # replace ONLY the Novita SDK boundary
    return tool


# --- 1. Terminal complete op: one round-trip, zero final-answer call ---------


@pytest.mark.asyncio
async def test_terminal_complete_op_ends_turn_in_one_provider_call() -> None:
    backend = FakeSandboxBackend()
    registry = _registry(_batch_tool(backend))
    # Model emits ONE batch: two real ops then action=complete with the summary.
    batch_call = ToolCallRequest(
        id="b0",
        name="sandbox_batch",
        arguments={
            "operations": [
                {"action": "run", "command": "echo build"},
                {"action": "run", "command": "echo test"},
                {"action": "complete", "message": "Build and tests done in one call."},
            ]
        },
    )
    provider = CountingProvider(
        [LLMResponse(content=None, tool_calls=[batch_call], finish_reason="tool_calls")]
    )
    runner = AgentRunner()
    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "build and test, then summarize"}],
        model="counting-test",
        tools=registry,
        max_iterations=6,
        max_tool_result_chars=8_000,
    )
    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    assert result.final_content == "Build and tests done in one call."
    assert len(backend.calls) == 2  # both real ops ran
    # The complete op ended the turn: NO second provider call for a closing
    # answer. Any second call would raise AssertionError in CountingProvider.
    assert provider.request_count == 1


@pytest.mark.asyncio
async def test_complete_requires_message() -> None:
    backend = FakeSandboxBackend()
    tool = _batch_tool(backend)
    report = await tool.execute(operations=[{"action": "complete"}])
    text = str(report)
    assert "ERR" in text
    assert "message" in text


# --- 2. Silent in-sandbox retries: flakes never bounce back to the model -----


@pytest.mark.asyncio
async def test_retries_rerun_failed_op_inside_batch_without_extra_llm_call() -> None:
    # First attempt fails ([exit=1]), the retry succeeds — but only ONE batch
    # execution happens, so the model never sees the failure mid-flight.
    backend = FakeSandboxBackend(script=["[exit=1] transient blip", "out:ok"])
    tool = _batch_tool(backend)
    await tool.execute(
        operations=[{"action": "run", "command": "flaky-cmd", "retries": 2}]
    )
    assert len(backend.calls) == 2  # original + one silent retry, not 3
    assert [c.get("command") for c in backend.calls] == ["flaky-cmd", "flaky-cmd"]


@pytest.mark.asyncio
async def test_retries_with_match_accept_only_matching_output() -> None:
    # Output never contains the required token despite retries → still fails,
    # and all attempts stay inside the single batch.
    backend = FakeSandboxBackend(script=["building...", "building...", "building..."])
    tool = _batch_tool(backend)
    report = await tool.execute(
        operations=[
            {
                "action": "run",
                "command": "build.sh",
                "retries": 2,
                "match": "build succeeded",
            }
        ]
    )
    text = str(report)
    assert len(backend.calls) == 3  # exhausted all attempts silently
    assert "1 failure(s)" in text


@pytest.mark.asyncio
async def test_retries_succeed_when_match_eventually_appears() -> None:
    backend = FakeSandboxBackend(script=["starting", "started", "[exit=0] build succeeded"])
    tool = _batch_tool(backend)
    report = await tool.execute(
        operations=[
            {
                "action": "run",
                "command": "build.sh",
                "retries": 3,
                "match": "build succeeded",
            }
        ]
    )
    text = str(report)
    assert len(backend.calls) == 3
    assert "0 failure(s)" in text
    assert "build succeeded" in text


# --- 3. Replay cache: identical task replays with ZERO provider calls --------


@pytest.mark.asyncio
async def test_replay_cache_serves_identical_task_with_zero_provider_calls(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate the cache root so the test never touches real runtime state.
    monkeypatch.setattr(
        "nanobot.agent.task_cache.get_runtime_subdir",
        lambda _name: tmp_path / "replay_cache",
    )

    task_text = "build a landing page for a coffee shop and deploy it to vercel"
    user_msg = {"role": "user", "content": task_text}
    backend = FakeSandboxBackend()

    def run_once(provider: CountingProvider) -> AgentRunSpec:
        registry = _registry(_batch_tool(backend))
        return make_run_spec(
            provider,
            initial_messages=[user_msg],
            model="counting-test",
            tools=registry,
            max_iterations=6,
            max_tool_result_chars=8_000,
            enable_replay_cache=True,
            workspace=str(tmp_path / "workspace"),
        )

    runner = AgentRunner()
    # First run: one batch (build + complete), one provider call total.
    first_provider = CountingProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="b0",
                        name="sandbox_batch",
                        arguments={
                            "operations": [
                                {"action": "run", "command": "echo build"},
                                {"action": "complete", "message": "done: landing page live at https://x.dev"},
                            ]
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    first = await runner.run(run_once(first_provider))
    assert first.stop_reason == "completed"
    assert "landing page live" in (first.final_content or "")
    assert first_provider.request_count == 1

    # Second run: identical task text → the cache replays the stored answer.
    # The provider has NO responses; any call fails the test.
    second_provider = CountingProvider([])
    second = await runner.run(run_once(second_provider))
    assert second.stop_reason == "completed"
    assert second.final_content == first.final_content
    assert second_provider.request_count == 0
    assert second.usage.get("replayed") == 1
    assert second.tools_used == []


@pytest.mark.asyncio
async def test_replay_cache_not_consulted_when_disabled(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nanobot.agent.task_cache.get_runtime_subdir",
        lambda _name: tmp_path / "replay_cache",
    )
    user_msg = {"role": "user", "content": "same identical task text over and over again"}
    backend = FakeSandboxBackend()

    def run_once(provider: CountingProvider) -> AgentRunSpec:
        registry = _registry(_batch_tool(backend))
        return make_run_spec(
            provider,
            initial_messages=[user_msg],
            model="counting-test",
            tools=registry,
            max_iterations=6,
            max_tool_result_chars=8_000,
            enable_replay_cache=False,  # opt-out
            workspace=str(tmp_path / "workspace"),
        )

    runner = AgentRunner()
    responses = [
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="b0",
                    name="sandbox_batch",
                    arguments={
                        "operations": [
                            {"action": "run", "command": "echo hi"},
                            {"action": "complete", "message": "completed once"},
                        ]
                    },
                )
            ],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="final answer", finish_reason="stop"),
    ]
    provider_a = CountingProvider(list(responses))
    provider_b = CountingProvider(list(responses))
    await runner.run(run_once(provider_a))
    second = await runner.run(run_once(provider_b))
    # With the cache disabled, the second run pays the FULL normal cost
    # again (1 call: batch emit + terminal complete) — NOT zero.
    assert second.stop_reason == "completed"
    assert provider_a.request_count == 1
    assert provider_b.request_count == 1
    assert second.usage.get("replayed") != 1