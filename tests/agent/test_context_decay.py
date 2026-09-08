"""Tests for age-based decay of stale tool results.

An agent turn re-sends its entire message history on every iteration, so a long
turn pays repeatedly for output it has already acted on. Decay collapses old tool
results to a one-line stub while keeping the most recent ones verbatim, which is
what makes 200-iteration turns affordable.

These tests pin down the two things that must hold: the model must never lose the
*verdict* of an earlier step, and the very last tool result must always survive
untouched (it is what the model is currently reasoning about).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.context_governance import ContextGovernanceConfig, ContextGovernor


class _FakeRegistry:
    def get_definitions(self) -> list[dict[str, Any]]:
        return []


@pytest.fixture()
def governor() -> ContextGovernor:
    return ContextGovernor()


@pytest.fixture()
def config(tmp_path: Path) -> ContextGovernanceConfig:
    return ContextGovernanceConfig(
        provider=None,
        model="deepseek-v4-flash",
        tools=_FakeRegistry(),
        workspace=tmp_path,
        session_key="test",
        max_tool_result_chars=16_000,
        context_window_tokens=1_000_000,
    )


def _history(n_steps: int, *, payload: str | None = None) -> list[dict[str, Any]]:
    body = payload if payload is not None else "op output " * 900  # ~8KB per result
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "do the thing"},
    ]
    for i in range(n_steps):
        msgs.append(
            {
                "role": "assistant",
                "content": f"step {i}",
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "sandbox_batch", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "sandbox_batch", "content": body})
    return msgs


# --------------------------------------------------------------------------
# window semantics
# --------------------------------------------------------------------------
def test_short_history_is_left_completely_alone(governor, config) -> None:
    """Under the keep-window there is nothing stale; output must be untouched."""
    msgs = _history(ContextGovernor.RECENT_TOOL_RESULTS_KEPT)
    out = governor.apply_tool_result_budget(config, msgs)
    assert out == msgs or all(
        o["content"] == m["content"] for o, m in zip(out, msgs) if m.get("role") == "tool"
    )


def test_only_the_recent_window_stays_verbatim(governor, config) -> None:
    msgs = _history(20)
    out = governor.apply_tool_result_budget(config, msgs)
    tool_positions = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    aged = set(tool_positions[: -ContextGovernor.RECENT_TOOL_RESULTS_KEPT])
    kept = set(tool_positions[-ContextGovernor.RECENT_TOOL_RESULTS_KEPT :])

    for i in kept:
        assert len(out[i]["content"]) > 1_000, f"recent result {i} should stay verbatim"
    for i in aged:
        assert len(out[i]["content"]) <= ContextGovernor._STUB_MAX_CHARS, (
            f"stale result {i} should have decayed to a stub"
        )


def test_newest_result_is_never_compacted(governor, config) -> None:
    """The model is reasoning about the last result right now; truncating it
    would corrupt the very decision the next call has to make."""
    msgs = _history(40)
    out = governor.apply_tool_result_budget(config, msgs)
    last_tool = [m for m in out if m.get("role") == "tool"][-1]
    assert len(last_tool["content"]) > 1_000


def test_decay_bounds_a_long_turn_prompt(governor, config) -> None:
    msgs = _history(50)
    raw = sum(len(m.get("content", "")) for m in msgs if m.get("role") == "tool")
    out = governor.apply_tool_result_budget(config, msgs)
    after = sum(len(m.get("content", "")) for m in out if m.get("role") == "tool")
    assert after < raw / 5, f"expected >5x shrink, got {raw}/{after}"


# --------------------------------------------------------------------------
# information preservation
# --------------------------------------------------------------------------
def test_stub_keeps_exit_code_and_verdict(governor, config) -> None:
    body = ("." * 5_000) + "\nFAILED src/x.py::test_y - AssertionError\n2 failed, 3 passed\n[exit=1]"
    msgs = _history(12, payload=body)
    out = governor.apply_tool_result_budget(config, msgs)
    stale = [m for m in out if m.get("role") == "tool"][0]
    assert "exit=1" in stale["content"], "verdict must survive decay"
    assert "FAILED src/x.py::test_y" in stale["content"]
    assert "." * 100 not in stale["content"], "progress spam must not survive"


def test_stub_keeps_first_and_last_meaningful_lines(governor, config) -> None:
    body = "running migration for users table\n" + ("noise line here\n" * 800) + "applied 12 migrations OK"
    msgs = _history(12, payload=body)
    out = governor.apply_tool_result_budget(config, msgs)
    stale = [m for m in out if m.get("role") == "tool"][0]["content"]
    assert "running migration" in stale
    assert "applied 12 migrations OK" in stale
    assert "noise line" not in stale


def test_small_results_are_not_rewritten_even_when_old(governor, config) -> None:
    """Decay must not touch content that is already cheap — that would churn the
    prompt prefix and destroy provider-side cache hits."""
    msgs = _history(15, payload="done\n[exit=0]")
    out = governor.apply_tool_result_budget(config, msgs)
    assert all(
        o["content"] == m["content"]
        for o, m in zip(out, msgs)
        if m.get("role") == "tool"
    )


def test_non_text_content_is_left_alone(governor, config) -> None:
    msgs = _history(12, payload="x" * 5_000)
    blob = {"role": "tool", "tool_call_id": "cX", "name": "image", "content": 12345}
    msgs.append(blob)
    out = governor.apply_tool_result_budget(config, msgs)
    assert out[-1]["content"] == 12345, "non-string content must pass through untouched"


# --------------------------------------------------------------------------
# purity
# --------------------------------------------------------------------------
def test_input_messages_are_not_mutated(governor, config) -> None:
    """The persisted transcript keeps full fidelity; only the model view decays."""
    msgs = _history(20)
    before = [m.get("content") for m in msgs]
    governor.apply_tool_result_budget(config, msgs)
    assert [m.get("content") for m in msgs] == before


def test_decay_is_idempotent(governor, config) -> None:
    msgs = _history(20)
    once = governor.apply_tool_result_budget(config, msgs)
    twice = governor.apply_tool_result_budget(config, once)
    assert [m.get("content") for m in once] == [m.get("content") for m in twice]


def test_assistant_and_user_messages_are_untouched(governor, config) -> None:
    msgs = _history(20)
    out = governor.apply_tool_result_budget(config, msgs)
    for i, m in enumerate(msgs):
        if m.get("role") != "tool":
            assert out[i] == m


def test_zero_kept_window_disables_decay(governor, config, monkeypatch) -> None:
    monkeypatch.setattr(ContextGovernor, "RECENT_TOOL_RESULTS_KEPT", 0)
    msgs = _history(10)
    out = governor.apply_tool_result_budget(config, msgs)
    # With no aged positions, nothing may be rewritten by decay.
    assert all(
        o["content"] == m["content"]
        for o, m in zip(out, msgs)
        if m.get("role") == "tool" and isinstance(m.get("content"), str)
    )
