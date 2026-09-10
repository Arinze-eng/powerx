"""AI-Efficiency Engine tests.

The core idea behind Manus's 22 API calls / 13h of work is that the LLM is
only paid at genuine decision points; everything else is resolved by
deterministic rules, caches, plan replays and tool middleware. This suite
guards the two pieces we layer on top of that discipline:

1. The **channel-agnostic deterministic router** — fresh text *file/workspace*
   lookups ("find config.json", "where is main.py") are answered by executing
   the ``file_search`` tool with ZERO provider calls, on ANY channel (webui,
   api, telegram), not just Telegram.

2. The **llm-call telemetry** — ``result.usage["llm_calls"]`` honestly reports
   how many distinct requests actually hit the configured LLM, so
   "API called: N" (the metric from the Manus task panel) is observable.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.deterministic_router import deterministic_plan
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider as ProviderBase
from nanobot.providers.base import LLMResponse, ToolCallRequest


class RejectingProvider(ProviderBase):
    """Raises on any make at the LLM: a clean completion means zero LLM calls."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.request_count = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.request_count += 1
        raise AssertionError(f"LLM called {self.request_count} times, expected zero")

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "efficiency-test"


class RespondingProvider(ProviderBase):
    """Returns canned responses; used to prove the LLM path still runs for
    non-routed asks."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self.responses = list(responses)
        self.request_count = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.request_count += 1
        if not self.responses:
            raise AssertionError("provider called with no responses left")
        return self.responses.pop(0)

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "efficiency-test"


class StubFileSearchTool(Tool):
    """Minimal stand-in for file_search: records queries, returns canned rows."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "file_search"

    @property
    def description(self) -> str:
        return "Search the workspace (test stub)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "expand_paths": {"type": "boolean"},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return ToolResult('{"matches": [{"path": "src/' + str(kwargs.get("query")) + '"}]}')


def _registry(tool: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tool)
    return reg


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


def _run_spec(
    provider: Any,
    text: str,
    *,
    tool: Tool,
    enable_router: bool = True,
    **kwargs: Any,
) -> Any:
    params: dict[str, Any] = {
        "enable_deterministic_router": enable_router,
        "deterministic_router_text": text,
    }
    params.update(kwargs)
    return make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": text}],
        model="efficiency-test",
        tools=_registry(tool),
        max_iterations=4,
        max_tool_result_chars=8_000,
        **params,
    )


# --- Generic file-lookup routing (channel-agnostic) -------------------------

class TestGenericFileLookupRouting:
    def test_concrete_file_ask_routes(self) -> None:
        call = deterministic_plan("find the file config.json")
        assert call is not None
        assert call.name == "file_search"
        assert call.arguments["query"] == "config.json"

    def test_where_is_file_routes(self) -> None:
        call = deterministic_plan("where is the setup.py file")
        assert call is not None
        assert call.name == "file_search"
        assert call.arguments["query"] == "setup.py"

    def test_show_me_file_routes(self) -> None:
        call = deterministic_plan("show me main.py")
        assert call is not None
        assert call.arguments["query"] == "main.py"

    def test_quoted_filename_wins(self) -> None:
        call = deterministic_plan('what is in the "data/report.pdf" file')
        assert call is not None
        assert call.arguments["query"] == "data/report.pdf"

    def test_multiple_file_terms_routes_last_most_specific(self) -> None:
        call = deterministic_plan("search for app.js implementation in server.ts")
        assert call is not None
        assert call.name == "file_search"

    def test_conversational_ask_not_routed(self) -> None:
        assert deterministic_plan("find me a good movie to watch") is None

    def test_weather_ask_not_routed(self) -> None:
        assert deterministic_plan("what is the weather in lagos") is None

    def test_vague_file_mention_not_routed(self) -> None:
        # No concrete file-like name -> the model must decide.
        assert deterministic_plan("list the files in src") is None

    def test_write_shaped_ask_not_routed(self) -> None:
        assert deterministic_plan("delete the file temp.txt") is None
        assert deterministic_plan("build a python script") is None
        assert deterministic_plan("create config.json for me") is None

    def test_hello_not_routed(self) -> None:
        assert deterministic_plan("hello there") is None


@pytest.mark.asyncio
async def test_generic_file_lookup_answered_with_zero_provider_calls() -> None:
    tool = StubFileSearchTool()
    provider = RejectingProvider()
    runner = AgentRunner()
    result = await runner.run(_run_spec(provider, "find the file config.json", tool=tool))

    assert provider.request_count == 0
    assert result.usage.get("deterministic") == 1
    assert result.usage.get("llm_calls") == 0
    assert tool.calls == [{"query": "config.json", "expand_paths": True}]


@pytest.mark.asyncio
async def test_generic_lookup_falls_through_when_tool_not_registered() -> None:
    # Router matches file_search, but registry only has a different tool.
    # Must fall through to the LLM path, not crash.
    reg = ToolRegistry()
    reg.register(_other())
    provider = RespondingProvider([LLMResponse(content="llm handled it", finish_reason="stop")])
    runner = AgentRunner()
    result = await runner.run(
        make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "find the file config.json"}],
            model="efficiency-test",
            tools=reg,
            max_iterations=4,
            max_tool_result_chars=8_000,
            enable_deterministic_router=True,
            deterministic_router_text="find the file config.json",
        )
    )
    assert provider.request_count == 1
    assert result.final_content == "llm handled it"
    assert result.usage.get("llm_calls") == 1


def _other() -> Any:
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

    return Other()


# --- llm_calls telemetry ------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_calls_zero_on_router_answer() -> None:
    tool = StubFileSearchTool()
    provider = RejectingProvider()
    runner = AgentRunner()
    result = await runner.run(_run_spec(provider, "find the file config.json", tool=tool))
    assert result.usage.get("llm_calls") == 0


@pytest.mark.asyncio
async def test_llm_calls_equals_one_on_normal_llm_turn() -> None:
    tool = StubFileSearchTool()
    provider = RespondingProvider([LLMResponse(content="hello back", finish_reason="stop")])
    runner = AgentRunner()
    result = await runner.run(
        _run_spec(provider, "hello there how are you", tool=tool, enable_router=False, deterministic_router_text=None)
    )
    assert provider.request_count == 1
    assert result.usage.get("llm_calls") == 1


@pytest.mark.asyncio
async def test_llm_calls_counts_distinct_requests_not_iterations() -> None:
    # A turn that does one tool call then a final model reply = TWO distinct
    # model requests (first for planning, second for finalization). The
    # counter must reflect that real count.
    tool = StubFileSearchTool()
    provider = RespondingProvider(
        [
            # First request: model decides to call the tool.
            LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    # The runner needs typed ToolCallRequest, not a bare dict.
                    _tool_call("call_1", "file_search", {"query": "x.py"})
                ],
            ),
            # Second request: model finalizes after seeing the tool result.
            LLMResponse(content="the file is x.py", finish_reason="stop"),
        ]
    )
    runner = AgentRunner()
    result = await runner.run(
        _run_spec(
            provider,
            "show me x.py",
            tool=tool,
            enable_router=False,
            deterministic_router_text=None,
        )
    )
    assert result.usage.get("llm_calls") == 2
    assert provider.request_count == 2