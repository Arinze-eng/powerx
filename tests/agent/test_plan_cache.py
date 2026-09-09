"""Zero-call TASK PLAN cache, proven live.

The layer that makes "the sandbox handles a user's task without calling the
LLM every time" real: solve a coding/task once, then replay its recorded tool
steps directly on any structurally-identical repeat — the model is only paid
again when something is genuinely new or a replay hits an exception (Manus's
2126-commands / 22-calls shape).

Coverage:
* normalize_task: variable stripping + template stability across name/number
  changes; refuses to key tiny/volatile prompts.
* safety gate: side-effecting steps are never stored or replayed.
* substitute_variables: positional fill-in, longest-first so substrings don't
  clobber.
* PlanCache: put/get/expiry/cap.
* Runner integration: first run LEARNS a plan (model called), second same-shape
  run REPLAYS it with ZERO provider calls; error/missing-tool/unsafe cases fall
  back to the model instead of answering wrong.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from nanobot.agent.plan_cache import (
    PlanCache,
    StoredPlan,
    normalize_task,
    plan_is_safe,
    replayable_for,
    substitute_variables,
)
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider as ProviderBase, LLMResponse, ToolCallRequest

from agent.runner_helpers import make_run_spec


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalizeTask:
    def test_strips_names_and_numbers_into_one_template(self) -> None:
        a = normalize_task("build a python script that prints hello world")
        b = normalize_task("build a python script that prints goodbye now")
        assert a is not None and b is not None
        # Same instruction shape -> same template (the varying words stay in the
        # template because they aren't variables; but numbers/names do collapse).
        c = normalize_task("sum these 3 numbers please")
        d = normalize_task("sum these 9 numbers please")
        assert c is not None and d is not None
        assert c.template == d.template
        assert c.variables == ["3"] and d.variables == ["9"]

    def test_refuses_tiny_prompts(self) -> None:
        assert normalize_task("hi") is None
        assert normalize_task("ok thanks") is None
        assert normalize_task("") is None

    def test_captures_regno_date_file_number_name(self) -> None:
        norm = normalize_task(
            "download transcript 22/205EEE/172 for Ada Okafor dated 2026-09-01 into report.pdf"
        )
        assert norm is not None
        joined = " ".join(norm.variables)
        assert "22/205EEE/172" in joined
        assert "Ada Okafor" in joined
        assert "2026-09-01" in joined
        assert "report.pdf" in joined
        # Template has none of the literals left in it.
        assert "22/205EEE/172" not in norm.template
        assert "<id>" in norm.template or "<name>" in norm.template

    def test_case_insensitive_template(self) -> None:
        a = normalize_task("Count the files in folder 5")
        b = normalize_task("count the FILES in Folder 7")
        assert a is not None and b is not None
        assert a.template == b.template


# ---------------------------------------------------------------------------
# Safety gate
# ---------------------------------------------------------------------------


class TestSafetyGate:
    def test_coding_steps_are_safe(self) -> None:
        steps = [
            {"name": "novita_sandbox", "arguments": {"action": "run", "command": "ls"}},
            {"name": "sandbox_batch", "arguments": {"operations": [{"action": "run", "command": "echo hi"}]}},
            {"name": "exec", "arguments": {"command": "pwd"}},
        ]
        assert plan_is_safe(steps)

    def test_unsafe_tool_rejected(self) -> None:
        assert not plan_is_safe([{"name": "message", "arguments": {"to": "x"}}])
        assert not plan_is_safe([{"name": "supabase_write", "arguments": {}}])

    def test_unsafe_sandbox_action_rejected(self) -> None:
        assert not plan_is_safe(
            [{"name": "novita_sandbox", "arguments": {"action": "upload", "path": "/x"}}]
        )
        assert not plan_is_safe(
            [{"name": "novita_sandbox", "arguments": {"action": "download_url", "url": "http://x"}}]
        )

    def test_mutating_student_action_rejected(self) -> None:
        assert not plan_is_safe(
            [{"name": "uniabuja_student", "arguments": {"action": "submit_question"}}]
        )

    def test_empty_and_oversized_rejected(self) -> None:
        assert not plan_is_safe([])
        assert not plan_is_safe(
            [{"name": "exec", "arguments": {"command": f"c{i}"}} for i in range(100)]
        )

    def test_non_dict_step_rejected(self) -> None:
        assert not plan_is_safe(["not-a-dict"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Substitution + match
# ---------------------------------------------------------------------------


class TestSubstitution:
    def test_positional_fill_in(self) -> None:
        steps = [{"name": "exec", "arguments": {"command": "greet Ada Okafor"}}]
        out = substitute_variables(steps, ["Ada Okafor"], ["Emeka Obi"])
        assert out[0]["arguments"]["command"] == "greet Emeka Obi"

    def test_longest_first_no_clobber(self) -> None:
        # "cat" is a substring of "concatenate"; replacing shortest-first would
        # corrupt the longer token.
        steps = [{"name": "exec", "arguments": {"command": "concatenate cat.txt"}}]
        out = substitute_variables(steps, ["concatenate", "cat.txt"], ["merge", "dog.txt"])
        assert out[0]["arguments"]["command"] == "merge dog.txt"

    def test_nested_structures_walked(self) -> None:
        steps = [
            {
                "name": "sandbox_batch",
                "arguments": {
                    "operations": [
                        {"action": "write", "path": "/tmp/Ada.txt"},
                        {"action": "run", "command": "cat /tmp/Ada.txt"},
                    ]
                },
            }
        ]
        out = substitute_variables(steps, ["Ada"], ["Zainab"])
        ops = out[0]["arguments"]["operations"]
        assert ops[0]["path"] == "/tmp/Zainab.txt"
        assert ops[1]["command"] == "cat /tmp/Zainab.txt"

    def test_replayable_requires_same_template_and_var_count(self) -> None:
        plan_norm = normalize_task("add 3 and 5 together")
        assert plan_norm is not None
        plan = StoredPlan(template=plan_norm.template, variables=plan_norm.variables,
                          steps=[{"name": "exec", "arguments": {"c": "x"}}], saved_at=time.time())
        same_shape = normalize_task("add 7 and 9 together")
        diff_shape = normalize_task("multiply 7 and 9 together")
        assert same_shape is not None and diff_shape is not None
        assert replayable_for(same_shape, plan)
        assert not replayable_for(diff_shape, plan)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestPlanCacheStore:
    def test_put_get_roundtrip(self, tmp_path) -> None:
        cache = PlanCache(tmp_path)
        norm = normalize_task("compile the rust project please")
        assert norm is not None
        steps = [{"name": "exec", "arguments": {"command": "cargo build"}}]
        assert cache.put(norm, steps) is not None
        got = cache.get(norm)
        assert got is not None
        assert got.steps == steps

    def test_expiry(self, tmp_path) -> None:
        cache = PlanCache(tmp_path)
        norm = normalize_task("run the unit tests now")
        assert norm is not None
        cache.put(norm, [{"name": "exec", "arguments": {"command": "pytest"}}])
        # Force expiry by rewriting saved_at far into the past.
        path = cache._path(norm.template)
        data = json.loads(path.read_text())
        data["saved_at"] = time.time() - 999999
        path.write_text(json.dumps(data))
        assert cache.get(norm) is None

    def test_unsafe_plan_not_stored(self, tmp_path) -> None:
        cache = PlanCache(tmp_path)
        norm = normalize_task("send an email to the team")
        assert norm is not None
        assert cache.put(norm, [{"name": "message", "arguments": {"to": "boss"}}]) is None


# ---------------------------------------------------------------------------
# Runner integration: learn once, replay with ZERO calls
# ---------------------------------------------------------------------------


class SandboxStub(Tool):
    """Stands in for novita_sandbox; records executions."""

    def __init__(self, output: str = "build ok") -> None:
        self.calls: list[dict[str, Any]] = []
        self.output = output

    @property
    def name(self) -> str:
        return "novita_sandbox"

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"action": {"type": "string"}, "command": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self.output


class CountingProvider(ProviderBase):
    """Returns scripted responses per call; counts how often it's consulted."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self.request_count = 0
        self.responses = responses

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        idx = min(self.request_count, len(self.responses) - 1)
        self.request_count += 1
        return self.responses[idx]

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:  # pragma: no cover
        return "test-model"


def _learn_response(command: str) -> LLMResponse:
    """Model asks to run one sandbox command, then answers."""
    return LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="t1", name="novita_sandbox", arguments={"action": "run", "command": command})],
        finish_reason="tool_calls",
    )


def _final_response(text: str) -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop")


class TestRunnerPlanReplay:
    async def test_learn_then_replay_zero_calls(self, tmp_path) -> None:
        sandbox = SandboxStub(output="compiled successfully")
        registry = ToolRegistry()
        registry.register(sandbox)

        # First task: model learns a plan (2 provider calls: tool turn + final).
        provider = CountingProvider([_learn_response("make build"), _final_response("Done: compiled")])
        runner = AgentRunner()
        spec = make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "please compile the project in folder 7 now"}],
            model="test-model",
            tools=registry,
            max_iterations=5,
            max_tool_result_chars=8000,
            workspace=str(tmp_path),
            enable_plan_cache=True,
        )
        first = await runner.run(spec)
        assert provider.request_count == 2  # model was needed to learn
        assert first.usage.get("plan_replayed") is None
        assert len(sandbox.calls) == 1

        # Second SAME-SHAPE task (only the folder NUMBER changes): zero LLM calls.
        provider2 = CountingProvider([_final_response("should not be called")])
        spec2 = make_run_spec(
            provider2,
            initial_messages=[{"role": "user", "content": "please compile the project in folder 9 now"}],
            model="test-model",
            tools=registry,
            max_iterations=5,
            max_tool_result_chars=8000,
            workspace=str(tmp_path),
            enable_plan_cache=True,
        )
        second = await runner.run(spec2)
        assert provider2.request_count == 0  # NO LLM call at all
        assert second.usage.get("plan_replayed") == 1
        assert second.final_content == "compiled successfully"  # last step's output
        assert len(sandbox.calls) == 2  # replay re-ran the sandbox step

    async def test_replay_substitutes_variable_command(self, tmp_path) -> None:
        """A number/name in the task flows through into the replayed step args."""
        sandbox = SandboxStub(output="wrote 42 rows")
        registry = ToolRegistry()
        registry.register(sandbox)
        provider = CountingProvider(
            [_learn_response("process file 7 rows"), _final_response("processed")]
        )
        runner = AgentRunner()
        spec = make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "process data file 7 rows please"}],
            model="test-model",
            tools=registry,
            max_iterations=5,
            max_tool_result_chars=8000,
            workspace=str(tmp_path),
            enable_plan_cache=True,
        )
        await runner.run(spec)
        # The learned command embedded the literal '7'. A new task says '9'.
        provider2 = CountingProvider([_final_response("nope")])
        spec2 = make_run_spec(
            provider2,
            initial_messages=[{"role": "user", "content": "process data file 9 rows please"}],
            model="test-model",
            tools=registry,
            max_iterations=5,
            max_tool_result_chars=8000,
            workspace=str(tmp_path),
            enable_plan_cache=True,
        )
        result = await runner.run(spec2)
        assert provider2.request_count == 0
        assert result.usage.get("plan_replayed") == 1
        # Replay swapped 7 -> 9 inside the recorded command argument.
        assert sandbox.calls[-1]["command"] == "process file 9 rows"

    async def test_replay_error_falls_back_to_model(self, tmp_path) -> None:
        """If a cached step now errors, the model takes over (no wrong answer)."""

        class FlakySandbox(SandboxStub):
            def __init__(self) -> None:
                super().__init__()
                self.n = 0

            async def execute(self, **kwargs: Any) -> Any:
                self.calls.append(dict(kwargs))
                self.n += 1
                if self.n == 1:
                    return "build ok"  # learning run succeeds
                return ToolResult.error("sandbox unavailable")  # replay fails

        sandbox = FlakySandbox()
        registry = ToolRegistry()
        registry.register(sandbox)
        provider = CountingProvider([_learn_response("go build ./..."), _final_response("built")])
        runner = AgentRunner()
        spec = make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "build the golang app please"}],
            model="test-model",
            tools=registry,
            max_iterations=5,
            max_tool_result_chars=8000,
            workspace=str(tmp_path),
            enable_plan_cache=True,
        )
        await runner.run(spec)
        # Replay attempt fails -> falls through to the model, which answers.
        provider2 = CountingProvider(
            [_final_response("model handled it after replay failed")]
        )
        spec2 = make_run_spec(
            provider2,
            initial_messages=[{"role": "user", "content": "build the golang app please"}],
            model="test-model",
            tools=registry,
            max_iterations=5,
            max_tool_result_chars=8000,
            workspace=str(tmp_path),
            enable_plan_cache=True,
        )
        result = await runner.run(spec2)
        assert provider2.request_count >= 1  # model WAS consulted again
        assert result.usage.get("plan_replayed") is None

    async def test_flag_off_disables_layer(self, tmp_path) -> None:
        sandbox = SandboxStub()
        registry = ToolRegistry()
        registry.register(sandbox)
        provider = CountingProvider([_learn_response("make"), _final_response("done")])
        runner = AgentRunner()
        spec = make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "run the make build target"}],
            model="test-model",
            tools=registry,
            max_iterations=5,
            max_tool_result_chars=8000,
            workspace=str(tmp_path),
            enable_plan_cache=False,
        )
        await runner.run(spec)
        # No plan was stored, so a second identical run consults the model again.
        provider2 = CountingProvider([_learn_response("make"), _final_response("done")])
        spec2 = make_run_spec(
            provider2,
            initial_messages=[{"role": "user", "content": "run the make build target"}],
            model="test-model",
            tools=registry,
            max_iterations=5,
            max_tool_result_chars=8000,
            workspace=str(tmp_path),
            enable_plan_cache=False,
        )
        await runner.run(spec2)
        assert provider2.request_count == 2  # behaved exactly as before this change
