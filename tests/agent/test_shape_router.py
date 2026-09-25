"""Lever-A proofs: shape routing (fewer DECISIONS) and the burn-loop guard.

Two independent claims are asserted here, and they are deliberately kept apart
from Lever B (parallel tool batching inside one response, already implemented at
``runner.py`` ``_execute_tools``). Nothing in this file may be satisfied by
batching: every assertion below is about the NUMBER OF PROVIDER CALLS.

1. **Shape routing** — a batch/loop-shaped ask is steered onto the one-call
   ``run_plan`` path by a deterministic, zero-cost classification, and an
   exploratory or conversational ask is left completely alone (same path, same
   cost, no injected message). The steering hint rides on the request view only
   and must never reach the persisted transcript.

2. **Burn-loop guard** — a ``finish_reason="length"`` response that re-emits a
   blank or byte-identical truncated prefix cannot be replayed forever. This is
   the documented production bug from ``tools/run_plan.py``: the runner would
   replay turn after turn with 0 tool commands executed and every call burned.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.runner import AgentRunner
from nanobot.agent.shape_router import (
    prefer_library_workflow,
    steer_message_for,
    classify_task_shape,
    plan_preference_message,
    shape_router_enabled,
    should_steer_to_plan,
)
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider as ProviderBase
from nanobot.providers.base import LLMResponse


# --- Layer 1: pure shape classification -------------------------------------


class TestClassifyMultiStep:
    """Loop-shaped, counted-set and chained asks classify as multi-step."""

    @pytest.mark.parametrize(
        "text",
        [
            "for each of these files, add a license header",
            "for every module in the package, add a type hint",
            "rename all the .txt files to .md",
            "process each of the 40 records and write them to a csv",
            "read the 25 modules and extract every docstring",
            "these 30 tests need a timeout decorator",
            "read the csv, clean the rows and then write a summary report",
            "scan all python files for bugs",
            "loop over the rows and normalise the dates",
        ],
    )
    def test_multi_step_shapes(self, text: str) -> None:
        assert classify_task_shape(text) == "multi_step", text


class TestClassifyLibraryWork:
    """Deliverable/library work is multi-step even with no explicit loop.

    This is the production case: "create a powerpoint, 16 slides, green" walked
    the probe/install spiral one provider call at a time (pandoc missing ->
    install attempt -> pdflatex missing -> install attempt -> ...) and still had
    not written the artefact. The shape is really
    probe -> provision -> generate, so it must be steered as multi-step.
    """

    @pytest.mark.parametrize(
        "text",
        [
            # The exact production prompt from the screenshot.
            "Create a power point presentation on how to create a ai agent "
            "16slides ,green color",
            "create a 16 slide pptx about AI agents in green",
            "build a 20 page pdf report with charts",
            "generate an excel spreadsheet with 100 rows",
            "make a slide deck about our roadmap",
            "generate 10 images of cats",
            "convert this folder of markdown into a pdf",
        ],
    )
    def test_library_shapes_are_multi_step(self, text: str) -> None:
        assert classify_task_shape(text) == "multi_step", text

    def test_library_tasks_get_the_provisioning_hint(self) -> None:
        """A deliverable ask must get the provisioning hint, not the generic
        plan hint — the failure mode is a probe/install spiral."""
        message = steer_message_for(
            "Create a power point presentation on how to create a ai agent "
            "16slides ,green color"
        )
        content = message["content"]
        assert "library/deliverable" in content
        # Must forbid one-probe-per-call and one-install-per-call.
        assert "FIRST call" in content
        assert "ONE install" in content
        # Must point at a library that needs no system binary at all.
        assert "python-pptx" in content
        # Must bound the waste explicitly.
        assert "2 calls on provisioning" in content

    @pytest.mark.parametrize(
        "text",
        [
            "read the pdf config",
            "show me the report",
            "explain what pptx is",
            "what does pandoc do?",
            "list the spreadsheets in the workspace",
        ],
    )
    def test_reads_and_questions_about_formats_are_not_steered(
        self, text: str
    ) -> None:
        """Asking *about* a format is not a request to build one."""
        assert not prefer_library_workflow(text), text

    def test_images_batch_gets_library_hint(self) -> None:
        message = steer_message_for("generate 10 images of cats")
        assert "library/deliverable" in message["content"]


class TestClassifyExploratory:
    """Exploratory / adaptive asks stay on Re-Act — this is its home turf.

    The plan path commits the whole graph before any observation. Forcing it on
    a turn whose next step depends on the previous result would turn graceful
    partial progress into total failure, so these MUST NOT be steered.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "check on what you just found",
            "why did that test fail?",
            "explain what that error means",
            "take a look at what you found and tell me more",
            "dig deeper into the root cause",
            "how does the caching layer work?",
            "what do you think we should do here?",
            "hello",
            "thanks!",
            "investigate why the build broke",
        ],
    )
    def test_exploratory_shapes(self, text: str) -> None:
        assert classify_task_shape(text) != "multi_step", text


class TestClassifySingleAction:
    """Single-action and ambiguous asks are never steered."""

    @pytest.mark.parametrize(
        "text",
        [
            "read the config file",
            "show me the readme",
            "run the tests",
            "find the main entry point",
            "2 files",
            "add a docstring to the helper",
        ],
    )
    def test_single_action_shapes(self, text: str) -> None:
        assert classify_task_shape(text) == "single", text

    def test_overlong_and_empty_text_never_steered(self) -> None:
        assert classify_task_shape("for each of these files " + ("pad " * 200)) == "single"
        assert classify_task_shape("") == "single"
        assert classify_task_shape(None) == "single"


class TestSteerPreconditions:
    """The gate: no tool, no steering. And the operator switch is honoured."""

    def test_steers_when_a_tool_is_available(self) -> None:
        assert should_steer_to_plan(
            "for each of these files, add a header",
            plan_tool_available=True,
            code_tool_available=True,
        )

    def test_no_steer_when_neither_tool_registered(self) -> None:
        # GATE CHECK (Phase 2): if the sandbox gate is closed, neither run_plan
        # nor python_code is registered, so nothing may be steered.
        assert not should_steer_to_plan(
            "for each of these files, add a header",
            plan_tool_available=False,
            code_tool_available=False,
        )

    def test_env_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POWERX_SHAPE_ROUTER", "0")
        assert shape_router_enabled() is False
        assert not should_steer_to_plan(
            "for each of these files, add a header",
            plan_tool_available=True,
            code_tool_available=True,
        )

    def test_hint_is_short_and_keeps_the_escape_hatch(self) -> None:
        message = plan_preference_message()
        assert message["role"] == "user"
        # Cheap: paid on every call of a steered turn.
        assert len(message["content"]) < 1_600
        assert "run_plan" in message["content"]
        # The Re-Act escape hatch must survive, or hard tasks regress.
        assert "escape" in message["content"].lower()


# --- Layer 2: end-to-end through the real AgentRunner ------------------------


class _ExecTool(Tool):
    """Minimal stand-in so ``run_plan`` has a sibling to invoke."""

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "execute a shell command"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def execute(self, command: str = "", **kw: Any) -> str:
        self.commands.append(command)
        return "ok"


class _RecordingProvider(ProviderBase):
    """Records every request it receives, then answers once and stops."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        self.seen_messages.append([dict(m) for m in messages])
        return LLMResponse(content="done", finish_reason="stop")

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "shape-test"


def _registry_with_plan() -> ToolRegistry:
    from nanobot.agent.tools.run_plan import RunPlanTool

    registry = ToolRegistry()
    registry.register(_ExecTool())
    plan_tool = RunPlanTool()
    plan_tool.bind_registry(registry)
    registry.register(plan_tool)
    return registry


def _steer_texts(provider: _RecordingProvider) -> list[str]:
    """Every steering hint string the provider actually saw, across requests."""
    found: list[str] = []
    for request in provider.seen_messages:
        for message in request:
            content = message.get("content")
            if isinstance(content, str) and "Routing hint" in content:
                found.append(content)
    return found


class TestRunnerSteering:
    """The hint reaches the model on multi-step turns and nowhere else."""

    async def test_multi_step_turn_receives_the_hint(self) -> None:
        provider = _RecordingProvider()
        result = await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[
                    {
                        "role": "user",
                        "content": "for each of these files, add a license header",
                    }
                ],
                model="shape-test",
                tools=_registry_with_plan(),
                max_iterations=4,
                max_tool_result_chars=8_000,
                enable_deterministic_router=True,
                deterministic_router_text="for each of these files, add a license header",
            )
        )
        hints = _steer_texts(provider)
        assert hints, "the multi-step turn was not steered onto the plan path"
        assert "run_plan" in hints[0]
        # The hint must NOT pollute the persisted transcript — only the request
        # view. If it leaked, history would accumulate one copy per turn.
        assert not any(
            isinstance(m.get("content"), str) and "Routing hint" in m["content"]
            for m in result.messages
        )

    async def test_exploratory_turn_is_untouched(self) -> None:
        """Same registry, same tooling — only the SHAPE differs. No hint, and
        the turn still costs exactly one call, identical to today."""
        provider = _RecordingProvider()
        await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[
                    {"role": "user", "content": "check on what you just found"}
                ],
                model="shape-test",
                tools=_registry_with_plan(),
                max_iterations=4,
                max_tool_result_chars=8_000,
                enable_deterministic_router=True,
                deterministic_router_text="check on what you just found",
            )
        )
        assert _steer_texts(provider) == []
        assert provider.calls == 1

    async def test_no_steering_when_plan_tool_absent(self) -> None:
        """Closed gate: a batch ask with no plan tool must cost what it did
        before — no hint pointing at a tool that does not exist."""
        registry = ToolRegistry()
        registry.register(_ExecTool())
        provider = _RecordingProvider()
        await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[
                    {"role": "user", "content": "for each of these files, add a header"}
                ],
                model="shape-test",
                tools=registry,
                max_iterations=4,
                max_tool_result_chars=8_000,
                enable_deterministic_router=True,
                deterministic_router_text="for each of these files, add a header",
            )
        )
        assert _steer_texts(provider) == []

    async def test_steering_never_changes_the_call_count(self) -> None:
        """The hint is guidance, not a decision: it adds no provider call.

        The saving it enables is the model collapsing N steps into 1 plan — the
        hint itself must be free. (This is the Lever-B separation: nothing here
        is batching.)
        """
        provider = _RecordingProvider()
        result = await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[
                    {"role": "user", "content": "rename all the .txt files to .md"}
                ],
                model="shape-test",
                tools=_registry_with_plan(),
                max_iterations=4,
                max_tool_result_chars=8_000,
                enable_deterministic_router=True,
                deterministic_router_text="rename all the .txt files to .md",
            )
        )
        assert provider.calls == 1
        assert result.usage["llm_calls"] == 1


# --- Layer 2b: static-prefix stability (Phase 5) -----------------------------


class TestStaticPrefixStability:
    """The steering hint must not break prompt-cache prefix matching.

    Caching (automatic prefix caching, and the explicit ``cache_control``
    breakpoints applied by the providers) pays off only while the request
    prefix is byte-stable. The hint is therefore appended LAST, as a trailing
    message, so the leading system prompt + tool schemas — the expensive,
    reusable part — are byte-identical to an un-steered turn.
    """

    async def test_hint_is_appended_tail_only(self) -> None:
        provider = _RecordingProvider()
        await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[
                    {"role": "system", "content": "SYSTEM PREFIX"},
                    {"role": "user", "content": "for each of these files, add a header"},
                ],
                model="shape-test",
                tools=_registry_with_plan(),
                max_iterations=4,
                max_tool_result_chars=8_000,
                enable_deterministic_router=True,
                deterministic_router_text="for each of these files, add a header",
            )
        )
        request = provider.seen_messages[0]
        # The static prefix keeps its position and content byte-for-byte.
        assert request[0]["role"] == "system"
        assert request[0]["content"] == "SYSTEM PREFIX"
        assert request[1]["role"] == "user"
        # The hint is the LAST message — nothing cached precedes a change.
        assert request[-1]["role"] == "user"
        assert "Routing hint" in request[-1]["content"]

    def test_hint_text_is_static(self) -> None:
        """Byte-identical across calls, so it can itself be cached."""
        assert plan_preference_message() == plan_preference_message()

    async def test_unsteered_turn_prefix_is_untouched(self) -> None:
        """Control: with no hint, the request is exactly the input messages."""

        class _PrefixProvider(_RecordingProvider):
            pass

        provider = _PrefixProvider()
        await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[
                    {"role": "system", "content": "SYSTEM PREFIX"},
                    {"role": "user", "content": "hello there"},
                ],
                model="shape-test",
                tools=_registry_with_plan(),
                max_iterations=4,
                max_tool_result_chars=8_000,
                enable_deterministic_router=True,
                deterministic_router_text="hello there",
            )
        )
        request = provider.seen_messages[0]
        assert [m["role"] for m in request] == ["system", "user"]
        assert request[1]["content"] == "hello there"


# --- Layer 3: the burn-loop guard -------------------------------------------


class _TruncatingProvider(ProviderBase):
    """Always answers ``finish_reason="length"`` with the SAME truncated text.

    This is the pathological provider the guard exists for: the runner would
    otherwise keep appending a recovery prompt and paying a new call per replay.
    """

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        return LLMResponse(
            content="x" * 4_000,
            finish_reason="length",
        )

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "length-test"


class _BlankTruncatingProvider(ProviderBase):
    """Truncates with no usable content at all — nothing to recover."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        return LLMResponse(content="", finish_reason="length")

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "length-test"


class TestBurnLoopGuard:
    """A length-terminated replay can never burn calls without progress."""

    async def test_identical_truncated_replay_does_not_loop(self) -> None:
        provider = _TruncatingProvider()
        result = await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[{"role": "user", "content": "do a long thing"}],
                model="length-test",
                tools=ToolRegistry(),
                max_iterations=50,
                max_tool_result_chars=8_000,
            )
        )
        # Before the guard this ran to max_iterations (50 paid calls producing
        # the same text). The identical-prefix replay is refused after the first
        # recovery, so the cost is bounded and tiny.
        assert provider.calls <= 3, f"burn loop: {provider.calls} provider calls"
        assert result.usage["llm_calls"] == provider.calls
        # The work that WAS produced is still returned — never a failed turn.
        assert result.final_content
        assert "x" * 100 in result.final_content

    async def test_blank_truncated_replay_does_not_loop(self) -> None:
        provider = _BlankTruncatingProvider()
        result = await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[{"role": "user", "content": "do a long thing"}],
                model="length-test",
                tools=ToolRegistry(),
                max_iterations=50,
                max_tool_result_chars=8_000,
            )
        )
        # A blank segment can never advance the answer, so replaying it is pure
        # waste: exactly one call, then finish.
        assert provider.calls == 1, f"burn loop: {provider.calls} provider calls"
        assert result.usage["llm_calls"] == 1

    async def test_genuine_progress_still_recovers(self) -> None:
        """The guard must not break REAL length recovery.

        A provider that keeps making progress across truncations still gets its
        bounded continuation attempts — only the zero-progress replay is cut.
        """

        class _ProgressingProvider(ProviderBase):
            def __init__(self) -> None:
                super().__init__(api_key="test")
                self.calls = 0

            async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
                self.calls += 1
                # Distinct, growing content each time -> real progress.
                return LLMResponse(
                    content=("segment-%d " % self.calls) * 200,
                    finish_reason="length"
                    if self.calls < 3
                    else "stop",
                )

            async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
                return await self.chat(messages, tools, **kwargs)

            def get_default_model(self) -> str:
                return "length-test"

        provider = _ProgressingProvider()
        result = await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[{"role": "user", "content": "do a long thing"}],
                model="length-test",
                tools=ToolRegistry(),
                max_iterations=20,
                max_tool_result_chars=8_000,
            )
        )
        # Two length segments then a clean stop: recovery worked as designed.
        assert provider.calls == 3
        assert "segment-1" in result.final_content
        assert "segment-3" in result.final_content


    async def test_a_short_but_novel_segment_is_not_a_burn_loop(self) -> None:
        """The guard refuses replays that add nothing, not replays that are brief.

        An earlier version also refused any segment under 16 characters. That is
        not a burn loop: a short segment is ordinary early truncation, and
        refusing it ENDED the turn, discarding the rest of the answer and any
        tool call the model was about to make. The replay budget already bounds
        the cost of a model that only ever emits a little at a time.
        """

        class _ShortButProgressingProvider(ProviderBase):
            def __init__(self) -> None:
                super().__init__(api_key="test")
                self.calls = 0

            async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(content="working", finish_reason="length")
                if self.calls == 2:
                    return LLMResponse(content="harder", finish_reason="length")
                return LLMResponse(content="done", finish_reason="stop")

            async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
                return await self.chat(messages, tools, **kwargs)

            def get_default_model(self) -> str:
                return "length-test"

        provider = _ShortButProgressingProvider()
        result = await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[{"role": "user", "content": "keep going"}],
                model="length-test",
                tools=ToolRegistry(),
                max_iterations=20,
                max_tool_result_chars=8_000,
            )
        )

        assert provider.calls == 3, "a short segment must be continued, not refused"
        assert result.stop_reason == "completed"
        # Every segment is kept: "working harder done", whitespace-normalised by
        # the runner's per-segment bookkeeping.
        assert "working" in result.final_content
        assert "harder" in result.final_content
        assert "done" in result.final_content
        assert result.usage["llm_calls"] == 3

    async def test_a_short_segment_still_cannot_be_replayed_forever(self) -> None:
        """Brevity is allowed; repeating yourself is not. The budget is the limit."""

        class _ShortAndRepetitiveProvider(ProviderBase):
            def __init__(self) -> None:
                super().__init__(api_key="test")
                self.calls = 0

            async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
                self.calls += 1
                return LLMResponse(content="ab", finish_reason="length")

            async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
                return await self.chat(messages, tools, **kwargs)

            def get_default_model(self) -> str:
                return "length-test"

        provider = _ShortAndRepetitiveProvider()
        result = await AgentRunner().run(
            make_run_spec(
                provider,
                initial_messages=[{"role": "user", "content": "do a long thing"}],
                model="length-test",
                tools=ToolRegistry(),
                max_iterations=50,
                max_tool_result_chars=8_000,
            )
        )

        # "ab" is novel once, then byte-identical on every replay, so the guard
        # stops it after two calls rather than running to max_iterations.
        assert provider.calls <= 3, f"burn loop: {provider.calls} provider calls"
        assert result.final_content
