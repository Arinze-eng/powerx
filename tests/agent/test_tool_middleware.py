"""Zero-call tool middleware, proven live.

The fourth Manus-style cost layer: once a governed read-only UniAbuja lookup
has run, NOTHING about serving its answer should cost another API call —

1. Pure rendering (``render_uniabuja_output``): known JSON envelopes become
   chat text with zero tokens; anything ambiguous returns None so the model
   keeps control.
2. Short-TTL live cache + in-flight singleflight (``ToolMiddleware.execute``):
   repeats inside the window never touch the backend; concurrent duplicates
   collapse into one execution; volatile reads (my_questions, transcript) are
   never cached.
3. Runner integration: an in-loop governed call whose output renders ends the
   turn with ZERO further provider calls (CountingProvider would raise on any
   second request), and the deterministic router's answers flow through the
   same cache.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.deterministic_router import deterministic_plan
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tool_middleware import (
    ToolMiddleware,
    middleware_enabled,
    render_uniabuja_output,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider as ProviderBase, LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime


# ---------------------------------------------------------------------------
# Fixtures: counting provider + stub tools
# ---------------------------------------------------------------------------


class CountingProvider(ProviderBase):
    """Records every request; raises if asked for a SECOND response."""

    def __init__(self, first_response: LLMResponse | None = None) -> None:
        super().__init__(api_key="test")
        self.request_count = 0
        self.first_response = first_response

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.request_count += 1
        if self.request_count == 1 and self.first_response is not None:
            return self.first_response
        # A non-retryable provider error surfaces as stop_reason="provider_error"
        # so tests can assert "the model was consulted again" without the retry
        # loop swallowing it or an exception being converted to a safe response.
        return LLMResponse(
            content=f"provider called {self.request_count} times; "
            "middleware should have ended the turn",
            finish_reason="error",
            error_should_retry=False,
        )

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:  # pragma: no cover
        return "test-model"


class StubStudentTool(Tool):
    """Stands in for uniabuja_student; records executions."""

    def __init__(self, payload: dict[str, Any] | None = None, delay: float = 0.0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload = payload or {"ok": True, "action": "query", "resource": "announcements", "rows": []}
        self.delay = delay

    @property
    def name(self) -> str:
        return "uniabuja_student"

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "query", "submit_question", "transcript"]},
                "resource": {"type": "string", "enum": ["knowledge", "announcements", "my_questions"]},
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        payload = dict(self.payload)
        if kwargs.get("action") == "status":
            payload = {"ok": True, "action": "status", "access_level": "student", "enabled": True}
        return json.dumps(payload)


class StubTranscriptTool(Tool):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload = payload

    @property
    def name(self) -> str:
        return "uniabuja_transcript"

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return json.dumps(self.payload)


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _call(name: str, arguments: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id="c1", name=name, arguments=arguments)


# ---------------------------------------------------------------------------
# 1. Pure rendering
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_announcements_rows(self) -> None:
        raw = json.dumps(
            {
                "ok": True,
                "action": "query",
                "resource": "announcements",
                "rows": [
                    {"title": "Result upload", "date": "2026-09-01", "body": "Semester results are out"},
                    {"title": "Resumption", "body": "x" * 400},
                ],
            }
        )
        out = render_uniabuja_output(
            "uniabuja_student", {"action": "query", "resource": "announcements"}, raw
        )
        assert out is not None
        assert "📢 Announcements" in out
        assert "• Result upload (2026-09-01)" in out
        assert "…and 0 more" not in out
        # Long bodies are clipped, never dumped whole.
        assert "x" * 400 not in out

    def test_empty_rows(self) -> None:
        raw = json.dumps({"ok": True, "action": "query", "resource": "announcements", "rows": []})
        out = render_uniabuja_output(
            "uniabuja_student", {"action": "query", "resource": "announcements"}, raw
        )
        assert out is not None and "Nothing found" in out

    def test_status_envelope(self) -> None:
        raw = json.dumps({"ok": True, "action": "status", "access_level": "student", "enabled": False})
        out = render_uniabuja_output("uniabuja_student", {"action": "status"}, raw)
        assert out is not None
        assert "access level: student" in out
        assert "enabled: no" in out

    def test_transcript_record(self) -> None:
        raw = json.dumps(
            {
                "ok": True,
                "student": {"first_name": "Ada", "surname": "Okafor", "regno": "22/205EEE/172", "status": "ACTIVE"},
                "courses": [{"course_code": "EEE301", "title": "Signals", "unit": 3, "semester": "first"}],
            }
        )
        out = render_uniabuja_output("uniabuja_transcript", {"action": "student_lookup"}, raw)
        assert out is not None
        assert "Ada Okafor" in out
        assert "22/205EEE/172" in out
        assert "Signals" in out

    def test_not_renderable_falls_through(self) -> None:
        # plain text (real transcript output format), non-dict, bad envelope
        assert render_uniabuja_output("uniabuja_transcript", {"action": "student_lookup"}, "regno | course\n...") is None
        assert render_uniabuja_output("uniabuja_student", {"action": "status"}, "[1,2]") is None
        assert render_uniabuja_output("uniabuja_student", {"action": "status"}, '{"ok": false}') is None
        assert render_uniabuja_output("uniabuja_student", {"action": "submit_question"}, '{"ok": true}') is None

    def test_renderer_is_pure_and_total(self) -> None:
        # No input shape should ever raise out of the renderer.
        for junk in ("{", "{}", '{"ok": true, "rows": 5}', '{"ok": true, "student": 7}'):
            try:
                render_uniabuja_output("uniabuja_student", {"action": "query", "resource": "announcements"}, junk)
            except Exception as exc:  # pragma: no cover
                pytest.fail(f"renderer raised on {junk!r}: {exc}")


# ---------------------------------------------------------------------------
# 2. Cache + singleflight
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERX_TOOL_MIDDLEWARE_TTL_SCALE", "0.05")  # 120s -> 6s floor


class TestCacheAndSingleflight:
    async def test_cache_hit_skips_backend(self) -> None:
        tool = StubStudentTool()
        registry = _registry(tool)
        mw = ToolMiddleware()
        call = _call("uniabuja_student", {"action": "query", "resource": "announcements"})
        first = await mw.execute(registry, call)
        second = await mw.execute(registry, call)
        assert len(tool.calls) == 1
        assert first == second

    async def test_expiry_refetches(self) -> None:
        tool = StubStudentTool()
        registry = _registry(tool)
        mw = ToolMiddleware()
        call = _call("uniabuja_student", {"action": "status"})
        await mw.execute(registry, call)
        # Force expiry by rewriting the stored deadline.
        key = mw.cache_key(call)
        assert key is not None
        expires_at, result = mw._cache[key]
        mw._cache[key] = (time.monotonic() - 1, result)
        await mw.execute(registry, call)
        assert len(tool.calls) == 2

    async def test_volatile_reads_never_cached(self) -> None:
        tool = StubStudentTool(payload={"ok": True, "action": "query", "resource": "my_questions", "rows": []})
        registry = _registry(tool)
        mw = ToolMiddleware()
        call = _call("uniabuja_student", {"action": "query", "resource": "my_questions"})
        assert mw.cache_key(call) is None
        await mw.execute(registry, call)
        await mw.execute(registry, call)
        assert len(tool.calls) == 2

    async def test_transcript_never_cached(self) -> None:
        tool = StubTranscriptTool({"ok": True, "student": {"regno": "22/205EEE/172"}})
        registry = _registry(tool)
        mw = ToolMiddleware()
        call = _call("uniabuja_transcript", {"action": "student_lookup", "regno": "22/205EEE/172"})
        assert mw.cache_key(call) is None
        await mw.execute(registry, call)
        await mw.execute(registry, call)
        assert len(tool.calls) == 2

    async def test_error_results_not_cached(self) -> None:
        class ErrorTool(StubStudentTool):
            async def execute(self, **kwargs: Any) -> Any:
                self.calls.append(dict(kwargs))
                return ToolResult.error("not signed in")

        tool = ErrorTool()
        registry = _registry(tool)
        mw = ToolMiddleware()
        call = _call("uniabuja_student", {"action": "status"})
        r1 = await mw.execute(registry, call)
        r2 = await mw.execute(registry, call)
        assert len(tool.calls) == 2  # failure is retried against the backend
        assert "not signed in" in str(r1)

    async def test_singleflight_collapses_concurrent_duplicates(self) -> None:
        tool = StubStudentTool(delay=0.05)
        registry = _registry(tool)
        mw = ToolMiddleware()
        call = _call("uniabuja_student", {"action": "query", "resource": "announcements"})
        results = await asyncio.gather(*(mw.execute(registry, call) for _ in range(5)))
        assert len(tool.calls) == 1  # ONE backend hit for five simultaneous asks
        assert all(r == results[0] for r in results)

    async def test_leader_failure_does_not_poison_followers(self) -> None:
        class FlakyOnce(StubStudentTool):
            n = 0

            def __init__(self) -> None:
                super().__init__()  # bind self.calls onto the instance
                type(self).n = 0

            async def execute(self, **kwargs: Any) -> Any:
                self.calls.append(dict(kwargs))
                type(self).n += 1
                if type(self).n == 1:
                    return ToolResult.error("transient backend failure")
                return json.dumps({"ok": True, "action": "status", "access_level": "student"})

        tool = FlakyOnce()
        registry = _registry(tool)
        mw = ToolMiddleware()
        call = _call("uniabuja_student", {"action": "status"})
        first = await mw.execute(registry, call)
        assert "transient backend failure" in str(first)
        # The failed attempt left no inflight entry and nothing cached...
        assert mw._inflight == {}
        assert mw._cache == {}
        # ...so the retry reaches the backend and succeeds normally.
        second = await mw.execute(registry, call)
        assert "ok" in str(second)
        assert len(tool.calls) == 2

    def test_handles_scope(self) -> None:
        mw = ToolMiddleware()
        assert mw.handles(_call("uniabuja_student", {"action": "status"}))
        assert not mw.handles(_call("novita_sandbox", {"action": "run"}))
        assert not mw.handles(_call("uniabuja_student", "not-a-dict"))  # type: ignore[arg-type]

    def test_env_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POWERX_TOOL_MIDDLEWARE", "0")
        assert not middleware_enabled()
        mw = ToolMiddleware()
        assert not mw.handles(_call("uniabuja_student", {"action": "status"}))


# ---------------------------------------------------------------------------
# 3. Runner integration
# ---------------------------------------------------------------------------


def _spec(provider: CountingProvider, tools: ToolRegistry, text: str | None = None) -> AgentRunSpec:
    return make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": text or "hi"}],
        model="test-model",
        tools=tools,
        max_iterations=5,
        max_tool_result_chars=8_000,
        tool_middleware=True,
    )


class TestRunnerIntegration:
    async def test_in_loop_governed_call_ends_without_second_llm_call(self) -> None:
        """Model made ONE call to fetch data; the final render costs ZERO."""
        tool = StubStudentTool(
            payload={
                "ok": True,
                "action": "query",
                "resource": "announcements",
                "rows": [{"title": "Freshers orientation", "date": "2026-09-15"}],
            }
        )
        provider = CountingProvider(
            first_response=LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="t1", name="uniabuja_student", arguments={"action": "query", "resource": "announcements"})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        )
        runner = AgentRunner()
        result = await runner.run(_spec(provider, _registry(tool), "any news?"))
        assert provider.request_count == 1  # NO second round-trip
        assert result.stop_reason == "completed"
        assert "Freshers orientation" in (result.final_content or "")
        assert result.usage.get("middleware_formatted") == 1
        assert tool.calls == [{"action": "query", "resource": "announcements"}]

    async def test_unrenderable_output_keeps_model_in_loop(self) -> None:
        """A payload the renderer cannot confidently format still goes to the model."""

        class TextTool(StubStudentTool):
            async def execute(self, **kwargs: Any) -> Any:
                self.calls.append(dict(kwargs))
                return "raw tab-separated rows nobody can safely format"

        tool = TextTool()
        provider = CountingProvider(
            first_response=LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="t1", name="uniabuja_student", arguments={"action": "query", "resource": "announcements"})],
                finish_reason="tool_calls",
            )
        )
        runner = AgentRunner()
        result = await runner.run(_spec(provider, _registry(tool)))
        # The model was consulted a second time (provider returned its
        # sentinel error) — middleware correctly did NOT hijack synthesis.
        assert provider.request_count == 2
        assert result.stop_reason == "error"

    async def test_error_verdict_stays_in_loop(self) -> None:
        """Tool errors must not be formatted into finals; the model may recover."""

        class FailingTool(StubStudentTool):
            async def execute(self, **kwargs: Any) -> Any:
                self.calls.append(dict(kwargs))
                return ToolResult.error("UniAbuja student read access is disabled by the administrator")

        tool = FailingTool()
        provider = CountingProvider(
            first_response=LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="t1", name="uniabuja_student", arguments={"action": "status"})],
                finish_reason="tool_calls",
            )
        )
        runner = AgentRunner()
        result = await runner.run(_spec(provider, _registry(tool)))
        assert provider.request_count == 2  # model saw the error and got another turn
        assert result.stop_reason == "error"

    async def test_non_governed_tools_untouched(self) -> None:
        """Other tools (e.g. exec/shell) bypass the middleware entirely."""
        seen: list[Any] = []

        class OtherTool(StubStudentTool):
            @property
            def name(self) -> str:  # type: ignore[override]
                return "exec"

            @property
            def parameters(self) -> dict[str, Any]:
                return {"type": "object", "properties": {"command": {"type": "string"}}}

            async def execute(self, **kwargs: Any) -> Any:
                seen.append(kwargs)
                return "done"

        provider = CountingProvider(
            first_response=LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="t1", name="exec", arguments={"command": "ls"})],
                finish_reason="tool_calls",
            )
        )
        runner = AgentRunner()
        result = await runner.run(_spec(provider, _registry(OtherTool())))
        # Second call happened: middleware must not hijack other tools.
        assert provider.request_count == 2
        assert result.stop_reason == "error"
        assert seen

    async def test_router_answers_flow_through_cache(self) -> None:
        """Two back-to-back routed announcements asks: ONE backend hit total."""
        tool = StubStudentTool(
            payload={"ok": True, "action": "query", "resource": "announcements", "rows": [{"title": "Notice A"}]}
        )
        registry = _registry(tool)
        plan = deterministic_plan("any announcements?")
        assert plan is not None and plan.name == "uniabuja_student"

        runner = AgentRunner()
        spec = _spec(CountingProvider(), registry)
        spec.enable_deterministic_router = True
        spec.deterministic_router_text = "any announcements?"
        first = await runner.run(spec)
        second = await runner.run(spec)
        assert first.final_content == second.final_content
        assert len(tool.calls) == 1  # cache served the repeat, zero provider calls both times

    async def test_spec_flag_off_disables_layer(self) -> None:
        tool = StubStudentTool()
        registry = _registry(tool)
        provider = CountingProvider(
            first_response=LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="t1", name="uniabuja_student", arguments={"action": "status"})],
                finish_reason="tool_calls",
            )
        )
        spec = _spec(provider, registry)
        spec.tool_middleware = False
        runner = AgentRunner()
        result = await runner.run(spec)
        # Layer off: the model is consulted again exactly as before this change.
        assert provider.request_count == 2
        assert result.stop_reason == "error"
