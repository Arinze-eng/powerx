"""Regression tests for sandbox-call coalescing and batch enforcement."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from nanobot.agent.runner import AgentRunner
from nanobot.providers.base import ToolCallRequest


def _spec_with_batch(has_batch: bool = True) -> SimpleNamespace:
    tools = MagicMock()
    tools.has.return_value = has_batch
    return SimpleNamespace(tools=tools)


def test_consecutive_sandbox_calls_merge_into_one_batch() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch()
    calls = [
        ToolCallRequest(id="1", name="novita_sandbox", arguments={"action": "write", "path": "/workspace/a", "content": "x"}),
        ToolCallRequest(id="2", name="novita_sandbox", arguments={"action": "run", "command": "bash a"}),
        ToolCallRequest(id="3", name="novita_sandbox", arguments={"action": "read", "path": "/workspace/out"}),
    ]
    merged = runner._coalesce_sandbox_calls(spec, calls)
    assert len(merged) == 1
    assert merged[0].name == "sandbox_batch"
    ops = merged[0].arguments["operations"]
    assert [op["action"] for op in ops] == ["write", "run", "read"]


def test_member_without_action_stays_standalone_not_poisoning_batch() -> None:
    """A malformed member must not inject action="" into the merged batch."""
    runner = AgentRunner()
    spec = _spec_with_batch()
    calls = [
        ToolCallRequest(id="1", name="novita_sandbox", arguments={"action": "run", "command": "a"}),
        ToolCallRequest(id="2", name="novita_sandbox", arguments={}),  # no action
        ToolCallRequest(id="3", name="novita_sandbox", arguments={"action": "run", "command": "b"}),
    ]
    merged = runner._coalesce_sandbox_calls(spec, calls)
    batch_calls = [c for c in merged if c.name == "sandbox_batch"]
    standalone = [c for c in merged if c.name == "novita_sandbox"]
    assert len(batch_calls) == 1
    # The action-less member passes through as its own call instead of
    # poisoning the batch with an empty action.
    assert len(standalone) == 1
    assert all(str(op.get("action", "")).strip() for op in batch_calls[0].arguments["operations"])


def test_single_sandbox_call_is_not_wrapped() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch()
    calls = [ToolCallRequest(id="1", name="novita_sandbox", arguments={"action": "run", "command": "ls"})]
    merged = runner._coalesce_sandbox_calls(spec, calls)
    assert merged == calls


def test_coalescing_disabled_when_batch_tool_absent() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch(has_batch=False)
    calls = [
        ToolCallRequest(id="1", name="novita_sandbox", arguments={"action": "run", "command": "a"}),
        ToolCallRequest(id="2", name="novita_sandbox", arguments={"action": "run", "command": "b"}),
    ]
    merged = runner._coalesce_sandbox_calls(spec, calls)
    assert [c.name for c in merged] == ["novita_sandbox", "novita_sandbox"]


def test_non_sandbox_tools_pass_through_untouched() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch()
    calls = [
        ToolCallRequest(id="1", name="message", arguments={"content": "hi"}),
        ToolCallRequest(id="2", name="read_file", arguments={"path": "x"}),
    ]
    merged = runner._coalesce_sandbox_calls(spec, calls)
    assert merged == calls


def test_batch_enforcement_allows_read_without_penalising() -> None:
    """Lone reads must never be blocked — models inspect between steps."""
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {"single_run_streak": 99}
    spec.session_key = "test"
    read_call = ToolCallRequest(id="1", name="novita_sandbox", arguments={"action": "read", "path": "/workspace/x"})
    assert runner._batch_enforcement_check(spec, read_call) is None
    # streak unchanged by a read
    assert spec.batch_enforcement_state["single_run_streak"] == 99


def test_batch_enforcement_blocks_lone_run_and_batch_resets() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {"single_run_streak": 0}
    spec.session_key = "test"
    run_call = ToolCallRequest(id="1", name="novita_sandbox", arguments={"action": "run", "command": "ls"})
    blocked = runner._batch_enforcement_check(spec, run_call)
    assert blocked is not None
    batch_call = ToolCallRequest(id="2", name="sandbox_batch", arguments={"operations": []})
    assert runner._batch_enforcement_check(spec, batch_call) is None
    assert spec.batch_enforcement_state["single_run_streak"] == 0
