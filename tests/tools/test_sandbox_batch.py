"""Regression tests for nanobot.agent.tools.sandbox_batch.

These cover the production failure modes: hanging ops stalling the whole
batch, malformed payloads (operations as JSON string, numeric timeout, null
padding, bare-string ops) being rejected instead of normalised, silent output
truncation hiding which operations actually ran, and stop_on_error semantics.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.sandbox_batch import (
    _MAX_RESULT_CHARS_PER_OP,
    _MAX_TOTAL_RESULT_CHARS,
    SandboxBatchTool,
)


class FakeSandbox:
    """Stand-in for NovitaSandboxTool with scripted per-action behaviour."""

    def __init__(self, handler=None):
        self.calls: list[dict] = []
        self._handler = handler or (lambda kwargs: f"out:{kwargs.get('action')}")

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        result = self._handler(kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result


def _tool_with(handler=None) -> tuple[SandboxBatchTool, FakeSandbox]:
    tool = SandboxBatchTool()
    fake = FakeSandbox(handler)
    tool._sandbox = fake
    return tool, fake


@pytest.mark.asyncio
async def test_happy_path_runs_ops_in_order() -> None:
    tool, fake = _tool_with()
    report = await tool.execute(
        operations=[
            {"action": "write", "path": "/workspace/a.py", "content": "x=1"},
            {"action": "run", "command": "python a.py"},
        ]
    )
    assert [c["action"] for c in fake.calls] == ["write", "run"]
    assert "[sandbox_batch: 2 operation(s), 0 failure(s)]" in report
    assert "[op 0 write → ok]" in report
    assert "[op 1 run → ok]" in report


@pytest.mark.asyncio
async def test_operations_as_json_string_is_normalised() -> None:
    tool, fake = _tool_with()
    report = await tool.execute(
        operations='[{"action": "run", "command": "echo hi"}]'
    )
    assert len(fake.calls) == 1
    assert fake.calls[0]["command"] == "echo hi"
    assert "failure" not in report.split("\n")[0].replace("0 failure", "")


@pytest.mark.asyncio
async def test_numeric_timeout_cast_to_int_and_passed_through() -> None:
    tool, fake = _tool_with()
    await tool.execute(operations=[{"action": "run", "command": "sleep 1", "timeout": 45}])
    assert fake.calls[0]["timeout"] == 45


@pytest.mark.asyncio
async def test_string_timeout_normalized() -> None:
    tool, fake = _tool_with()
    await tool.execute(operations=[{"action": "run", "command": "x", "timeout": "90"}])
    assert fake.calls[0]["timeout"] == 90


@pytest.mark.asyncio
async def test_garbage_timeout_dropped_not_fatal() -> None:
    tool, fake = _tool_with()
    await tool.execute(operations=[{"action": "run", "command": "x", "timeout": "soon"}])
    assert "timeout" not in fake.calls[0]


@pytest.mark.asyncio
async def test_null_padded_fields_are_stripped() -> None:
    tool, fake = _tool_with()
    await tool.execute(
        operations=[{"action": "run", "command": "ls", "source": None, "url": None}]
    )
    assert "source" not in fake.calls[0]
    assert "url" not in fake.calls[0]


@pytest.mark.asyncio
async def test_bare_string_op_becomes_run() -> None:
    tool, fake = _tool_with()
    await tool.execute(operations=["ls -la"])
    assert fake.calls[0] == {"action": "run", "command": "ls -la"}


@pytest.mark.asyncio
async def test_missing_action_inferred_from_command() -> None:
    tool, fake = _tool_with()
    await tool.execute(operations=[{"command": "echo hi"}])
    assert fake.calls[0]["action"] == "run"


@pytest.mark.asyncio
async def test_stop_on_error_halts_and_reports_skipped() -> None:
    def handler(kwargs):
        if kwargs["action"] == "run":
            return ToolResult.error("boom")
        return "fine"

    tool, fake = _tool_with(handler)
    report = await tool.execute(
        operations=[
            {"action": "write", "path": "/workspace/a", "content": "x"},
            {"action": "run", "command": "false"},
            {"action": "read", "path": "/workspace/a"},
        ],
        stop_on_error=True,
    )
    assert len(fake.calls) == 2  # third op never executed
    assert "halted early on error" in report
    assert "1 op(s) not executed" in report
    assert "1 failure" in report


@pytest.mark.asyncio
async def test_continue_after_error_when_stop_on_error_false() -> None:
    def handler(kwargs):
        if kwargs["action"] == "run":
            return ToolResult.error("boom")
        return "fine"

    tool, fake = _tool_with(handler)
    report = await tool.execute(
        operations=[
            {"action": "run", "command": "false"},
            {"action": "read", "path": "/workspace/a"},
        ],
        stop_on_error=False,
    )
    assert len(fake.calls) == 2
    assert "[op 0 run → ERR]" in report
    assert "[op 1 read → ok]" in report


@pytest.mark.asyncio
async def test_string_false_stop_on_error_is_respected() -> None:
    def handler(kwargs):
        return ToolResult.error("boom")

    tool, fake = _tool_with(handler)
    report = await tool.execute(
        operations=[
            {"action": "run", "command": "false"},
            {"action": "run", "command": "echo"},
        ],
        stop_on_error="false",
    )
    assert len(fake.calls) == 2
    assert "halted" not in report


@pytest.mark.asyncio
async def test_exception_in_one_op_does_not_crash_batch() -> None:
    def handler(kwargs):
        if kwargs.get("command") == "explode":
            raise RuntimeError("kaboom")
        return "ok-out"

    tool, fake = _tool_with(handler)
    report = await tool.execute(
        operations=[
            {"action": "run", "command": "explode"},
            {"action": "run", "command": "safe"},
        ],
        stop_on_error=False,
    )
    assert "RuntimeError: kaboom" in report
    assert "ok-out" in report


@pytest.mark.asyncio
async def test_hanging_operation_hits_wall_clock_guard() -> None:
    started = asyncio.Event()

    async def hang(**kwargs):
        started.set()
        await asyncio.sleep(3600)  # simulates a wedged transport

    tool, _ = _tool_with()

    async def slow_handler(kwargs):
        started.set()
        await asyncio.sleep(3600)

    tool._sandbox = FakeSandbox(slow_handler)
    # Shrink the guard so the test stays fast: declared timeout 1s + grace.
    import nanobot.agent.tools.sandbox_batch as module

    original_grace = module._OP_GUARD_GRACE_SECONDS
    module._OP_GUARD_GRACE_SECONDS = 1
    try:
        report = await tool.execute(
            operations=[
                {"action": "run", "command": "never-returns", "timeout": 1},
                {"action": "run", "command": "after"},
            ],
            stop_on_error=True,
        )
    finally:
        module._OP_GUARD_GRACE_SECONDS = original_grace
    assert "timed out" in report
    assert "halted early on error" in report


@pytest.mark.asyncio
async def test_per_op_result_truncation_is_marked() -> None:
    big = "z" * (_MAX_RESULT_CHARS_PER_OP + 5_000)
    tool, _ = _tool_with(lambda kwargs: big)
    report = await tool.execute(operations=[{"action": "run", "command": "cat big.log"}])
    assert "truncated" in report
    assert f"{len(big)} chars" in report
    assert "grep/sed/head/tail" in report


@pytest.mark.asyncio
async def test_total_budget_keeps_status_for_every_op() -> None:
    # Each op returns ~5k chars; five ops exceed the 24k total budget.
    chunk = "u" * (_MAX_RESULT_CHARS_PER_OP - 100)
    tool, fake = _tool_with(lambda kwargs: chunk)
    ops = [{"action": "run", "command": f"step{i}"} for i in range(6)]
    report = await tool.execute(operations=ops, stop_on_error=False)
    # All six operations must still have RUN (side effects matter)...
    assert len(fake.calls) == 6
    # ...and every op must appear with an explicit status marker.
    for i in range(6):
        assert f"[op {i} run" in report, f"op {i} vanished from report"
    assert "budget" in report


@pytest.mark.asyncio
async def test_empty_operations_rejected_cleanly() -> None:
    tool, _ = _tool_with()
    result = await tool.execute(operations=[])
    assert isinstance(result, ToolResult)
    assert result.is_error


@pytest.mark.asyncio
async def test_too_many_operations_rejected() -> None:
    tool, fake = _tool_with()
    result = await tool.execute(
        operations=[{"action": "run", "command": "x"}] * 41
    )
    assert isinstance(result, ToolResult)
    assert result.is_error
    assert "max is 40" in str(result)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_operations_wrapped_one_level_deep() -> None:
    tool, fake = _tool_with()
    await tool.execute(operations={"operations": [{"action": "run", "command": "echo"}]})
    assert len(fake.calls) == 1
