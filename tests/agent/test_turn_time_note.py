"""The agent's clock: each model request carries elapsed/remaining turn time.

The note is the model's only sense of time. It must (a) reach the model on every
request while a budget exists, (b) never be written into the transcript the
caller persists, and (c) be switchable off for an A/B comparison.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.runtime import time_note_enabled, turn_time_note

NOTE_TAG = "[Time check]"


def _runtime() -> LLMRuntime:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="", tool_calls=[], finish_reason="error")
    )
    return LLMRuntime(
        provider=provider,
        model="test-model",
        generation=MagicMock(),
        context_window_tokens=10000,
    )


# --- the message --------------------------------------------------------------


def test_note_reports_used_and_remaining_minutes() -> None:
    note = turn_time_note(elapsed_s=600.0, budget_s=1800.0)
    assert note["role"] == "user"
    assert "10 of 30 minutes used" in note["content"]
    assert "about 20 minutes left" in note["content"]


def test_note_asks_for_batching_while_time_remains() -> None:
    note = turn_time_note(elapsed_s=60.0, budget_s=1800.0)
    assert "Batch independent tool calls" in note["content"]
    assert "Wrap up now" not in note["content"]


def test_note_orders_a_wrap_up_when_a_quarter_or_less_is_left() -> None:
    note = turn_time_note(elapsed_s=1400.0, budget_s=1800.0)  # 400s left < 450s
    assert "Wrap up now" in note["content"]


def test_note_clamps_past_the_budget_to_zero_left() -> None:
    note = turn_time_note(elapsed_s=2000.0, budget_s=1800.0)
    assert "about 0 minutes left" in note["content"]
    assert "Wrap up now" in note["content"]


def test_note_is_its_own_user_message_after_a_tool_result() -> None:
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    view = [*messages, turn_time_note(60.0, 1800.0)]
    assert view[-1]["role"] == "user"
    assert NOTE_TAG in view[-1]["content"]
    assert view[-2]["role"] == "tool"


# --- the switch ---------------------------------------------------------------


def test_time_note_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POWERX_TIME_NOTE", raising=False)
    assert time_note_enabled() is True


@pytest.mark.parametrize("value", ["off", "OFF", " off "])
def test_time_note_switch_turns_it_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("POWERX_TIME_NOTE", value)
    assert time_note_enabled() is False


# --- the loop -----------------------------------------------------------------


def _capturing_runner(seen: list[list[dict]]) -> AgentRunner:
    runner = AgentRunner()
    counter = {"n": 0}

    async def fake_request_model(spec, messages, hook, *args, **kwargs):
        spec.llm_calls[0] += 1
        counter["n"] += 1
        seen.append(list(messages))
        if counter["n"] > 2:
            return LLMResponse(content="done", tool_calls=[], finish_reason="stop")
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id=f"t{counter['n']}",
                    name="read_file",
                    arguments={"path": f"f{counter['n']}.txt"},
                )
            ],
            finish_reason="tool_calls",
        )

    runner._request_model = fake_request_model  # type: ignore[assignment]
    return runner


def _tools() -> MagicMock:
    tool = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    tools = MagicMock()
    tools.prepare_call.side_effect = lambda name, params: (tool, params, None)
    tools.get_definitions.return_value = []
    return tools


def _note_count(messages: list[dict]) -> int:
    return sum(1 for m in messages if NOTE_TAG in str(m.get("content", "")))


@pytest.mark.asyncio
async def test_every_request_gets_the_note_and_the_transcript_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POWERX_TIME_NOTE", raising=False)
    seen: list[list[dict]] = []
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "build it"}],
        tools=_tools(),
        runtime=_runtime(),
        max_iterations=10,
        max_tool_result_chars=1000,
        concurrent_tools=False,
        turn_budget_s=1800.0,
    )

    result = await _capturing_runner(seen).run(spec)

    assert len(seen) == 3
    # Exactly one note per request, never stacked up across iterations.
    assert [_note_count(req) for req in seen] == [1, 1, 1]
    # The note is not in the saved transcript.
    assert _note_count(result.messages) == 0


@pytest.mark.asyncio
async def test_no_note_when_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERX_TIME_NOTE", "off")
    seen: list[list[dict]] = []
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "build it"}],
        tools=_tools(),
        runtime=_runtime(),
        max_iterations=10,
        max_tool_result_chars=1000,
        concurrent_tools=False,
        turn_budget_s=1800.0,
    )

    await _capturing_runner(seen).run(spec)

    assert all(_note_count(req) == 0 for req in seen)


@pytest.mark.asyncio
async def test_no_note_without_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POWERX_TIME_NOTE", raising=False)
    seen: list[list[dict]] = []
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "build it"}],
        tools=_tools(),
        runtime=_runtime(),
        max_iterations=10,
        max_tool_result_chars=1000,
        concurrent_tools=False,
        turn_budget_s=0,
    )

    await _capturing_runner(seen).run(spec)

    assert all(_note_count(req) == 0 for req in seen)
