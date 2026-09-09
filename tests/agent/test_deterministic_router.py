"""The deterministic zero-call router, proven live.

The Manus-style cost discipline extended one layer further: a recurring
read-only UniAbuja ask (status, announcements, my questions, my transcript,
an explicit regno read) is answered by executing the SAME registered tool the
model would have called, with ZERO provider round-trips and ZERO credit steps.

Two layers are covered:

1. Pure routing — ``deterministic_plan`` maps each unambiguous ask to the
   exact tool call, and refuses write-shaped / ambiguous / image-bearing /
   over-long text (falls back to the LLM unchanged).
2. The runner fast-path — a real AgentRunner + real ToolRegistry with a
   counting provider that has NO responses: any provider call would raise, so
   ``request_count == 0`` is the sharpest assertion that the router answered.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.deterministic_router import (
    deterministic_plan,
    last_user_text,
    router_enabled,
)
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider as ProviderBase
from nanobot.providers.base import LLMResponse

# --- Router unit layer -------------------------------------------------------


class TestRouting:
    def test_status_ask(self) -> None:
        call = deterministic_plan("what is my access status")
        assert call is not None
        assert call.name == "uniabuja_student"
        assert call.arguments == {"action": "status"}

    def test_announcements_ask(self) -> None:
        call = deterministic_plan("any new announcements?")
        assert call is not None
        assert call.name == "uniabuja_student"
        assert call.arguments == {"action": "query", "resource": "announcements"}

    def test_my_questions_ask(self) -> None:
        call = deterministic_plan("show my questions")
        assert call is not None
        assert call.arguments == {"action": "query", "resource": "my_questions"}

    def test_my_transcript_ask(self) -> None:
        call = deterministic_plan("i want my transcript please")
        assert call is not None
        assert call.name == "uniabuja_transcript"
        assert call.arguments == {"action": "student_lookup"}

    def test_explicit_regno_lookup(self) -> None:
        call = deterministic_plan("check 22/205EEE/172 results")
        assert call is not None
        assert call.name == "uniabuja_transcript"
        assert call.arguments == {"action": "student_lookup", "regno": "22/205EEE/172"}

    def test_plain_regno_alone_is_not_routed(self) -> None:
        # A regno with no lookup verb is ambiguous (could be a statement, a
        # payment, a complaint). Fall back to the model.
        assert deterministic_plan("22/205EEE/172") is None

    def test_write_shaped_ask_not_routed(self) -> None:
        assert deterministic_plan("build me a resume template") is None
        assert deterministic_plan("update my transcript record") is None
        assert deterministic_plan("please send my results to my email") is None

    def test_eligibility_ask_not_routed(self) -> None:
        # Ambiguous between account access and payment/graduation semantics.
        assert deterministic_plan("am i eligible for this semester") is None

    def test_open_ended_ask_not_routed(self) -> None:
        assert deterministic_plan("hello, how are you doing today?") is None
        assert deterministic_plan("explain how my scores are computed") is None

    def test_overlong_ask_not_routed(self) -> None:
        long_ask = "please show me my transcript " + ("and also tell me " * 60)
        assert deterministic_plan(long_ask) is None

    def test_disabled_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POWERX_DETERMINISTIC_ROUTER", "0")
        assert router_enabled() is False
        assert deterministic_plan("what is my access status") is None


class TestLastUserText:
    def test_plain_string_user_message(self) -> None:
        assert last_user_text([{"role": "user", "content": "show announcements"}]) == (
            "show announcements"
        )

    def test_block_content_returns_none(self) -> None:
        # An image/attachment turn must never be routed.
        content: list[dict[str, Any]] = [{"type": "image_url", "image_url": {"url": "x"}}]
        assert last_user_text([{"role": "user", "content": content}]) is None

    def test_finds_freshest_user_message(self) -> None:
        messages = [
            {"role": "user", "content": "old ask"},
            {"role": "assistant", "content": "an answer"},
            {"role": "user", "content": "what is my access status"},
        ]
        assert last_user_text(messages) == "what is my access status"

    def test_empty_returns_none(self) -> None:
        assert last_user_text([]) is None
        assert last_user_text(None) is None


# --- Runner fast-path layer --------------------------------------------------


class CountingProvider(ProviderBase):
    """Records every request; raises if asked for a response it does not have."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.request_count = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.request_count += 1
        raise AssertionError(f"provider called {self.request_count} times, expected no calls")

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "counting-test"


class RespondingProvider(ProviderBase):
    """Returns canned responses; used to prove the LLM path still runs for
    non-routed asks (request_count == 1, router untouched)."""

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
        return "counting-test"


class StubUniAbujaStudentTool(Tool):
    """Minimal stand-in for uniabuja_student: records calls, returns canned rows."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "uniabuja_student"

    @property
    def description(self) -> str:
        return "Student UniAbuja access (test stub)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "query"]},
                "resource": {"type": "string", "enum": ["announcements", "my_questions"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        action = str(kwargs.get("action") or "")
        if action == "status":
            return ToolResult('{"ok": true, "read_access": true, "write_access": false}')
        return ToolResult('{"ok": true, "rows": [{"id": 1, "title": "hello"}]}')


def _registry(tool: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tool)
    return reg


def _run_spec(provider: CountingProvider, text: str, *, tool: Tool, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "enable_deterministic_router": True,
        "deterministic_router_text": text,
    }
    params.update(kwargs)
    return make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": text}],
        model="counting-test",
        tools=_registry(tool),
        max_iterations=4,
        max_tool_result_chars=8_000,
        **params,
    )


@pytest.mark.asyncio
async def test_router_answers_status_with_zero_provider_calls() -> None:
    tool = StubUniAbujaStudentTool()
    provider = CountingProvider()
    runner = AgentRunner()
    result = await runner.run(
        _run_spec(provider, "what is my access status", tool=tool)
    )

    assert provider.request_count == 0
    assert result.stop_reason == "completed"
    assert "read_access" in (result.final_content or "")
    assert result.usage.get("deterministic") == 1
    assert tool.calls == [{"action": "status"}]

@pytest.mark.asyncio
async def test_router_answers_announcements_with_zero_provider_calls() -> None:
    tool = StubUniAbujaStudentTool()
    provider = CountingProvider()
    runner = AgentRunner()
    result = await runner.run(
        _run_spec(provider, "any new announcements?", tool=tool)
    )

    assert provider.request_count == 0
    assert "rows" in (result.final_content or "")
    assert tool.calls == [{"action": "query", "resource": "announcements"}]


@pytest.mark.asyncio
async def test_unmatched_text_falls_through_to_provider() -> None:
    # A non-routed ask must reach the normal LLM path unchanged: the provider
    # is called exactly once and its answer becomes the final content.
    tool = StubUniAbujaStudentTool()
    provider = RespondingProvider(
        [LLMResponse(content="llm fallback answer", finish_reason="stop")]
    )
    runner = AgentRunner()
    result = await runner.run(_run_spec(provider, "write me a poem about rain", tool=tool))

    assert provider.request_count == 1
    assert result.final_content == "llm fallback answer"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_router_disabled_falls_through_to_provider() -> None:
    tool = StubUniAbujaStudentTool()
    provider = RespondingProvider(
        [LLMResponse(content="llm fallback answer", finish_reason="stop")]
    )
    runner = AgentRunner()
    result = await runner.run(
        _run_spec(
            provider,
            "what is my access status",
            tool=tool,
            enable_deterministic_router=False,
            deterministic_router_text=None,
        )
    )

    assert provider.request_count == 1
    assert result.final_content == "llm fallback answer"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_router_skips_when_tool_not_registered() -> None:
    # Plan matches uniabuja_transcript, but the registry only has the student
    # stub. The run must fall through to the normal LLM path, not crash on an
    # unregistered tool.
    tool = StubUniAbujaStudentTool()
    provider = RespondingProvider(
        [LLMResponse(content="llm fallback answer", finish_reason="stop")]
    )
    runner = AgentRunner()
    result = await runner.run(_run_spec(provider, "check 22/205EEE/172 results", tool=tool))

    assert provider.request_count == 1
    assert result.final_content == "llm fallback answer"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_router_is_an_early_return_before_replay_cache_write() -> None:
    # The deterministic answer must be produced WITHOUT consulting or writing
    # the replay cache (it is a live-data lookup, not a cacheable answer).
    # The CountingProvider would fail on any LLM call, so a clean completion
    # already proves the fast-path ran first.
    tool = StubUniAbujaStudentTool()
    provider = CountingProvider()
    runner = AgentRunner()
    await runner.run(
        _run_spec(provider, "show my questions", tool=tool)
    )
    assert provider.request_count == 0
    assert tool.calls == [{"action": "query", "resource": "my_questions"}]
