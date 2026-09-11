"""Generic task-recipe router tests.

The generic task router answers recurring, read-only *coding/workspace* tasks
("check for bugs", "run the tests", "summarise the project") with a single,
deterministic ``exec`` command — ZERO LLM calls. This is the Manus-style
"command runner" discipline: routine shapes of work never pay the model.

Two layers are covered, mirroring the existing deterministic-router suite:

1. Pure routing — ``task_recipe_plan`` maps each unambiguous ask to the exact
   ``exec`` call, and refuses write/build-shaped, ambiguous, or image-bearing
   text (falls back to the LLM unchanged).
2. The runner fast-path — a real AgentRunner + real ToolRegistry with a
   counting provider that raises on any call, so ``request_count == 0`` is the
   sharpest proof the recipe answered deterministically.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.runner import AgentRunner
from nanobot.agent.task_router import task_recipe_plan, task_router_enabled
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider as ProviderBase

# --- Pure routing unit layer -------------------------------------------------


class TestTaskRouting:
    def test_bug_check_ask_routes(self) -> None:
        call = task_recipe_plan("check this code for bugs")
        assert call is not None
        assert call.name == "sandbox_batch"
        ops = call.arguments["operations"]
        assert isinstance(ops, list) and len(ops) == 1
        assert ops[0]["action"] == "run"
        assert "py_compile" in ops[0]["command"]
        assert call.id == "task-bug-scan"

    def test_look_for_issues_routes(self) -> None:
        call = task_recipe_plan("look for issues in the project please")
        assert call is not None
        assert call.name == "sandbox_batch"
        assert call.id == "task-bug-scan"

    def test_review_code_routes(self) -> None:
        call = task_recipe_plan("review the code for bugs")
        assert call is not None
        assert call.id == "task-bug-scan"

    def test_run_tests_routes(self) -> None:
        call = task_recipe_plan("run the tests")
        assert call is not None
        assert call.name == "sandbox_batch"
        assert call.id == "task-run-tests"
        assert "pytest" in call.arguments["operations"][0]["command"]

    def test_run_pytest_routes(self) -> None:
        call = task_recipe_plan("please run pytest")
        assert call is not None
        assert call.id == "task-run-tests"

    def test_project_structure_routes(self) -> None:
        call = task_recipe_plan("list the project structure")
        assert call is not None
        assert call.id == "task-project-structure"

    def test_show_workspace_files_routes(self) -> None:
        call = task_recipe_plan("show me what files are in this workspace")
        assert call is not None
        assert call.id == "task-project-structure"

    def test_write_shaped_ask_not_routed(self) -> None:
        # Builds / mutations / refactors are the model's job, not a read-only scan.
        assert task_recipe_plan("build a web app") is None
        assert task_recipe_plan("fix the bugs in this project") is None
        assert task_recipe_plan("refactor the code to be faster") is None
        assert task_recipe_plan("deploy this to production") is None

    def test_conversational_ask_not_routed(self) -> None:
        assert task_recipe_plan("hello, how are you doing today?") is None
        assert task_recipe_plan("tell me a joke") is None

    def test_overlong_ask_not_routed(self) -> None:
        long_ask = "check the code for bugs " + ("and also " * 60)
        assert task_recipe_plan(long_ask) is None

    def test_disabled_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POWERX_TASK_ROUTER", "0")
        assert task_router_enabled() is False
        assert task_recipe_plan("check this code for bugs") is None


# --- Runner fast-path layer --------------------------------------------------


class TaskRejectingProvider(ProviderBase):
    """Raises on any LLM call; a clean completion proves zero calls happened."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.request_count = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.request_count += 1
        raise AssertionError(f"LLM called {self.request_count} times, expected zero")

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "task-test"


class StubSandboxBatchTool(Tool):
    """Minimal stand-in for ``sandbox_batch``: records ops, returns canned report."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "sandbox_batch"

    @property
    def description(self) -> str:
        return "Run sandbox operations (test stub)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operations": {"type": "array"},
                "stop_on_error": {"type": "boolean"},
            },
            "required": ["operations"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        ops = kwargs.get("operations") or [{}]
        cmd = str(ops[0].get("command") or "")[:40]
        return ToolResult(f"[sandbox_batch: 1 operation(s), 0 failure(s)]\nran: {cmd}")


class StubExecTool(Tool):
    """Minimal stand-in for ``exec``: records commands, returns canned output."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command (test stub)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return ToolResult(f"ran: {(kwargs.get('command') or '')[:40]}")


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


def _run_spec(provider: TaskRejectingProvider, text: str, *, tools: ToolRegistry, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "enable_deterministic_router": True,
        "deterministic_router_text": text,
    }
    params.update(kwargs)
    return make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": text}],
        model="task-test",
        tools=tools,
        max_iterations=4,
        max_tool_result_chars=8_000,
        **params,
    )


@pytest.mark.asyncio
async def test_bug_scan_answered_with_zero_provider_calls_via_sandbox() -> None:
    # Primary path: the recipe runs inside the sandbox where the user's code lives.
    tool = StubSandboxBatchTool()
    provider = TaskRejectingProvider()
    runner = AgentRunner()
    result = await runner.run(
        _run_spec(provider, "check this code for bugs", tools=_registry(tool))
    )

    assert provider.request_count == 0
    assert result.usage.get("deterministic") == 1
    assert result.usage.get("llm_calls") == 0
    assert result.stop_reason == "completed"
    assert "py_compile" in tool.calls[0]["operations"][0]["command"]


@pytest.mark.asyncio
async def test_run_tests_answered_with_zero_provider_calls_via_sandbox() -> None:
    tool = StubSandboxBatchTool()
    provider = TaskRejectingProvider()
    runner = AgentRunner()
    result = await runner.run(
        _run_spec(provider, "run the tests", tools=_registry(tool))
    )

    assert provider.request_count == 0
    assert result.usage.get("deterministic") == 1
    assert "pytest" in tool.calls[0]["operations"][0]["command"]


@pytest.mark.asyncio
async def test_recipe_falls_back_to_exec_when_no_sandbox_registered() -> None:
    # No sandbox_batch on this run but exec is present -> the runner re-targets
    # the same read-only command to the local shell, still with ZERO LLM calls.
    exec_tool = StubExecTool()
    provider = TaskRejectingProvider()
    runner = AgentRunner()
    result = await runner.run(
        _run_spec(provider, "list the project structure", tools=_registry(exec_tool))
    )

    assert provider.request_count == 0
    assert result.usage.get("deterministic") == 1
    assert "tree" in exec_tool.calls[0]["command"] or "find" in exec_tool.calls[0]["command"]


@pytest.mark.asyncio
async def test_unrouted_ask_falls_through_when_no_command_tool() -> None:
    # A routed task but neither sandbox_batch nor exec registered -> must fall
    # through untouched to the model, not crash.
    other_registry = ToolRegistry()

    class Other(Tool):
        @property
        def name(self) -> str:
            return "some_other_tool"

        @property
        def description(self) -> str:
            return "unrelated"

        @property
        def parameters(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs: Any) -> Any:  # pragma: no cover
            return ToolResult("ok")

    other_registry.register(Other())

    from nanobot.providers.base import LLMResponse

    class ReplyProvider(TaskRejectingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.responses = [LLMResponse(content="llm handled it", finish_reason="stop")]

        async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
            self.request_count += 1
            return self.responses.pop(0)

    provider = ReplyProvider()
    runner = AgentRunner()
    result = await runner.run(
        make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "check this code for bugs"}],
            model="task-test",
            tools=other_registry,
            max_iterations=4,
            max_tool_result_chars=8_000,
            enable_deterministic_router=True,
            deterministic_router_text="check this code for bugs",
        )
    )
    assert provider.request_count == 1
    assert result.final_content == "llm handled it"

