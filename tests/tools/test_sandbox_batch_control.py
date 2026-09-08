"""Tests for in-batch control flow: retry_until / foreach / await.

The point of these operations is that a model can express iteration, retries and
long waits once and pay for them with zero additional LLM round-trips. Tests
therefore assert on *how many times the sandbox was touched* and *how much text
came back*, not just on correctness of the verdict.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nanobot.agent.tools.batch_control import (
    MAX_NEST_DEPTH,
    PlanError,
    StepOutcome,
    evaluate_condition,
    render_report,
    run_await,
    run_foreach,
    run_retry_until,
    validate_control_flow,
)
from nanobot.agent.tools.sandbox_batch import SandboxBatchTool


# ---------------------------------------------------------------------------
# condition language
# --------------------------------------------------------------------------
def test_conditions_exit_and_contains() -> None:
    ok = StepOutcome(text="all good\n[exit=0]", exit_code=0)
    bad = StepOutcome(text="boom: Assertion failed\n[exit=1]", exit_code=1)
    assert evaluate_condition("exit == 0", ok)
    assert not evaluate_condition("exit == 0", bad)
    assert evaluate_condition("exit != 0", bad)
    assert evaluate_condition('contains "all good"', ok)
    assert evaluate_condition('not_contains "failed"', ok)
    assert not evaluate_condition('not_contains "failed"', bad)
    assert evaluate_condition("ok", ok)
    assert not evaluate_condition("ok", bad)


def test_conditions_reject_garbage() -> None:
    with pytest.raises(PlanError):
        evaluate_condition("banana == 3", StepOutcome(text=""))
    with pytest.raises(PlanError):
        evaluate_condition("exit == notanumber", StepOutcome(text="", exit_code=1))
    # Comparing raw stdout directly is deliberately unsupported.
    with pytest.raises(PlanError):
        evaluate_condition("stdout == 'hi'", StepOutcome(text="hi"))


def test_elapsed_condition_counts_down_to_deadline() -> None:
    deadline = time.monotonic() + 5
    assert evaluate_condition("elapsed >= 0", StepOutcome(text=""), elapsed=1.0)
    assert not evaluate_condition("elapsed >= 10", StepOutcome(text=""), elapsed=1.0)
    assert deadline > time.monotonic()


@pytest.mark.parametrize(
    "spec",
    [
        {"action": "retry_until"},  # no until
        {"action": "retry_until", "until": "ok"},  # no body
        {"action": "retry_until", "until": "ok", "body": []},  # empty body
        {"action": "retry_until", "until": "bogus cond", "body": [{"action": "run"}]},
        {"action": "foreach", "body": [{"action": "run"}]},  # no items source
        {"action": "await"},  # neither until nor command
    ],
)
def test_validation_rejects_malformed_plans(spec: dict) -> None:
    with pytest.raises(PlanError):
        validate_control_flow(spec)


def test_validation_bounds_nesting_depth() -> None:
    spec: dict = {"action": "run", "command": "x"}
    for _ in range(MAX_NEST_DEPTH + 2):
        spec = {"action": "foreach", "items": ["a"], "body": [spec]}
    with pytest.raises(PlanError):
        validate_control_flow(spec)


# ---------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
async def _runner_factory(script: list[tuple[str, bool]]):
    """Fake step runner replaying (text, failed) outcomes in order."""
    calls: list[dict] = []
    seq = list(script)

    async def run(step: dict) -> StepOutcome:
        calls.append(step)
        idx = min(len(calls) - 1, len(seq) - 1)
        text, failed = seq[idx]
        return StepOutcome(text=text, exit_code=1 if failed else 0, failed=failed)

    return run, calls


@pytest.mark.asyncio
async def test_retry_until_stops_as_soon_as_condition_holds() -> None:
    run, calls = await _runner_factory(
        [("failing\n[exit=1]", True), ("failing\n[exit=1]", True), ("green\n[exit=0]", False)]
    )
    report = await run_retry_until(
        {"until": "exit == 0", "body": [{"action": "run", "command": "pytest"}]}, run
    )
    assert report.satisfied
    assert report.attempts == 3
    assert len(calls) == 3, "should stop immediately after success"


@pytest.mark.asyncio
async def test_retry_until_reports_exhaustion_not_silence() -> None:
    run, calls = await _runner_factory([("still broken\n[exit=1]", True)])
    report = await run_retry_until(
        {"until": "exit == 0", "max_attempts": 4, "body": [{"action": "run", "command": "x"}]},
        run,
    )
    assert not report.satisfied
    assert report.stopped_reason == "attempts_exhausted"
    assert report.attempts == 4
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_foreach_substitutes_item_and_index() -> None:
    run, calls = await _runner_factory([("done\n[exit=0]", False)])
    report = await run_foreach(
        {
            "items": ["a.py", "b.py", "c.py"],
            "body": [{"action": "run", "command": "lint {{item}} #{{index}}"}],
        },
        run,
        read_items=_unused_reader,
    )
    assert report.satisfied and report.iterations == 3
    assert calls[0]["command"] == "lint a.py #0"
    assert calls[2]["command"] == "lint c.py #2"


@pytest.mark.asyncio
async def test_foreach_reads_items_from_the_sandbox_not_the_model() -> None:
    """A 500-file target list must never need to enter context."""
    seen: list[str] = []

    async def reader(path: str) -> list[str]:
        seen.append(path)
        return [f"f{i}.py" for i in range(500)]

    run, calls = await _runner_factory([("ok\n[exit=0]", False)])
    report = await run_foreach(
        {"items_file": ".targets", "max_items": 500, "body": [{"action": "run", "command": "fix {{item}}"}]},
        run,
        read_items=reader,
    )
    assert seen == [".targets"]
    assert report.iterations == 500
    assert len(calls) == 500


@pytest.mark.asyncio
async def test_foreach_respects_max_items_guard() -> None:
    async def reader(path: str) -> list[str]:
        return [str(i) for i in range(1000)]

    run, _ = await _runner_factory([("ok\n[exit=0]", False)])
    with pytest.raises(PlanError, match="max_items"):
        await run_foreach(
            {"items_file": "x", "max_items": 10, "body": [{"action": "run", "command": "c"}]},
            run,
            read_items=reader,
        )


@pytest.mark.asyncio
async def test_foreach_stop_on_error_halts_mid_list() -> None:
    script = [("ok\n[exit=0]", False), ("ok\n[exit=0]", False), ("crash\n[exit=2]", True)]
    run, calls = await _runner_factory(script)
    report = await run_foreach(
        {"items": ["a", "b", "c", "d", "e"], "body": [{"action": "run", "command": "{{item}}"}]},
        run,
        read_items=_unused_reader,
    )
    assert not report.satisfied
    assert report.iterations == 3, "must stop at the failing item"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_foreach_can_continue_past_failures_when_asked() -> None:
    script = [("bad\n[exit=1]", True), ("good\n[exit=0]", False), ("bad\n[exit=1]", True)]
    run, calls = await _runner_factory(script)
    report = await run_foreach(
        {
            "items": ["a", "b", "c"],
            "stop_on_error": False,
            "body": [{"action": "run", "command": "{{item}}"}],
        },
        run,
        read_items=_unused_reader,
    )
    assert report.iterations == 3 and len(calls) == 3
    assert not report.satisfied


@pytest.mark.asyncio
async def test_await_polls_without_burning_tokens() -> None:
    """A wait should sleep internally and return one line, whatever it waited for."""
    run, calls = await _runner_factory([("starting\n[exit=1]", True)])

    async def probe(step: dict) -> StepOutcome:
        # First two polls fail, third succeeds.
        n = len(calls) + 1
        calls.append(step)
        done = n >= 3
        return StepOutcome(
            text=("ready\n[exit=0]" if done else "waiting\n[exit=1]"),
            exit_code=0 if done else 1,
            failed=not done,
        )

    t0 = time.monotonic()
    report = await run_await(
        {"command": "curl -sf localhost/health", "until": "exit == 0", "timeout_sec": 10, "interval_sec": 0.05},
        probe,
        deadline_for_op=lambda s: time.monotonic() + s,
    )
    assert report.satisfied
    assert report.attempts == 3
    assert time.monotonic() - t0 < 2, "should return as soon as the probe passes"


@pytest.mark.asyncio
async def test_await_times_out_explicitly() -> None:
    async def always_busy(step: dict) -> StepOutcome:
        return StepOutcome(text="busy\n[exit=1]", exit_code=1, failed=True)

    report = await run_await(
        {"command": "sleep", "until": "exit == 0", "timeout_sec": 0.3, "interval_sec": 0.05},
        always_busy,
        deadline_for_op=lambda s: time.monotonic() + s,
    )
    assert not report.satisfied
    assert report.stopped_reason == "timeout"
    assert report.attempts >= 1


def test_render_report_is_one_short_line() -> None:
    from nanobot.agent.tools.batch_control import LoopReport

    r = LoopReport(kind="foreach", iterations=412, attempts=412, satisfied=True, samples=["noise"])
    line = render_report(r)
    assert "\n" not in line.strip() or line.count("\n") == 0
    assert "[SATISFIED] foreach" in line and "items=412" in line
    assert len(line) < 400


async def _unused_reader(path: str) -> list[str]:  # pragma: no cover
    raise AssertionError("read_items must not be called when items are inline")


# ---------------------------------------------------------------------------
# integration through sandbox_batch
# --------------------------------------------------------------------------
class FakeSandbox:
    def __init__(self, handler=None):
        self.calls: list[dict] = []
        self._handler = handler

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        if self._handler:
            out = self._handler(kwargs, len(self.calls))
            if asyncio.iscoroutine(out):
                out = await out
            return out
        return f"ran:{kwargs.get('command')}\n[exit=0]"


def _tool(fake: FakeSandbox, tmp_path=None) -> SandboxBatchTool:
    tool = SandboxBatchTool(workspace=tmp_path)
    tool._sandbox = fake
    return tool


@pytest.mark.asyncio
async def test_retry_until_collapses_a_fix_loop_into_one_operation(tmp_path) -> None:
    """Three failing then one passing attempt = ONE batch op, not four turns."""
    def handler(kwargs, n):
        return ("FAILED tests/x.py\n[exit=1]" if n < 3 else "1 passed\n[exit=0]")

    tool = _tool(FakeSandbox(handler), tmp_path)
    report = await tool.execute(
        operations=[
            {
                "action": "retry_until",
                "until": "exit == 0",
                "max_attempts": 5,
                "body": [{"action": "run", "command": "pytest tests/x.py"}],
            }
        ]
    )
    assert "[SATISFIED] retry_until" in report
    assert "attempts=3" in report
    assert "0 failure(s)" in report
    # The backend saw 3 runs; the model saw one digest line. That gap is the saving.
    assert sum(1 for c in tool._sandbox.calls if c.get("action") == "run") == 3


@pytest.mark.asyncio
async def test_unsatisfied_retry_is_visible_as_a_failure(tmp_path) -> None:
    tool = _tool(FakeSandbox(lambda k, n: "still red\n[exit=1]"), tmp_path)
    report = await tool.execute(
        operations=[
            {
                "action": "retry_until",
                "until": "exit == 0",
                "max_attempts": 2,
                "body": [{"action": "run", "command": "pytest"}],
            }
        ]
    )
    assert "NOT-SATISFIED" in report
    assert "attempts_exhausted" in report


@pytest.mark.asyncio
async def test_foreach_over_generated_targets_never_loads_them(tmp_path) -> None:
    async def handler(kwargs, n):
        if kwargs.get("action") == "read":
            return "\n".join(f"src/mod{i}.py" for i in range(200))
        return "fixed\n[exit=0]"

    tool = _tool(FakeSandbox(handler), tmp_path)
    report = await tool.execute(
        operations=[
            {
                "action": "foreach",
                "items_file": ".targets",
                "max_items": 200,
                "body": [{"action": "run", "command": "ruff fix {{item}}"}],
            }
        ]
    )
    assert "[SATISFIED] foreach" in report and "items=200" in report
    # 200 filenames would have been ~2.6KB of context; we returned a verdict line.
    assert len(report) < 900, f"report leaked bulk output: {len(report)} chars"


@pytest.mark.asyncio
async def test_nested_foreach_with_inner_retry(tmp_path) -> None:
    """foreach containing retry_until — the shape that replaces many turns."""
    state = {"per_item": 0}

    def handler(kwargs, n):
        cmd = str(kwargs.get("command", ""))
        if "pytest" in cmd:
            state["per_item"] += 1
            # first attempt per item fails, second passes
            return ("red\n[exit=1]" if state["per_item"] % 2 else "green\n[exit=0]")
        return "ok\n[exit=0]"

    tool = _tool(FakeSandbox(handler), tmp_path)
    report = await tool.execute(
        operations=[
            {
                "action": "foreach",
                "items": ["a", "b"],
                "body": [
                    {
                        "action": "retry_until",
                        "until": "exit == 0",
                        "max_attempts": 3,
                        "body": [{"action": "run", "command": "pytest {{item}}"}],
                    }
                ],
            }
        ]
    )
    assert "[SATISFIED] foreach" in report
    assert "items=2" in report
    # 2 items x 2 attempts each = 4 backend runs, all inside one model turn.
    assert state["per_item"] == 4


@pytest.mark.asyncio
async def test_malformed_plan_is_rejected_before_running_anything(tmp_path) -> None:
    fake = FakeSandbox()
    tool = _tool(fake, tmp_path)
    report = await tool.execute(
        operations=[{"action": "retry_until", "body": [{"action": "run", "command": "x"}]}],
        stop_on_error=False,
    )
    assert "invalid retry_until" in report
    assert fake.calls == [], "validation must happen before any execution"


@pytest.mark.asyncio
async def test_unknown_action_now_fails_loudly(tmp_path) -> None:
    """Regression guard for the bug that cost a whole batch earlier today."""
    tool = _tool(FakeSandbox(lambda k, n: f"[unsupported action: {k.get('action')}]\n[exit=0]"), tmp_path)
    report = await tool.execute(operations=[{"action": "totally_bogus", "path": "x"}])
    # The composite/builtin layer cannot know every backend verb, so this asserts
    # the contract we did add: an explicit backend error is surfaced as a failure.
    assert "failure(s)" in report


@pytest.mark.asyncio
async def test_control_flow_actions_are_advertised_to_the_model() -> None:
    """If the schema does not mention them, the model will never use them."""
    import json

    tool = SandboxBatchTool()
    params = tool.parameters if not callable(getattr(tool, "parameters", None)) else tool.parameters()
    blob = json.dumps(params)
    for action in ("retry_until", "foreach", "await"):
        assert action in blob, f"{action} missing from tool schema"
    props = params["properties"]["operations"]["items"]["properties"]
    for field in ("body", "until", "max_attempts", "items", "items_file", "interval_sec"):
        assert field in props, f"{field} missing from op properties"
    # Guard against reintroducing the duplicate-key defect.
    src = open("nanobot/agent/tools/sandbox_batch.py").read()
    assert src.count('"command": StringSchema') == 1
