"""Tests for ``python_code`` — smolagents-style local AST interpreter.

The whole point of this subsystem is that ONE model call yields a complete
Python program (with loops, branches, data munging, and tool calls) that runs
WITHOUT any further provider round-trips. These tests assert exactly that:

* pure computation (a 10k-iteration loop) costs ZERO tool/LLM calls;
* tool bridges (read_file/exec/...) run inside the program with no extra LLM
  round-trips;
* security: dunder escape + disallowed imports are blocked;
* end-to-end through the real AgentRunner: a multi-step task costs ONE call.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.python_code import (
    PythonCodeError,
    PythonCodeTool,
    run_python_code,
)


# --------------------------------------------------------------------------- #
# Unit: the sandboxed evaluator                                                #
# --------------------------------------------------------------------------- #

class _CountingTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        if name == "read_file":
            return "alpha\nbeta\ngamma"
        if name == "exec":
            return f"ran:{args['command']}"
        if name in ("list_files", "list_dir"):  # bridge maps list_files -> list_dir
            return "a.txt\nb.txt\nc.txt"
        return "ok"


@pytest.mark.asyncio
async def test_pure_loop_costs_zero_calls() -> None:
    """A 10,000-iteration loop runs locally with NO tool/LLM round-trip."""
    tools = _CountingTools()
    result = await run_python_code(
        "total = 0\nfor i in range(10000):\n    total += i\nfinal_answer(total)",
        tool_call=tools,
    )
    assert result == sum(range(10000))
    assert len(tools.calls) == 0  # <-- the cost-saving property


@pytest.mark.asyncio
async def test_last_expression_returned() -> None:
    tools = _CountingTools()
    result = await run_python_code("a = 5\nb = a * 2\nb", tool_call=tools)
    assert result == 10


@pytest.mark.asyncio
async def test_function_def_and_comprehension() -> None:
    tools = _CountingTools()
    code = "def sq(x):\n    return x * x\nfinal_answer([sq(i) for i in range(5)])"
    result = await run_python_code(code, tool_call=tools)
    assert result == [0, 1, 4, 9, 16]


@pytest.mark.asyncio
async def test_f_strings_work() -> None:
    """Models use f-strings constantly; JoinedStr must be supported."""
    tools = _CountingTools()
    code = 'name="world"\nfinal_answer(f"hello {name} {1+2}")'
    result = await run_python_code(code, tool_call=tools)
    assert result == "hello world 3"


@pytest.mark.asyncio
async def test_f_string_with_repr_and_loop() -> None:
    tools = _CountingTools()
    code = (
        "out=[]\n"
        "for i in range(3):\n"
        "    out.append(f'item-{i!r}')\n"
        "final_answer(out)"
    )
    result = await run_python_code(code, tool_call=tools)
    assert result == ["item-0", "item-1", "item-2"]


@pytest.mark.asyncio
async def test_tool_bridge_inside_loop() -> None:
    """read_file called per-item inside a loop runs locally (no LLM)."""
    tools = _CountingTools()
    code = (
        "out = []\n"
        "for p in ['a.txt', 'b.txt']:\n"
        "    c = read_file(p)\n"
        "    out.append(c.count('\\n'))\n"
        "final_answer(out)"
    )
    result = await run_python_code(code, tool_call=tools)
    assert result == [2, 2]
    assert len(tools.calls) == 2  # two LOCAL tool executions, zero LLM calls


@pytest.mark.asyncio
async def test_exec_bridge() -> None:
    tools = _CountingTools()
    result = await run_python_code("r = exec('ls -la')\nfinal_answer(r)", tool_call=tools)
    assert result == "ran:ls -la"
    assert tools.calls[0][0] == "exec"


@pytest.mark.asyncio
async def test_list_files_then_read_each() -> None:
    """Scan files then read each — all in ONE program, no per-file LLM call."""
    tools = _CountingTools()
    code = (
        "files = list_files('.').split('\\n')\n"
        "lines = 0\n"
        "for f in files:\n"
        "    lines += len(read_file(f).split('\\n'))\n"
        "final_answer(lines)"
    )
    result = await run_python_code(code, tool_call=tools)
    # 3 files * 3 lines each
    assert result == 9
    assert len(tools.calls) == 4  # 1 list + 3 reads, ZERO LLM round-trips


# --------------------------------------------------------------------------- #
# Security                                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_blocks_dunder_attribute_access() -> None:
    tools = _CountingTools()
    with pytest.raises(PythonCodeError):
        await run_python_code("x = (1).__class__", tool_call=tools)


@pytest.mark.asyncio
async def test_blocks_disallowed_import() -> None:
    tools = _CountingTools()
    with pytest.raises(PythonCodeError):
        await run_python_code("import os", tool_call=tools)


@pytest.mark.asyncio
async def test_blocks_open_builtin() -> None:
    tools = _CountingTools()
    with pytest.raises(PythonCodeError):
        await run_python_code("f = open('/etc/passwd')", tool_call=tools)


@pytest.mark.asyncio
async def test_allows_safe_math_import() -> None:
    tools = _CountingTools()
    result = await run_python_code("import math\nfinal_answer(math.sqrt(16))", tool_call=tools)
    assert result == 4.0


@pytest.mark.asyncio
async def test_timeout_guard() -> None:
    tools = _CountingTools()
    # deadline already passed -> must raise quickly
    with pytest.raises(PythonCodeError):
        await run_python_code(
            "while True:\n    pass",
            tool_call=tools,
            timeout_seconds=-1.0,
        )


@pytest.mark.asyncio
async def test_unknown_name_error() -> None:
    tools = _CountingTools()
    with pytest.raises(PythonCodeError):
        await run_python_code("final_answer(undefined_var)", tool_call=tools)


# --------------------------------------------------------------------------- #
# The Tool wrapper                                                             #
# --------------------------------------------------------------------------- #

def _fake_registry_with(tool_results: dict[str, str]) -> SimpleNamespace:
    reg = MagicMock()
    reg.has.side_effect = lambda n: n in tool_results or n in {"read_file", "exec"}
    async def _execute(name: str, params: dict) -> str:
        return tool_results.get(name, "ok")
    reg.execute.side_effect = _execute
    return reg


@pytest.mark.asyncio
async def test_tool_requires_registry() -> None:
    tool = PythonCodeTool()
    result = await tool.execute(code="final_answer(1)")
    assert "not wired" in str(result)


@pytest.mark.asyncio
async def test_tool_runs_locally_returns_one_result() -> None:
    tool = PythonCodeTool()
    tool.bind_registry(_fake_registry_with({"read_file": "hello world"}))
    result = await tool.execute(code="c = read_file('x')\nfinal_answer(len(c))")
    assert "0 additional model calls" in str(result)
    assert "11" in str(result)


@pytest.mark.asyncio
async def test_tool_rejects_empty_code() -> None:
    tool = PythonCodeTool()
    tool.bind_registry(_fake_registry_with({}))
    result = await tool.execute(code="   ")
    assert "non-empty" in str(result)


@pytest.mark.asyncio
async def test_tool_reports_syntax_error_cleanly() -> None:
    tool = PythonCodeTool()
    tool.bind_registry(_fake_registry_with({}))
    result = await tool.execute(code="def broken(:\n    pass")
    assert "Syntax error" in str(result)


@pytest.mark.asyncio
async def test_tool_reports_runtime_error_cleanly() -> None:
    tool = PythonCodeTool()
    tool.bind_registry(_fake_registry_with({}))
    result = await tool.execute(code="import os")
    assert "error" in str(result).lower()


# --------------------------------------------------------------------------- #
# End-to-end through the real AgentRunner: cost proof                          #
# --------------------------------------------------------------------------- #

from nanobot.agent.runner import AgentRunner  # noqa: E402
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest  # noqa: E402
from agent.runner_helpers import make_run_spec  # noqa: E402


class CountingProvider(LLMProvider):
    """Scripted provider that records every request it receives.

    The runner's non-streaming path calls ``chat_with_retry`` → ``_safe_chat`` →
    ``chat``, so :meth:`chat` is the real entry point we instrument.
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


class FakeFileBackend:
    """Stands in for read_file / exec so we can prove they run locally."""

    def __init__(self) -> None:
        self.reads = 0

    async def execute(self, path: str | None = None, command: str | None = None, **_: object) -> str:
        if path is not None:
            self.reads += 1
            return "line-a\nline-b\nline-c"
        return "ran"


def _pytool(registry) -> PythonCodeTool:
    t = PythonCodeTool()
    t.bind_registry(registry)
    return t


def _registry(*tools: object) -> object:
    from nanobot.agent.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)  # type: ignore[arg-type]
    return reg


def _read_file_tool(backend: FakeFileBackend) -> object:
    from nanobot.agent.tools.base import Tool

    class _RF(Tool):
        @property
        def name(self):
            return "read_file"

        @property
        def description(self):
            return "read"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"path": {"type": "string"}}}

        @property
        def read_only(self):
            return True

        async def execute(self, **kw):
            return await backend.execute(path=kw.get("path"))

    return _RF()


def _exec_tool(backend: FakeFileBackend) -> object:
    from nanobot.agent.tools.base import Tool

    class _EX(Tool):
        @property
        def name(self):
            return "exec"

        @property
        def description(self):
            return "run"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"command": {"type": "string"}}}

        @property
        def read_only(self):
            return False

        async def execute(self, **kw):
            return await backend.execute(command=kw.get("command"))

    return _EX()


_FINAL = LLMResponse(content="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_python_code_collapses_multi_step_task_to_one_llm_call() -> None:
    """A program that scans+reads many files in a loop = ONE provider call.

    This is the headline cost claim: N file operations executed locally inside
    one python_code call cost ZERO extra AI round-trips. Only the single call
    that emitted the program is billed.
    """
    backend = FakeFileBackend()
    py = _pytool(_registry(_read_file_tool(backend), _exec_tool(backend)))
    registry = _registry(py)

    program = (
        "files = ['a','b','c','d','e','f','g','h']\n"
        "total = 0\n"
        "for f in files:\n"
        "    total += len(read_file(f).split('\\n'))\n"
        "final_answer(total)"
    )
    call = ToolCallRequest(id="p1", name="python_code", arguments={"code": program})
    provider = CountingProvider(
        [LLMResponse(content=None, tool_calls=[call], finish_reason="tool_calls"), _FINAL]
    )
    runner = AgentRunner()
    spec = make_run_spec(
        provider,
        model="counting-test",
        initial_messages=[{"role": "user", "content": "scan files"}],
        tools=registry,
        max_iterations=5,
        max_tool_result_chars=8_000,
    )
    result = await runner.run(spec)
    assert result.stop_reason == "completed"
    # 8 file reads happened LOCALLY...
    assert backend.reads == 8
    # ...but only 2 LLM round-trips were billed (the program call + final stop).
    assert provider.request_count == 2
