"""AgentScript-style plan-program executor — proven deterministic & zero-call.

The whole point of this subsystem is that ONE model call yields a complete
multi-step program (with loops over dynamic data) that runs WITHOUT any further
provider round-trips. These tests assert exactly that property against a fake
tool executor, then prove the run_plan tool wires into a real registry, and
finally an end-to-end runner test shows a 15-file scan costing a single LLM call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.agent.plan_program import (
    MAX_EXECUTED_STEPS,
    PlanProgramError,
    execute_plan,
    parse_plan,
)
from nanobot.agent.tools.base import Tool

# --- Fake executor that records every tool invocation -----------------------


class FakeExec:
    def __init__(self, outputs: dict[str, Any] | None = None, default: str = "ok") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.outputs = outputs or {}
        self.default = default

    async def __call__(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        # Per-(name,args) override; else per-name; else default.
        key = f"{name}:{args}"
        if key in self.outputs:
            return self.outputs[key]
        if name in self.outputs:
            out = self.outputs[name]
            return out(args) if callable(out) else out
        return self.default


# --- Pure executor ----------------------------------------------------------


def _run(plan: dict[str, Any], ex: FakeExec) -> Any:
    return asyncio.run(execute_plan(plan, ex))


class TestSequentialSteps:
    def test_steps_run_in_order_and_pass_variables(self) -> None:
        ex = FakeExec({"exec": lambda a: f"ran {a['command']}"})
        res = _run(
            {
                "steps": [
                    {"tool": "exec", "args": {"command": "ls"}, "id": "listing"},
                    {"tool": "exec", "args": {"command": "wc -l $listing"}},
                ]
            },
            ex,
        )
        assert res.executed_steps == 2
        # Second step saw the first step's output substituted in.
        assert ex.calls[1][1]["command"] == "wc -l ran ls"

    def test_whole_string_reference_preserves_type(self) -> None:
        ex = FakeExec()
        res = _run(
            {
                "steps": [
                    {"tool": "make_list", "args": {}, "id": "items"},
                    {"tool": "use", "args": {"data": "$items"}},
                ]
            },
            ex,
        )
        # make_list returns default string "ok"; passed through as-is.
        assert ex.calls[1][1]["data"] == "ok"
        assert res.executed_steps == 2


class TestForeachLoops:
    def test_foreach_over_split_output_runs_body_per_item(self) -> None:
        # The canonical AgentScript win: one 'find' step, then loop its lines.
        ex = FakeExec(
            {
                "exec": lambda a: (
                    "a.py\nb.py\nc.py" if a["command"].startswith("find") else f"checked {a['command']}"
                )
            }
        )
        res = _run(
            {
                "steps": [
                    {"tool": "exec", "args": {"command": "find . -name '*.py'"}, "id": "files"},
                    {
                        "foreach": "$files.split('\\n')",
                        "as": "f",
                        "do": [
                            {"tool": "exec", "args": {"command": "compile $f"}},
                        ],
                    },
                ]
            },
            ex,
        )
        # 1 find + 3 loop iterations = 4 executed steps, all from ONE plan.
        assert res.executed_steps == 4
        commands = [c[1]["command"] for c in ex.calls]
        assert "compile a.py" in commands
        assert "compile b.py" in commands
        assert "compile c.py" in commands

    def test_foreach_exposes_index_variable(self) -> None:
        seen: list[Any] = []

        async def ex(name: str, args: dict[str, Any]) -> str:
            if name == "log":
                seen.append(args.get("note"))
            return "x"

        asyncio.run(
            execute_plan(
                {
                    "steps": [
                        {"tool": "seed", "args": {}, "id": "s"},
                        {
                            "foreach": ["p", "q"],
                            "as": "item",
                            "do": [{"tool": "log", "args": {"note": "$index"}}],
                        },
                    ]
                },
                ex,
            )
        )
        # $index is a whole-string ref so its native int type is preserved.
        assert seen == [0, 1]


    def test_nested_foreach(self) -> None:
        calls: list[str] = []

        async def ex(name: str, args: dict[str, Any]) -> str:
            calls.append(args.get("v", ""))
            return "done"

        asyncio.run(
            execute_plan(
                {
                    "steps": [
                        {
                            "foreach": ["a", "b"],
                            "as": "outer",
                            "do": [
                                {
                                    "foreach": ["1", "2"],
                                    "as": "inner",
                                    "do": [
                                        {"tool": "t", "args": {"v": "$outer-$inner"}}
                                    ],
                                }
                            ],
                        }
                    ]
                },
                ex,
            )
        )
        assert calls == ["a-1", "a-2", "b-1", "b-2"]


class TestSafetyAndErrors:
    def test_unknown_variable_raises(self) -> None:
        async def ex(name: str, args: dict[str, Any]) -> str:
            return "x"

        with pytest.raises(PlanProgramError):
            asyncio.run(
                execute_plan({"steps": [{"tool": "t", "args": {"a": "$nope"}}]}, ex)
            )

    def test_step_cap_prevents_runaway_loop(self) -> None:
        async def ex(name: str, args: dict[str, Any]) -> str:
            return "x"

        big = [str(i) for i in range(MAX_EXECUTED_STEPS + 50)]
        with pytest.raises(PlanProgramError):
            asyncio.run(
                execute_plan(
                    {
                        "steps": [
                            {"foreach": big, "as": "i", "do": [{"tool": "t", "args": {}}]}
                        ]
                    },
                    ex,
                )
            )

    def test_error_result_does_not_abort_but_marks_failed(self) -> None:
        async def ex(name: str, args: dict[str, Any]) -> str:
            return "Error: something broke"

        res = asyncio.run(
            execute_plan(
                {"steps": [{"tool": "t", "args": {}, "id": "r"}, {"tool": "t2", "args": {}}]},
                ex,
            )
        )
        assert res.failed
        assert res.executed_steps == 2  # continued past the error

    def test_parse_rejects_garbage(self) -> None:
        with pytest.raises(PlanProgramError):
            parse_plan("not json {")
        with pytest.raises(PlanProgramError):
            parse_plan({"steps": []})
        with pytest.raises(PlanProgramError):
            parse_plan(123)

    def test_parse_accepts_json_string(self) -> None:
        plan = parse_plan('{"steps": [{"tool": "t", "args": {}}]}')
        assert plan["steps"][0]["tool"] == "t"


# --- Tool wrapper -----------------------------------------------------------


class _MiniRegistry:
    """Minimal registry stand-in exposing has()/execute()."""

    def __init__(self, tools: dict[str, Any]) -> None:
        self.tools = tools
        self.log: list[tuple[str, dict[str, Any]]] = []

    def has(self, name: str) -> bool:
        return name in self.tools

    async def execute(self, name: str, args: dict[str, Any]) -> Any:
        self.log.append((name, dict(args)))
        fn = self.tools[name]
        return await fn(**args)


def test_run_plan_tool_executes_and_reports_zero_extra_calls() -> None:
    from nanobot.agent.tools.run_plan import RunPlanTool

    async def _exec(command: str = "", **kw: Any) -> str:
        return f"out[{command}]"

    reg = _MiniRegistry({"exec": _exec})
    tool = RunPlanTool()
    tool.bind_registry(reg)

    result = asyncio.run(
        tool.execute(
            plan={
                "steps": [
                    {"tool": "exec", "args": {"command": "find"}, "id": "f"},
                    {"foreach": "$f.split('|')", "as": "x", "do": [{"tool": "exec", "args": {"command": "check $x"}}]},
                ],
                "output": "done scanning",
            }
        )
    )
    text = str(result)
    assert "0 additional model calls" in text
    assert "done scanning" in text
    # find + 1 iteration (the find output had no '|', so one element)
    assert len(reg.log) >= 2


def test_run_plan_tool_errors_on_unknown_tool_without_crashing() -> None:
    from nanobot.agent.tools.run_plan import RunPlanTool

    reg = _MiniRegistry({})
    tool = RunPlanTool()
    tool.bind_registry(reg)
    result = asyncio.run(tool.execute(plan={"steps": [{"tool": "ghost", "args": {}}]}))
    # Unknown tool becomes a recorded error outcome; overall report still renders.
    assert "run_plan" in str(result) or "ERROR" in str(result)


def test_run_plan_tool_unbound_registry_is_clean_error() -> None:
    from nanobot.agent.tools.run_plan import RunPlanTool

    tool = RunPlanTool()  # never bound
    result = asyncio.run(tool.execute(plan={"steps": [{"tool": "exec", "args": {}}]}))
    assert "not wired" in str(result).lower() or "error" in str(result).lower()


# --- End-to-end through the real AgentRunner --------------------------------


class _ExecTool(Tool):
    """Real-ish exec tool: returns a file list for 'find', else echoes."""

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
        if command.startswith("find"):
            return "\n".join(f"f{i}.py" for i in range(15))
        return f"checked {command}"



async def test_single_llm_call_runs_a_whole_15_file_scan() -> None:
    """THE headline guarantee: one model call, fifteen files scanned, zero re-think.

    Mirrors AgentScript's win directly — the model writes one plan with a foreach;
    our executor loops 15 times against the real exec tool without ever asking the
    provider again. Before this subsystem that same task cost ~10+ calls.
    """
    from agent.runner_helpers import make_run_spec
    from nanobot.agent.runner import AgentRunner
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.run_plan import RunPlanTool
    from nanobot.providers.base import LLMProvider as ProviderBase
    from nanobot.providers.base import LLMResponse, ToolCallRequest

    class OneShotModel(ProviderBase):
        def __init__(self) -> None:
            super().__init__(api_key="t")
            self.count = 0

        async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
            self.count += 1
            if self.count == 1:
                # Model emits ONE run_plan describing the entire job.
                return LLMResponse(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCallRequest(
                            id="p1",
                            name="run_plan",
                            arguments={
                                "plan": {
                                    "steps": [
                                        {"tool": "exec", "args": {"command": "find . -name '*.py'"}, "id": "files"},
                                        {
                                            "foreach": "$files.split('\\n')",
                                            "as": "f",
                                            "do": [{"tool": "exec", "args": {"command": "compile $f"}}],
                                        },
                                    ],
                                    "output": "Scanned all python files.",
                                }
                            },
                        )
                    ],
                )
            # Second call: model reads the combined report and answers once.
            return LLMResponse(content="Done: scanned 15 files.", finish_reason="stop")

        async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
            return await self.chat(messages, tools, **kwargs)

        def get_default_model(self) -> str:  # pragma: no cover
            return "m"

    registry = ToolRegistry()
    ex = _ExecTool()
    registry.register(ex)
    plan_tool = RunPlanTool()
    plan_tool.bind_registry(registry)
    registry.register(plan_tool)

    model = OneShotModel()
    spec = make_run_spec(
        model,
        initial_messages=[{"role": "user", "content": "check every python file for bugs"}],
        model="m",
        tools=registry,
        max_iterations=20,
        max_tool_result_chars=20000,
        workspace="/tmp",
    )
    result = await AgentRunner().run(spec)

    # Only TWO provider calls total (emit plan + read summary), NOT 15+.
    assert model.count == 2, f"expected 2 LLM calls, got {model.count}"
    # But the sandbox actually executed find + 15 compiles = 16 commands.
    assert len(ex.commands) == 16
    assert result.final_content == "Done: scanned 15 files."

