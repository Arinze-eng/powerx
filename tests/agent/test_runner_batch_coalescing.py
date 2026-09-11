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


# --- Pre-emptive batching nudge (Addition 1) --------------------------------


def test_nudge_fires_on_first_lone_inspection_step() -> None:
    """The first lone workspace step whose result implies more work gets a hint."""
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {}
    spec.session_key = "test"
    # A directory listing with many entries => the model is about to walk it one
    # read per line (the expensive pattern). read_file was previously UNPOLICED.
    big_listing = "\n".join(f"file_{i}.py" for i in range(12))
    call = ToolCallRequest(id="1", name="list_dir", arguments={"path": "."})
    nudge = runner._maybe_inject_batching_nudge(spec, call, big_listing)
    assert nudge is not None
    assert "BATCHING TIP" in nudge
    assert "sandbox_batch" in nudge


def test_nudge_does_not_nag_a_short_one_shot_result() -> None:
    """A trivial single-step answer must NOT get an annoying batching tip."""
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {}
    spec.session_key = "test"
    call = ToolCallRequest(id="1", name="read_file", arguments={"path": "a"})
    assert runner._maybe_inject_batching_nudge(spec, call, "just one line") is None


def test_nudge_fires_only_once_per_task() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {}
    spec.session_key = "test"
    big = "\n".join(f"x{i}" for i in range(8))
    c1 = ToolCallRequest(id="1", name="exec", arguments={"command": "unzip x.zip"})
    c2 = ToolCallRequest(id="2", name="exec", arguments={"command": "ls"})
    assert runner._maybe_inject_batching_nudge(spec, c1, big) is not None
    # Second step must NOT nag again (guarded by state), even with a big result.
    assert runner._maybe_inject_batching_nudge(spec, c2, big) is None



def test_nudge_skipped_when_no_batch_tool_available() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch(has_batch=False)
    spec.batch_enforcement_state = {}
    spec.session_key = "test"
    call = ToolCallRequest(id="1", name="read_file", arguments={"path": "a"})
    assert runner._maybe_inject_batching_nudge(spec, call, "x") is None


def test_nudge_fires_when_only_run_plan_available() -> None:
    """The nudge must fire when run_plan is registered even if sandbox_batch is
    absent. run_plan is the cheaper/structured path for agentic file work, so the
    pre-emptive batching tip should still steer toward it. Fix: previously the
    nudge was hard-gated on sandbox_batch existing, so a harness/agent exposing
    only file tools + run_plan never got nudged and burned full round-trips."""
    runner = AgentRunner()
    # has() returns True ONLY for run_plan, False for everything else.
    tools = MagicMock()
    def _has(name: str) -> bool:
        return name == "run_plan"
    tools.has.side_effect = _has
    spec = SimpleNamespace(tools=tools)
    spec.batch_enforcement_state = {}
    spec.session_key = "test"
    big_listing = "\n".join(f"file_{i}.py" for i in range(12))
    call = ToolCallRequest(id="1", name="list_dir", arguments={"path": "."})
    nudge = runner._maybe_inject_batching_nudge(spec, call, big_listing)
    assert nudge is not None
    assert "run_plan" in nudge


def test_nudge_not_applied_to_a_good_batch_call() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {}
    spec.session_key = "test"
    call = ToolCallRequest(
        id="1", name="sandbox_batch", arguments={"operations": [{"action": "run"}]}
    )
    assert runner._maybe_inject_batching_nudge(spec, call, "done") is None


def test_nudge_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("POWERX_BATCH_NUDGE", "0")
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {}
    spec.session_key = "test"
    call = ToolCallRequest(id="1", name="read_file", arguments={"path": "a"})
    assert runner._maybe_inject_batching_nudge(spec, call, "x") is None


def test_nudge_covers_the_zip_walk_tools() -> None:
    """Every tool that made 'check zip' expensive must be on the nudge list."""
    from nanobot.agent.runner import AgentRunner

    for tool in ("novita_sandbox", "exec", "read_file", "list_dir", "grep", "find_files"):
        assert tool in AgentRunner._BATCH_NUDGE_TOOLS


# --- Runaway tree-walk guard (hard consolidation enforcement) -----------------


def test_runaway_guard_blocks_after_budget() -> None:
    """A long chain of lone inspection calls is eventually blocked into a plan/batch.

    Since the guard now enforces by default (no reliance on the soft 'nudged'
    flag), it trips once the lone-step budget (default 6) is exceeded.
    """
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {}
    spec.session_key = "test"
    # First several lone reads are allowed (legit inspect-then-decide)...
    for _ in range(6):
        call = ToolCallRequest(id="x", name="read_file", arguments={"path": "a"})
        assert runner._batch_enforcement_check(spec, call) is None
    # ...but past the budget it trips and forces consolidation.
    call = ToolCallRequest(id="x", name="read_file", arguments={"path": "a"})
    blocked = runner._batch_enforcement_check(spec, call)
    assert blocked is not None
    assert "BATCHING REQUIRED" in blocked[0]
    # With sandbox_batch AND run_plan both available, run_plan is preferred.
    assert "run_plan" in blocked[0]


def test_runaway_guard_blocks_without_prior_nudge() -> None:
    """The guard must trip even with no 'nudged' flag — hard enforcement.

    Previously the guard required the soft nudge to have fired first, so a model
    whose first results were small never got nudged and kept paying a full
    round-trip per lone step. Now the budget alone enforces consolidation.
    """
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {}  # no 'nudged' flag
    spec.session_key = "test"
    for _ in range(6):
        call = ToolCallRequest(id="x", name="read_file", arguments={"path": "a"})
        assert runner._batch_enforcement_check(spec, call) is None
    call = ToolCallRequest(id="x", name="read_file", arguments={"path": "a"})
    assert runner._batch_enforcement_check(spec, call) is not None


def test_runaway_guard_reset_by_a_batch_call() -> None:
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {"nudged": 1, "inspection_streak": 7}
    spec.session_key = "test"
    batch = ToolCallRequest(id="b", name="sandbox_batch", arguments={"operations": []})
    assert runner._batch_enforcement_check(spec, batch) is None
    assert spec.batch_enforcement_state["inspection_streak"] == 0


def test_runaway_guard_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("POWERX_MAX_LONE_INSPECTIONS", "0")
    runner = AgentRunner()
    spec = _spec_with_batch()
    spec.batch_enforcement_state = {"nudged": 1}
    spec.session_key = "test"
    for _ in range(50):
        call = ToolCallRequest(id="x", name="read_file", arguments={"path": "a"})
        assert runner._batch_enforcement_check(spec, call) is None


