"""The API-cost layers, proven live (post-batch: no ``sandbox_batch``):

1. Terminal completion — a tool result that declares ``terminal=True`` ends the
   turn in ONE provider round-trip (the call that emitted the final command).
   No separate final-answer call.
2. Zero-call task recipes — the generic task router answers recurring,
   read-only coding/workspace asks by running ONE deterministic command inside
   the sandbox (novita_sandbox), with ZERO provider calls.
3. Replay cache — an identical task completed once is served from disk on the
   next occurrence: zero provider round-trips, zero token spend.

Same harness style as the old batch suite: real AgentRunner + real tools, only
the SDK boundary faked via stubs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.runner import AgentRunner
from nanobot.agent.task_cache import TaskReplayCache
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.utils.llm_runtime import LLMRuntime


def make_run_spec(provider: Any, **kwargs: Any) -> Any:
    """Build an AgentRunSpec around a provider (mirrors tests/agent helper)."""
    from nanobot.agent.runner import AgentRunSpec
    from nanobot.config.schema import AgentDefaults

    model = kwargs.pop("model", None) or provider.get_default_model()
    generation = kwargs.pop("generation", None)
    if generation is None:
        generation = GenerationSettings(temperature=None, max_tokens=None)
    context_window_tokens = kwargs.pop(
        "context_window_tokens", AgentDefaults().context_window_tokens
    )
    runtime = LLMRuntime(
        provider=provider,
        model=model,
        generation=generation,
        context_window_tokens=int(context_window_tokens),
    )
    return AgentRunSpec(runtime=runtime, **kwargs)


# ---------------------------------------------------------------------------
# Fakes at the true boundaries
# ---------------------------------------------------------------------------


class CountingProvider(OpenAICompatProvider):
    """Returns scripted responses; raises if asked for one it does not have."""

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
        return "cost-test"


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


class StubTerminalSandboxTool(Tool):
    """``novita_sandbox`` shaped stub whose 'complete' action returns a
    TERMINAL ToolResult — proving the runner ends the turn without paying for
    a closing model call."""

    def __init__(self, backend: FakeSandboxBackend) -> None:
        self._backend = backend

    @property
    def name(self) -> str:
        return "novita_sandbox"

    @property
    def description(self) -> str:
        return "Run sandbox operations (test stub)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "command": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        action = str(kwargs.get("action") or "")
        if action == "complete":
            message = str(kwargs.get("command") or "").strip()
            if not message:
                return ToolResult.error("ERR complete requires a message")
            return ToolResult(message, terminal=True)
        out = await self._backend.execute(**kwargs)
        return ToolResult(str(out))


def _registry(*tools: Any) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


def _sandbox_tool(backend: FakeSandboxBackend) -> StubTerminalSandboxTool:
    return StubTerminalSandboxTool(backend)


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin all runtime data dirs into tmp_path so caches never leak."""
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path / "data"))
    import nanobot.config.paths as paths

    monkeypatch.setattr(paths, "get_data_dir", lambda: tmp_path / "data")
    return tmp_path


# --- 1. Terminal completion: one round-trip, zero final-answer call ---------


@pytest.mark.asyncio
async def test_terminal_result_ends_turn_without_final_answer_call() -> None:
    backend = FakeSandboxBackend()
    registry = _registry(_sandbox_tool(backend))
    provider = CountingProvider(
        [
            LLMResponse(
                content="working",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="b1",
                        name="novita_sandbox",
                        arguments={"action": "run", "command": "pytest -q"},
                    )
                ],
            ),
            LLMResponse(
                content="done",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="b2",
                        name="novita_sandbox",
                        arguments={"action": "complete", "command": "Build and tests done in one call."},
                    )
                ],
            ),
        ]
    )
    runner = AgentRunner()
    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "build and test"}],
        tools=registry,
        max_iterations=6,
        max_tool_result_chars=8_000,
        model="cost-test",
    )
    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    assert result.final_content == "Build and tests done in one call."
    assert len(backend.calls) == 1  # the real op ran once inside the sandbox
    # The terminal result ended the turn: NO third provider call for a closing
    # answer. Any third call would raise AssertionError in CountingProvider.
    assert provider.request_count == 2


@pytest.mark.asyncio
async def test_terminal_requires_message() -> None:
    backend = FakeSandboxBackend()
    tool = _sandbox_tool(backend)
    report = await tool.execute(action="complete")
    text = str(report)
    assert "ERR" in text
    assert "message" in text


# --- 2. Zero-call task recipe runs through novita_sandbox -------------------


@pytest.mark.asyncio
async def test_task_recipe_runs_in_sandbox_with_zero_llm_calls() -> None:
    from nanobot.agent.task_router import task_recipe_plan

    call = task_recipe_plan("check this code for bugs")
    assert call is not None
    assert call.name == "novita_sandbox"
    assert call.arguments["action"] == "run"

    backend = FakeSandboxBackend(script=["no syntax errors found"])
    registry = _registry(_sandbox_tool(backend))
    provider = CountingProvider([])  # any provider call fails the test
    runner = AgentRunner()
    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "check this code for bugs"}],
        tools=registry,
        max_iterations=4,
        max_tool_result_chars=8_000,
        model="cost-test",
        enable_deterministic_router=True,
        deterministic_router_text="check this code for bugs",
    )
    result = await runner.run(spec)

    assert provider.request_count == 0
    assert len(backend.calls) == 1
    assert "py_compile" in backend.calls[0]["command"]
    assert result.final_content and "no syntax errors found" in result.final_content


# --- 3. Replay cache: identical task answered twice costs one round-trip -----


@pytest.mark.asyncio
async def test_replay_cache_serves_second_run_without_provider(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POWERX_REPLAY_CACHE", "1")
    workspace = str(isolated_data_dir / "ws")

    task_text = "please build the android application release apk for me now"
    backend = FakeSandboxBackend(script=["tests passed", "tests passed"])
    registry = _registry(_sandbox_tool(backend))
    responses = [
        LLMResponse(
            content="running",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="novita_sandbox",
                    arguments={"action": "run", "command": "pytest -q"},
                )
            ],
        ),
        LLMResponse(content="all green", finish_reason="stop"),
    ]
    provider_a = CountingProvider(list(responses))
    provider_b = CountingProvider(list(responses))
    runner = AgentRunner()

    first = await runner.run(
        make_run_spec(
            provider_a,
            initial_messages=[{"role": "user", "content": task_text}],
            tools=registry,
            max_iterations=6,
            max_tool_result_chars=8_000,
            model="cost-test",
            workspace=Path(workspace),
            enable_replay_cache=True,
        )
    )
    second = await runner.run(
        make_run_spec(
            provider_b,
            initial_messages=[{"role": "user", "content": task_text}],
            tools=registry,
            max_iterations=6,
            max_tool_result_chars=8_000,
            model="cost-test",
            workspace=Path(workspace),
            enable_replay_cache=True,
        )
    )

    assert first.stop_reason == "completed"
    assert provider_a.request_count == 2
    # Second identical task: served from the replay cache, ZERO provider calls.
    assert second.stop_reason == "completed"
    assert provider_b.request_count == 0
    assert second.final_content == "all green"


@pytest.mark.asyncio
async def test_replay_cache_disabled_pays_full_cost_again(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POWERX_REPLAY_CACHE", "1")
    workspace = str(isolated_data_dir / "ws2")

    task_text = "please build the android application release apk for me now"
    backend = FakeSandboxBackend(script=["tests passed", "tests passed"])
    registry = _registry(_sandbox_tool(backend))
    responses = [
        LLMResponse(
            content="running",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="novita_sandbox",
                    arguments={"action": "run", "command": "pytest -q"},
                )
            ],
        ),
        LLMResponse(content="all green", finish_reason="stop"),
    ]
    provider_a = CountingProvider(list(responses))
    provider_b = CountingProvider(list(responses))
    runner = AgentRunner()
    await runner.run(
        make_run_spec(
            provider_a,
            initial_messages=[{"role": "user", "content": task_text}],
            tools=registry,
            max_iterations=6,
            max_tool_result_chars=8_000,
            model="cost-test",
            workspace=Path(workspace),
            enable_replay_cache=True,
        )
    )
    second = await runner.run(
        make_run_spec(
            provider_b,
            initial_messages=[{"role": "user", "content": task_text}],
            tools=registry,
            max_iterations=6,
            max_tool_result_chars=8_000,
            model="cost-test",
            workspace=Path(workspace),
            enable_replay_cache=False,
        )
    )
    # With the cache disabled, the second run pays the FULL normal cost again.
    assert second.stop_reason == "completed"
    assert provider_a.request_count == 2
    assert provider_b.request_count == 2


def test_replay_cache_unit(isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERX_REPLAY_CACHE", "1")
    cache = TaskReplayCache(workspace=str(isolated_data_dir / "ws3"))
    task = "summarise the quarterly revenue numbers for the board deck please"
    assert cache.get(task) is None
    assert cache.put(task, "Revenue up 12% QoQ.") is True
    assert cache.get(task) == "Revenue up 12% QoQ."
    # A different task must NOT hit the same fingerprint.
    assert cache.get("summarise the quarterly profit numbers for the board deck please") is None
